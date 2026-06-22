"""Three-layer context compaction — keep the Agent's working memory clean.

Layer 1 (micro_compact): Every turn, replace tool_results older than N turns
         with short placeholders. Silent, cheap, always-on.
Layer 2 (auto_compact): When estimated tokens exceed threshold, LLM summarizes
         the full conversation. Transcript saved to disk before compaction.
Layer 3 (manual compact): Agent can call memory.compact_context explicitly
         when persisted history is getting noisy.
"""
from __future__ import annotations

import json
import math
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.agent.blueprint_confirmation_state import pending_blueprint_plan
from app.agent.vision_context import (
    image_token_estimate,
    message_text_for_compare,
    redact_image_data_urls,
    vision_metadata_from_message,
)


KEEP_RECENT_TOOL_RESULTS = 3
TOKEN_THRESHOLD = 50000
CHARS_PER_TOKEN = 3.5  # rough estimate for CJK + English mix
TOOL_RESULT_CONTEXT_BUDGET_CHARS = 3000
PRESERVED_TAIL_TOKEN_BUDGET = 6000
FULL_RESULT_CONTEXT_DETAIL_VALUES = {"full", "detail", "details", "完整"}
FULL_RESULT_CONTEXT_TOOL_PREFIXES = ("skill.",)


def _safe_path_component(value: str, fallback: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value or fallback)


def transcripts_dir() -> Path:
    """Use mounted project data so transcripts survive API container rebuilds."""
    from app.config import settings

    path = Path(settings.PROJECT_ROOT) / "data" / "transcripts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def tool_results_dir() -> Path:
    """Directory for large tool results removed from model context."""
    from app.config import settings

    path = Path(settings.PROJECT_ROOT) / "data" / "tool_results"
    path.mkdir(parents=True, exist_ok=True)
    return path


def estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate from message content length."""
    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        content_has_images = False
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        total_chars += len(str(part.get("text", "")))
                    elif part.get("type") == "image_url":
                        content_has_images = True
                        total_chars += int(image_token_estimate() * CHARS_PER_TOKEN)
                    else:
                        total_chars += len(str(part.get("content", "")))
        if not content_has_images:
            metadata = msg.get("_metadata") if isinstance(msg.get("_metadata"), dict) else {}
            payload = vision_metadata_from_message(metadata)
            images = payload.get("images") if isinstance(payload, dict) else None
            if isinstance(images, list) and images:
                total_chars += int(len(images) * image_token_estimate() * CHARS_PER_TOKEN)
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            total_chars += len(json.dumps(tool_calls, ensure_ascii=False, default=str))
    return math.ceil(total_chars / CHARS_PER_TOKEN)


def _estimate_text_tokens(messages: list[dict]) -> int:
    """Rough text-only token estimate for compacted tail retention.

    This mirrors Codex remote compaction v2: images remain independent content
    items and do not consume the text truncation budget used to decide which
    recent messages survive compaction.
    """
    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total_chars += len(str(part.get("text", "")))
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            total_chars += len(json.dumps(tool_calls, ensure_ascii=False, default=str))
    return math.ceil(total_chars / CHARS_PER_TOKEN)


def micro_compact(messages: list[dict]) -> list[dict]:
    """Layer 1: Replace legacy tool_result blocks with short placeholders.

    OpenAI-style ``role=tool`` messages are append-only within a run. Mutating
    already-sent tool messages breaks prompt-cache prefixes; completed rounds
    are summarized separately before being persisted to chat history.

    This still supports the legacy Anthropic-style tool_result blocks used by
    the tutorial harness.
    Vision image context is persisted history, not a tool result; keep it
    stable during a run so prompt-cache prefixes do not drift mid-loop.
    Keeps the most recent KEEP_RECENT_TOOL_RESULTS intact.
    Modifies messages in-place and returns the same list.
    """
    tool_results: list[tuple[str, Any]] = []

    for msg in messages:
        content = msg.get("content")
        if msg.get("role") != "user" or not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "tool_result":
                tool_results.append(("block", part))

    if len(tool_results) <= KEEP_RECENT_TOOL_RESULTS:
        return messages

    for _, target in tool_results[:-KEEP_RECENT_TOOL_RESULTS]:
        content_str = target.get("content", "")
        if isinstance(content_str, str) and len(content_str) > 200:
            target["content"] = "[Previous tool result - compacted]"

    return messages


def save_transcript(messages: list[dict], project_id: str = "") -> Path:
    """Save full conversation to disk before compaction."""
    prefix = f"{project_id}_" if project_id else ""
    filename = f"{prefix}{int(time.time())}.jsonl"
    path = transcripts_dir() / filename
    with open(path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(redact_image_data_urls(msg), ensure_ascii=False, default=str) + "\n")
    return path


def _truncate_text(value: Any, limit: int = 1600) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"


def _summarize_project_state(result: dict[str, Any]) -> dict[str, Any]:
    pending_blueprint = pending_blueprint_plan(result)
    blueprint = result.get("project_blueprint")
    if isinstance(blueprint, dict):
        blueprint_summary: dict[str, Any] = {
            "status": blueprint.get("status"),
            "title": blueprint.get("title") or blueprint.get("name"),
            "selected_video_mode": blueprint.get("selected_video_mode") or blueprint.get("video_mode"),
        }
    else:
        semantic_blueprint = result.get("semantic_blueprint")
        if isinstance(semantic_blueprint, dict):
            blueprint_summary = {
                "status": semantic_blueprint.get("status"),
                "title": semantic_blueprint.get("title"),
                "tree_version": semantic_blueprint.get("tree_version"),
                "node_count": semantic_blueprint.get("node_count"),
                "source": "semantic_blueprint_file",
                "needs_finalize": bool(semantic_blueprint.get("needs_finalize")),
            }
            fields = semantic_blueprint.get("fields") if isinstance(semantic_blueprint.get("fields"), dict) else {}
            if fields:
                blueprint_summary["fields"] = {
                    key: fields.get(key)
                    for key in ("episode_count", "segment_seconds", "production_basis")
                    if fields.get(key) not in (None, "", [], {})
                }
        else:
            blueprint_summary = {"status": "none"}

    token_summary = result.get("agent_token_usage_summary")
    if not isinstance(token_summary, dict):
        token_summary = {}
    reference_summary = result.get("reference_assets_summary")
    if not isinstance(reference_summary, dict):
        reference_summary = {}

    return {
        "status": "error" if result.get("error") or result.get("ok") is False else "ok",
        "title": result.get("title"),
        "project_mode": result.get("project_mode"),
        "project_sub_mode": result.get("project_sub_mode"),
        "selected_video_mode": result.get("selected_video_mode"),
        "blueprint": blueprint_summary,
        "pending": {
            "pending_blueprint_confirmation": bool(pending_blueprint),
            "pending_blueprint_tree_version": pending_blueprint.get("tree_version") if isinstance(pending_blueprint, dict) else None,
            "pending_reset_confirm": bool(result.get("_pending_reset_confirm")),
            "pending_blueprint_review": bool(result.get("pending_blueprint_review")),
            "pending_blueprint_section_review": bool(result.get("pending_blueprint_section_review")),
        },
        "reference_assets_summary": reference_summary,
        "agent_token_usage_summary": token_summary,
    }


def _summarize_deferred_guide(result: dict[str, Any]) -> dict[str, Any]:
    references_count = result.get("references_count")
    if not isinstance(references_count, int):
        references_count = len(result.get("references") or []) if isinstance(result.get("references"), list) else 0
    return {
        "status": "error" if result.get("error") or result.get("ok") is False else "ok",
        "deferred_tool": result.get("_deferred_tool"),
        "topic": result.get("topic"),
        "detail": result.get("detail"),
        "guidance": _truncate_text(result.get("guidance"), 1800),
        "references_count": references_count,
        "reference_policy": result.get("reference_policy"),
        "available_topics": (result.get("available_topics") or [])[:40] if isinstance(result.get("available_topics"), list) else [],
        "has_full_guide": result.get("has_full_guide"),
        "full_guide_request": result.get("full_guide_request"),
    }


def _resolved_tool_name(tool_name: str, result: dict[str, Any]) -> str:
    return str(result.get("_deferred_tool") or tool_name or "")


def _allows_full_result_context(tool_name: str, result: dict[str, Any]) -> bool:
    resolved_tool = _resolved_tool_name(tool_name, result)
    if not resolved_tool.startswith(FULL_RESULT_CONTEXT_TOOL_PREFIXES):
        return False
    detail = str(result.get("detail") or "").strip().lower()
    if detail not in FULL_RESULT_CONTEXT_DETAIL_VALUES:
        return False
    return result.get("guidance") not in (None, "", [], {}) or result.get("guide_content") not in (None, "", [], {})


def _full_result_context_payload(tool_name: str, result: dict[str, Any]) -> dict[str, Any] | None:
    if not _allows_full_result_context(tool_name, result):
        return None

    payload = deepcopy(result)
    payload["context_policy"] = "full_result"
    payload["context_policy_reason"] = (
        "This tool is allowed to expose full model-visible content when detail='full'."
    )
    return payload


def _context_payload_policy(tool_name: str, result: Any) -> str:
    if isinstance(result, dict) and _allows_full_result_context(tool_name, result):
        return "full_result"
    return "summary"


def _summarize_reference_result(result: dict[str, Any]) -> dict[str, Any]:
    refs = result.get("refs") or result.get("assets") or result.get("matches") or result.get("candidates") or []
    compact_refs: list[dict[str, Any]] = []
    if isinstance(refs, list):
        for item in refs[:12]:
            if not isinstance(item, dict):
                continue
            compact_refs.append({
                "ref_id": item.get("ref_id"),
                "mention": item.get("mention"),
                "label": item.get("label"),
                "reference_input": item.get("reference_input") or item.get("rel_path") or item.get("url"),
                "node_id": item.get("node_id"),
                "status": item.get("status"),
                "roles": item.get("roles"),
            })
    return {
        "status": "error" if result.get("error") or result.get("ok") is False else "ok",
        "action": result.get("action"),
        "ref_id": result.get("ref_id"),
        "mention": result.get("mention"),
        "reference_input": result.get("reference_input"),
        "error": result.get("error"),
        "error_kind": result.get("error_kind"),
        "refs": compact_refs,
        "next_action": result.get("next_action") or result.get("hint"),
    }


def _copy_present(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in keys:
        value = source.get(key)
        if value not in (None, "", [], {}):
            payload[key] = value
    return payload


def _coerce_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _node_prompt_text(node: dict[str, Any]) -> str:
    fields = _coerce_dict(node.get("fields"))
    input_payload = _coerce_dict(node.get("input") or node.get("input_json"))
    input_fields = _coerce_dict(input_payload.get("fields"))
    for source in (node, fields, input_payload, input_fields):
        value = source.get("prompt") if isinstance(source, dict) else None
        if value not in (None, "", [], {}):
            return str(value)
    preview = node.get("prompt_preview")
    if preview not in (None, "", [], {}):
        return str(preview)
    return ""


def _summarize_node_list_item(node: Any) -> dict[str, Any]:
    if not isinstance(node, dict):
        return {"value": _truncate_text(node, 120)}
    node_type = str(node.get("type") or "")
    prompt = _node_prompt_text(node)
    payload: dict[str, Any] = {
        "id": node.get("id"),
        "node_id": node.get("node_id") or node.get("id"),
        "type": node.get("type"),
        "title": node.get("title"),
        "status": node.get("status"),
        "prompt_preview": prompt[:20],
    }
    payload.update(_copy_present(node, ("surface", "render_state", "output_summary", "error", "error_kind", "error_message")))
    if prompt:
        payload["prompt_chars"] = node.get("prompt_chars") or len(prompt)

    input_payload = _summarize_node_input(node.get("input") or node.get("input_json"), node_type=node_type)
    fields_payload = _summarize_node_input(node.get("fields"), node_type=node_type)
    merged_input = {**fields_payload, **input_payload}
    for key in ("purpose", "stage", "aspect_ratio", "resolution", "quality", "duration_seconds", "production_path", "references", "depends_on"):
        value = merged_input.get(key)
        if value not in (None, "", [], {}):
            payload[key] = value
    required_keys = {"id", "node_id", "title", "status", "prompt_preview"}
    return {
        key: value
        for key, value in payload.items()
        if key in required_keys or value not in (None, "", [], {})
    }


def _summarize_node_list_result(result: dict[str, Any]) -> dict[str, Any]:
    nodes = result.get("nodes")
    compact_nodes = [_summarize_node_list_item(node) for node in nodes] if isinstance(nodes, list) else []
    payload: dict[str, Any] = {
        "status": "error" if result.get("error") or result.get("ok") is False else "ok",
        "project_id": result.get("project_id"),
        "total": result.get("total"),
        "returned": result.get("returned", len(compact_nodes)),
        "truncated": result.get("truncated"),
        "next_action": result.get("next_action"),
        "filters": result.get("filters"),
        "nodes": compact_nodes,
    }
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def _summarize_node_get_batch_result(result: dict[str, Any]) -> dict[str, Any]:
    nodes = result.get("nodes")
    if not isinstance(nodes, list):
        return _summarize_media_node_result(result)
    compact_nodes = [
        _summarize_media_node_result(node)
        for node in nodes
        if isinstance(node, dict)
    ]
    payload: dict[str, Any] = {
        "status": result.get("status") or ("error" if result.get("error") or result.get("ok") is False else "ok"),
        "project_id": result.get("project_id"),
        "requested": result.get("requested"),
        "returned": result.get("returned", len(compact_nodes)),
        "nodes": compact_nodes,
    }
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        payload["errors"] = [
            _copy_present(item, ("node_id", "error", "error_kind", "hint"))
            for item in errors
            if isinstance(item, dict)
        ]
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def _summarize_node_mutation_result(result: dict[str, Any]) -> dict[str, Any]:
    nodes = result.get("nodes")
    results = result.get("results")
    items = nodes if isinstance(nodes, list) else results if isinstance(results, list) else None
    if not isinstance(items, list):
        return _summarize_media_node_result(result)
    compact_items: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        compact = _summarize_node_list_item(item)
        compact.update(_copy_present(item, ("index", "client_ref", "requires_rerun", "review_status", "recommended_tool")))
        compact_items.append(compact)
    payload: dict[str, Any] = {
        "status": result.get("status") or ("error" if result.get("error") or result.get("ok") is False else "ok"),
        "project_id": result.get("project_id"),
        "requested": result.get("requested"),
        "created_count": result.get("created_count"),
        "updated_count": result.get("updated_count"),
        "failed_count": result.get("failed_count"),
        "nodes" if isinstance(nodes, list) else "results": compact_items,
        "client_node_ids": result.get("client_node_ids"),
        "next_action": result.get("next_action"),
    }
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        payload["errors"] = [
            _copy_present(item, ("index", "node_id", "client_ref", "error", "error_kind", "hint"))
            for item in errors
            if isinstance(item, dict)
        ]
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def _summarize_media_stage(stage: Any) -> dict[str, Any]:
    if not isinstance(stage, dict):
        return {}
    return _copy_present(
        stage,
        (
            "name",
            "status",
            "url",
            "local_url",
            "remote_url",
            "size",
            "aspect_ratio",
            "quality",
            "duration_seconds",
            "error",
            "error_kind",
        ),
    )


def _summarize_node_io(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    payload = _copy_present(
        value,
        (
            "type",
            "status",
            "subject",
            "url",
            "local_url",
            "remote_url",
            "size",
            "aspect_ratio",
            "quality",
            "duration_seconds",
            "error",
            "error_kind",
        ),
    )
    stages = value.get("stages")
    if isinstance(stages, list):
        compact_stages = [_summarize_media_stage(stage) for stage in stages[:4]]
        compact_stages = [stage for stage in compact_stages if stage]
        if compact_stages:
            payload["stages"] = compact_stages
    images = value.get("images")
    if isinstance(images, list):
        compact_images: list[dict[str, Any]] = []
        for item in images[:4]:
            if isinstance(item, dict):
                compact = _copy_present(
                    item,
                    ("status", "url", "local_url", "remote_url", "size", "aspect_ratio", "quality"),
                )
                if compact:
                    compact_images.append(compact)
        if compact_images:
            payload["images"] = compact_images
    content = value.get("content") or value.get("text")
    if content not in (None, "", [], {}):
        payload["content_preview"] = _truncate_text(content, 1200)
    return payload


def _summarize_node_input(value: Any, *, node_type: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    payload = _copy_present(
        value,
        (
            "title",
            "purpose",
            "stage",
            "aspect_ratio",
            "resolution",
            "quality",
            "duration_seconds",
            "production_path",
            "references",
            "depends_on",
            "reference_images",
        ),
    )
    prompt = value.get("prompt")
    if prompt not in (None, "", [], {}):
        limit = 1200 if node_type in {"image", "video"} else 600
        payload["prompt_preview"] = _truncate_text(prompt, limit)
        payload["prompt_chars"] = len(str(prompt))
    content = value.get("content") or value.get("description")
    if content not in (None, "", [], {}):
        payload["content_preview"] = _truncate_text(content, 1400 if node_type == "text" else 600)
        payload["content_chars"] = len(str(content))
    return payload


def _summarize_media_node_result(result: dict[str, Any]) -> dict[str, Any]:
    node_type = str(result.get("type") or "")
    payload: dict[str, Any] = {
        "status": "error" if result.get("error") or result.get("ok") is False else "ok",
    }
    payload.update(_copy_present(result, ("id", "node_id", "type", "title", "action", "status", "url", "local_url", "remote_url")))

    output_payload = _summarize_node_io(result.get("output") or result.get("output_json"))
    if output_payload:
        payload["output"] = output_payload

    run_result = result.get("result")
    if isinstance(run_result, dict):
        nested = _summarize_node_io(run_result)
        nested.update(_copy_present(run_result, ("n_succeeded", "n_failed", "reference_warnings", "reference_images")))
        if nested:
            payload["result"] = nested

    input_payload = _summarize_node_input(result.get("input") or result.get("input_json"), node_type=node_type)
    fields_payload = _summarize_node_input(result.get("fields"), node_type=node_type)
    if fields_payload:
        input_payload = {**fields_payload, **input_payload}
    if input_payload:
        payload["input"] = input_payload
    attempts = result.get("node_render_attempts")
    if isinstance(attempts, list) and attempts:
        compact_attempts: list[dict[str, Any]] = []
        for item in attempts[-3:]:
            if isinstance(item, dict):
                compact = _copy_present(item, ("status", "url", "local_url", "remote_url", "error", "error_kind"))
                if compact:
                    compact_attempts.append(compact)
        if compact_attempts:
            payload["recent_attempts"] = compact_attempts
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def _summarize_review_result(result: dict[str, Any]) -> dict[str, Any]:
    review = result.get("result") if isinstance(result.get("result"), dict) else {}
    findings = review.get("findings") if isinstance(review.get("findings"), list) else []
    missing = review.get("missing_evidence") if isinstance(review.get("missing_evidence"), list) else []
    payload: dict[str, Any] = {
        "status": "error" if result.get("error") or result.get("ok") is False else "ok",
        "review_status": result.get("review_status") or review.get("status"),
        "summary": _truncate_text(result.get("summary"), 600),
        "passed": review.get("passed"),
        "safe_to_run": review.get("safe_to_run"),
        "safe_to_submit": review.get("safe_to_submit"),
        "findings_count": len(findings),
        "blocking_findings_count": sum(
            1 for item in findings if isinstance(item, dict) and item.get("severity") == "blocking"
        ),
        "missing_evidence_count": len(missing),
        "subagent_error": result.get("subagent_error"),
    }
    if findings:
        compact_findings: list[dict[str, Any]] = []
        for item in findings[:5]:
            if not isinstance(item, dict):
                continue
            compact_findings.append({
                "severity": item.get("severity"),
                "issue": _truncate_text(item.get("issue"), 260),
                "evidence": _truncate_text(item.get("evidence"), 220),
                "suggested_fix": _truncate_text(item.get("suggested_fix"), 220),
            })
        payload["findings"] = compact_findings
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def _error_context_payload(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    if not (result.get("error") or result.get("ok") is False):
        return None
    payload: dict[str, Any] = {
        "error": result.get("error"),
        "error_kind": result.get("error_kind"),
        "hint": result.get("hint"),
        "suggested_next": result.get("suggested_next"),
        "model_feedback": result.get("model_feedback"),
    }
    for key in (
        "node_id",
        "parent_id",
        "plan_id",
        "available_node_ids",
        "allowed_fields",
        "missing_fields",
        "expected_aspect_ratio",
        "conflicting_value",
        "supported_aspect_ratios",
    ):
        value = result.get(key)
        if value not in (None, "", [], {}):
            payload[key] = value
    return {k: v for k, v in payload.items() if v not in (None, "", [], {})}


def _context_summary_payload(tool_name: str, result: Any) -> Any:
    if isinstance(result, dict):
        error_context = _error_context_payload(result)
        if error_context:
            return error_context
        full_result_payload = _full_result_context_payload(tool_name, result)
        if full_result_payload is not None:
            return full_result_payload
        model_summary = result.get("model_summary")
        if model_summary not in (None, "", [], {}):
            reference_policy = result.get("reference_policy")
            resolved_tool = _resolved_tool_name(tool_name, result)
            if reference_policy not in (None, "", [], {}) and resolved_tool.startswith("skill."):
                return {
                    "model_summary": model_summary,
                    "reference_policy": reference_policy,
                }
            return model_summary
        if tool_name == "project.get_state":
            return _summarize_project_state(result)
        if tool_name == "tool.execute" and result.get("_deferred_tool") == "skill.project_mentor":
            return _summarize_deferred_guide(result)
        if tool_name == "reference.manage":
            return _summarize_reference_result(result)
        if tool_name in {"node.create", "node.update"}:
            return _summarize_node_mutation_result(result)
        if tool_name == "node.list":
            return _summarize_node_list_result(result)
        if tool_name == "node.get":
            return _summarize_node_get_batch_result(result)
        if tool_name == "node.run":
            return _summarize_media_node_result(result)
        if tool_name == "agent.review":
            return _summarize_review_result(result)
        return _summarize_tool_result(result)
    return _summarize_tool_result(result)


def summarize_tool_result_for_context(tool_name: str, result: Any) -> Any:
    """Return the compact model-visible summary used for large tool results."""
    return _context_summary_payload(tool_name, result)


def _summarize_tool_result(result: Any) -> str:
    if isinstance(result, dict):
        keys = ", ".join(str(k) for k in list(result.keys())[:12])
        status = "ok"
        if result.get("error") or result.get("ok") is False:
            status = "error"
        return f"{status}; keys: {keys}"
    if isinstance(result, list):
        return f"list with {len(result)} items"
    return type(result).__name__


def save_large_tool_result(
    result: Any,
    *,
    project_id: str,
    run_id: str,
    iteration: int,
    tool_name: str,
) -> Path:
    safe_project = _safe_path_component(project_id, "project")
    safe_run = _safe_path_component(run_id, "run")
    safe_tool = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in tool_name or "tool")
    out_dir = tool_results_dir() / safe_project / safe_run
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{int(time.time() * 1000)}_iter{iteration}_{safe_tool}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, default=str, indent=2)
    return path


def prepare_tool_result_for_context(
    result: Any,
    *,
    project_id: str,
    run_id: str,
    iteration: int,
    tool_name: str,
    budget_chars: int = TOOL_RESULT_CONTEXT_BUDGET_CHARS,
) -> str:
    """Serialize a tool result for the LLM context with a default budget.

    Large payloads are written to disk and then exposed according to the tool
    context policy. Most tools use summaries; full skill guides requested with
    detail='full' keep their complete model-visible content.
    """
    content = json.dumps(result, ensure_ascii=False, default=str)
    if len(content) <= budget_chars:
        return content

    path = save_large_tool_result(
        result,
        project_id=project_id,
        run_id=run_id,
        iteration=iteration,
        tool_name=tool_name,
    )
    try:
        rel_path = path.relative_to(Path.cwd())
    except ValueError:
        rel_path = path
    context_policy = _context_payload_policy(tool_name, result)
    summary = {
        "ok": not (isinstance(result, dict) and (result.get("error") or result.get("ok") is False)),
        "tool_result_compacted": True,
        "tool": tool_name,
        "context_policy": context_policy,
        "summary": _context_summary_payload(tool_name, result),
        "full_result_path": str(rel_path),
        "original_chars": len(content),
        "hint": (
            "Full tool result was stored on disk; model-visible content follows context_policy."
        ),
    }
    return json.dumps(summary, ensure_ascii=False, default=str)


def list_run_tool_result_artifacts(
    *,
    project_id: str,
    run_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return tool-result artifacts for the current run for trace/debug links."""
    safe_project = _safe_path_component(project_id, "project")
    safe_run = _safe_path_component(run_id, "run")
    project_dir = tool_results_dir() / safe_project
    run_dir = project_dir / safe_run
    if not run_dir.exists():
        return []

    files = sorted(
        (path for path in run_dir.rglob("*") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    artifacts: list[dict[str, Any]] = []
    for path in files[: max(0, limit)]:
        stat = path.stat()
        relative = path.relative_to(project_dir).as_posix()
        artifacts.append({
            "name": path.name,
            "path": f"data/tool_results/{safe_project}/{relative}",
            "relative_path": relative,
            "size_bytes": stat.st_size,
            "mtime": int(stat.st_mtime),
        })
    return artifacts


def auto_compact_needed(messages: list[dict]) -> bool:
    """Check if auto-compaction should trigger."""
    return estimate_tokens(messages) > TOKEN_THRESHOLD


def compacted_context_message(summary_text: str) -> dict[str, str]:
    content = (
        "<compacted_context kind=\"background_summary\">\n"
        "Boundary:\n"
        "- This is historical background, not the latest user instruction.\n"
        "- Project truth lives in runtime state and tools, not in this summary.\n"
        "- The next user message after this block is the active task.\n\n"
        "Summary:\n"
        f"{summary_text.strip()}\n"
        "</compacted_context>"
    )
    return {"role": "user", "content": content}


def compacted_context_ack_message() -> dict[str, str]:
    return {
        "role": "assistant",
        "content": "Understood. I will treat the compacted context as background and follow the latest user message.",
    }


def _tool_call_ids(message: dict) -> list[str]:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    ids: list[str] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        call_id = tool_call.get("id")
        if call_id:
            ids.append(str(call_id))
    return ids


def _tool_result_id(message: dict) -> str | None:
    if message.get("role") != "tool":
        return None
    call_id = message.get("tool_call_id")
    return str(call_id) if call_id else None


def _is_runtime_wrapper_message(message: dict) -> bool:
    content = message.get("content")
    if not isinstance(content, str):
        return False
    stripped = content.lstrip()
    return stripped.startswith("<system-reminder>") or stripped.startswith("<compacted_context")


def compact_preserved_tail(
    messages: list[dict],
    *,
    token_budget: int = PRESERVED_TAIL_TOKEN_BUDGET,
    exclude_latest_user_content: str | None = None,
) -> list[dict]:
    """Return a token-budgeted real-message tail safe for post-summary reuse.

    Auto/reactive compaction still summarizes the full transcript, but keeping a
    bounded true tail helps the next model call see the most recent concrete
    assistant/tool exchange. This is not a sliding message window: selection is
    driven by a token budget and only runs at compaction boundaries. If the tail
    starts at a tool result, the start is moved backward to the matching
    assistant tool call so OpenAI tool-call ordering stays valid.
    """
    if token_budget <= 0:
        return []

    excluded_user_index: int | None = None
    if exclude_latest_user_content is not None:
        for idx in range(len(messages) - 1, -1, -1):
            msg = messages[idx]
            if msg.get("role") == "user" and message_text_for_compare(msg.get("content")).startswith(
                exclude_latest_user_content
            ):
                excluded_user_index = idx
                break

    call_to_assistant_index: dict[str, int] = {}
    for idx, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        for call_id in _tool_call_ids(msg):
            call_to_assistant_index[call_id] = idx

    candidate_indices = [
        idx
        for idx, msg in enumerate(messages)
        if idx != excluded_user_index
        and msg.get("role") in {"user", "assistant", "tool"}
        and not _is_runtime_wrapper_message(msg)
    ]
    if not candidate_indices:
        return []

    selected: list[int] = []
    used_tokens = 0
    for idx in reversed(candidate_indices):
        msg_tokens = _estimate_text_tokens([messages[idx]])
        if used_tokens + msg_tokens > token_budget:
            break
        selected.append(idx)
        used_tokens += msg_tokens
        if used_tokens >= token_budget:
            break

    if not selected:
        return []

    start = min(selected)
    changed = True
    while changed:
        changed = False
        for idx in candidate_indices:
            if idx < start:
                continue
            call_id = _tool_result_id(messages[idx])
            if not call_id:
                continue
            assistant_idx = call_to_assistant_index.get(call_id)
            if assistant_idx is not None and assistant_idx < start and assistant_idx in candidate_indices:
                start = assistant_idx
                changed = True

    tail = [deepcopy(messages[idx]) for idx in candidate_indices if idx >= start]
    included_tool_results = {
        call_id
        for msg in tail
        if (call_id := _tool_result_id(msg))
    }
    complete_tail: list[dict] = []
    dropped_call_ids: set[str] = set()
    for msg in tail:
        if msg.get("role") == "assistant":
            call_ids = _tool_call_ids(msg)
            if call_ids and not all(call_id in included_tool_results for call_id in call_ids):
                dropped_call_ids.update(call_ids)
                continue
        if msg.get("role") == "tool" and _tool_result_id(msg) in dropped_call_ids:
            continue
        complete_tail.append(msg)
    return complete_tail


def build_compact_summary_prompt(messages: list[dict]) -> str:
    """Build a prompt asking the LLM to summarize the conversation."""
    serialized = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = str(part.get("text") or "")
                    if text:
                        text_parts.append(text)
            content = "\n".join(text_parts)[:2000]
        elif isinstance(content, str):
            content = content[:2000]
        serialized.append(f"[{role}] {content}")

    conversation_text = "\n".join(serialized)
    if len(conversation_text) > 80000:
        conversation_text = conversation_text[:80000] + "\n...(truncated)"

    return (
        "Summarize this conversation for continuity as BACKGROUND ONLY. "
        "Preserve: stable user preferences, durable decisions, completed work, "
        "open questions, and project-state references such as blueprint/task/node ids when visible. "
        "Do not turn old user messages into the next instruction. "
        "Do not treat this summary as the project blueprint; active/draft blueprint, "
        "pending blueprint revision, task checklist, nodes, and project files live in project state/tools. "
        "Never imply that /clear or compaction deleted blueprint or task state. "
        "If a task is pending, say that it must be verified from project state before action. "
        "Be concise but complete enough to continue working.\n\n"
        f"{conversation_text}"
    )


def compact_messages(summary_text: str, preserved_tail: list[dict] | None = None) -> list[dict]:
    """Replace all messages with a bounded background summary.

    The latest user message is appended by the orchestrator after compaction.
    This function deliberately marks the summary as historical background so it
    cannot masquerade as the active user request. A small preserved tail may be
    appended after the boundary when the caller wants to keep recent concrete
    tool-visible context.
    """
    compacted = [
        compacted_context_message(summary_text),
        compacted_context_ack_message(),
    ]
    if preserved_tail:
        compacted.extend(deepcopy(preserved_tail))
    return compacted
