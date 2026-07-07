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


def _summarize_authorized_workflow_refs(result: dict[str, Any]) -> list[dict[str, Any]]:
    refs = result.get("_workflow_spec_authorized_refs")
    if not isinstance(refs, list):
        return []

    compact_refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in reversed(refs):
        if not isinstance(item, dict):
            continue
        template_id = str(item.get("template_id") or "").strip()
        artifact_ref = str(item.get("artifact_ref") or "").strip()
        if not template_id and not artifact_ref:
            continue
        key = (template_id, artifact_ref)
        if key in seen:
            continue
        seen.add(key)
        compact = _copy_present(
            item,
            ("template_id", "artifact_ref", "decision", "version_id", "authorized_by", "authorized_at"),
        )
        input_fields = _summarize_input_fields(item.get("input_fields"))
        if input_fields:
            compact["input_fields"] = input_fields
        compact_refs.append(compact)
        if len(compact_refs) >= 3:
            break
    return compact_refs


def _summarize_workflow_input_values(result: dict[str, Any]) -> dict[str, Any]:
    store = result.get("workflow_input_values")
    if not isinstance(store, dict):
        return {}
    by_workflow = store.get("by_workflow")
    if not isinstance(by_workflow, dict):
        return {}

    workflows: list[dict[str, Any]] = []
    for key, item in list(by_workflow.items())[:3]:
        if not isinstance(item, dict):
            continue
        values = item.get("values") if isinstance(item.get("values"), dict) else {}
        value_preview: dict[str, Any] = {}
        for value_key, value in list(values.items())[:10]:
            if isinstance(value, (str, int, float, bool)) or value is None:
                value_preview[str(value_key)] = _truncate_text(value, 160) if isinstance(value, str) else value
            elif isinstance(value, list):
                value_preview[str(value_key)] = f"<list:{len(value)}>"
            elif isinstance(value, dict):
                value_preview[str(value_key)] = f"<object:{len(value)}>"
        workflows.append({
            key: value
            for key, value in {
                "workflow_key": key,
                "workflow_id": item.get("workflow_id"),
                "artifact_ref": item.get("artifact_ref"),
                "instance_id": item.get("instance_id"),
                "updated_at": item.get("updated_at"),
                "input_keys": list(values.keys())[:20],
                "values_preview": value_preview,
            }.items()
            if value not in (None, "", [], {})
        })
    if not workflows:
        return {}
    return {"by_workflow": workflows}


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
    payload = {
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
        "agent_token_usage_summary": token_summary,
    }
    authorized_refs = _summarize_authorized_workflow_refs(result)
    if authorized_refs:
        payload["latest_authorized_workflow_ref"] = authorized_refs[0]
        payload["authorized_workflow_refs"] = authorized_refs
    active_workflow = result.get("active_workflow")
    if isinstance(active_workflow, dict):
        compact_active = _copy_present(
            active_workflow,
            (
                "workflow_id",
                "template_id",
                "artifact_ref",
                "instance_id",
                "title",
                "name",
                "status",
            ),
        )
        if compact_active:
            payload["active_workflow"] = compact_active
    workflow_inputs = _summarize_workflow_input_values(result)
    if workflow_inputs:
        payload["workflow_input_values"] = workflow_inputs
    return payload


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


def _summarize_input_fields(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    compact_fields: list[dict[str, Any]] = []
    for item in value[:30]:
        if not isinstance(item, dict):
            continue
        compact = _copy_present(
            item,
            (
                "id",
                "name",
                "label",
                "type",
                "required",
                "default",
                "minimum",
                "maximum",
                "enum",
                "options",
                "missing",
            ),
        )
        description = item.get("description")
        if description not in (None, "", [], {}):
            compact["description"] = _truncate_text(description, 180)
        if compact:
            compact_fields.append(compact)
    return compact_fields


def _summarize_workflow_spec_agent_result(result: dict[str, Any], nested: dict[str, Any]) -> dict[str, Any]:
    workflow_summary: dict[str, Any] = {
        "status": nested.get("status") or result.get("status"),
        "decision": nested.get("decision"),
        "template_id": nested.get("template_id"),
        "artifact_ref": nested.get("artifact_ref"),
        "version_id": nested.get("version_id"),
        "next_action": nested.get("next_action"),
    }
    input_fields = _summarize_input_fields(nested.get("input_fields"))
    if input_fields:
        workflow_summary["input_fields"] = input_fields
    preview = nested.get("preview")
    if isinstance(preview, dict):
        workflow_summary["preview"] = _copy_present(
            preview,
            ("id", "name", "title", "description", "summary", "step_count", "workflow_spec_version"),
        )
    validation = nested.get("validation")
    if isinstance(validation, dict):
        workflow_summary["validation"] = _copy_present(
            validation,
            ("ok", "workflow_id", "step_count", "dimension_count", "deferred_group_count"),
        )
        protocol = validation.get("protocol")
        if isinstance(protocol, dict):
            workflow_summary["validation"]["protocol"] = _copy_present(
                protocol,
                ("workflow_spec_version", "required_capabilities", "required_extensions", "extension_ids"),
            )
    return {key: value for key, value in workflow_summary.items() if value not in (None, "", [], {})}


def _summarize_agent_run_result(result: dict[str, Any]) -> dict[str, Any]:
    nested = result.get("result") if isinstance(result.get("result"), dict) else {}
    payload: dict[str, Any] = {
        "_deferred_tool": result.get("_deferred_tool"),
        "ok": result.get("ok"),
        "agent": result.get("agent"),
        "status": result.get("status") or nested.get("status"),
        "summary": _truncate_text(result.get("summary"), 800),
        "steps_used": result.get("steps_used"),
    }
    agent_name = str(result.get("agent") or "").strip()
    if agent_name == "workflow_spec" or nested.get("template_id") or nested.get("artifact_ref"):
        workflow_spec = _summarize_workflow_spec_agent_result(result, nested)
        payload.update(_copy_present(workflow_spec, ("template_id", "artifact_ref", "decision", "next_action")))
        if workflow_spec:
            payload["workflow_spec"] = workflow_spec
    else:
        compact_nested = _copy_present(
            nested,
            (
                "status",
                "node_id",
                "node_ids",
                "completed_node_ids",
                "committed",
                "candidate_ref",
                "committed_ref",
                "operations_summary",
                "verification",
            ),
        )
        if compact_nested:
            payload["subagent_result"] = compact_nested
    available_agents = result.get("available_agents")
    if isinstance(available_agents, list) and result.get("status") == "catalog":
        payload["available_agents"] = available_agents[:20]
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def _resolved_tool_name(tool_name: str, result: dict[str, Any]) -> str:
    return str(result.get("_deferred_tool") or tool_name or "")


def _allows_full_result_context(tool_name: str, result: dict[str, Any]) -> bool:
    resolved_tool = _resolved_tool_name(tool_name, result)
    if not resolved_tool.startswith(FULL_RESULT_CONTEXT_TOOL_PREFIXES):
        return False
    detail = str(result.get("detail") or "").strip().lower()
    if detail not in FULL_RESULT_CONTEXT_DETAIL_VALUES:
        return False
    return (
        result.get("guidance") not in (None, "", [], {})
        or result.get("guide_content") not in (None, "", [], {})
        or (resolved_tool == "skill.get" and result.get("content") not in (None, "", [], {}))
    )


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
    nested_fields = _coerce_dict(value.get("fields"))
    source = {**nested_fields, **value} if nested_fields else value
    payload = _copy_present(
        source,
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
    prompt = source.get("prompt")
    if prompt not in (None, "", [], {}):
        limit = 1200 if node_type in {"image", "video", "audio"} else 600
        payload["prompt_preview"] = _truncate_text(prompt, limit)
        payload["prompt_chars"] = len(str(prompt))
    content = source.get("content") or source.get("description")
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
    if result.get("_deferred_tool") == "agent.run" or result.get("agent"):
        nested = result.get("result") if isinstance(result.get("result"), dict) else {}
        payload.update({
            "agent": result.get("agent"),
            "status": result.get("status"),
            "summary": _truncate_text(result.get("summary"), 800),
            "steps_used": result.get("steps_used"),
            "terminal": result.get("terminal"),
        })
        blocked_fields = {
            "status": nested.get("status"),
            "node_id": nested.get("node_id"),
            "committed": nested.get("committed"),
            "candidate_ref": nested.get("candidate_ref"),
            "committed_ref": nested.get("committed_ref"),
            "operations_summary": _truncate_text(nested.get("operations_summary"), 600),
            "verification": _truncate_text(nested.get("verification"), 600),
            "issues": [
                _truncate_text(item, 260)
                for item in (nested.get("issues") if isinstance(nested.get("issues"), list) else [])[:6]
            ],
        }
        payload["subagent_result"] = {
            key: value
            for key, value in blocked_fields.items()
            if value not in (None, "", [], {})
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
        "repair_ref",
        "content_fields",
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
        if tool_name == "skill.search":
            return _summarize_skill_search_result(result)
        if tool_name == "tool.search":
            return _summarize_tool_search_result(result)
        if tool_name == "tool.describe":
            return _summarize_tool_describe_result(result)
        if tool_name == "workflow.spec.apply_patch":
            return _summarize_workflow_spec_write_result(result)
        if tool_name == "workflow.canvas.inspect":
            return _summarize_workflow_canvas_inspect_result(result)
        if tool_name == "tool.execute" and result.get("_deferred_tool") == "agent.run":
            return _summarize_agent_run_result(result)
        if tool_name == "agent.run":
            return _summarize_agent_run_result(result)
        if tool_name == "tool.execute" and str(result.get("_deferred_tool") or "").startswith("workflow."):
            return _summarize_deferred_workflow_result(result)
        if tool_name == "tool.execute" and result.get("_deferred_tool") == "skill.project_mentor":
            return _summarize_deferred_guide(result)
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


def _summarize_tool_search_result(result: dict[str, Any]) -> dict[str, Any]:
    tools = result.get("tools")
    compact_tools: list[dict[str, Any]] = []
    if isinstance(tools, list):
        for item in tools[:12]:
            if not isinstance(item, dict):
                continue
            compact: dict[str, Any] = {
                "name": str(item.get("name") or ""),
                "category": str(item.get("category") or ""),
                "description": _truncate_text(item.get("description"), 180),
            }
            hints = item.get("usage_hints")
            if isinstance(hints, list):
                compact["usage_hints"] = [
                    _truncate_text(hint, 160)
                    for hint in hints[:2]
                    if str(hint or "").strip()
                ]
            example = item.get("example")
            if example not in (None, "", [], {}):
                compact["example"] = _truncate_text(example, 180)
            schema_summary = item.get("input_schema_summary")
            if isinstance(schema_summary, dict):
                compact["input_schema_summary"] = schema_summary
            compact_tools.append(compact)
    payload: dict[str, Any] = {
        "query": result.get("query"),
        "category": result.get("category"),
        "mode": result.get("mode"),
        "total": result.get("total"),
        "returned": result.get("returned", len(compact_tools)),
        "tools": compact_tools,
    }
    if result.get("not_found") not in (None, "", [], {}):
        payload["not_found"] = result.get("not_found")
    if result.get("catalog") not in (None, "", [], {}):
        catalog = result.get("catalog")
        if isinstance(catalog, dict):
            payload["catalog"] = {
                "total": catalog.get("total"),
                "categories": catalog.get("categories"),
            }
    payload["next_action"] = (
        "Pick a matching deferred tool. If its schema is needed, call tool.describe; "
        "then call tool.execute(name='<tool>', input={...})."
    )
    return payload


def _summarize_skill_search_result(result: dict[str, Any]) -> dict[str, Any]:
    def compact_skill(item: dict[str, Any]) -> dict[str, Any]:
        compact = _copy_present(
            item,
            (
                "name",
                "category",
                "description",
                "applies_to",
                "scope",
                "source",
                "usage",
                "recommended_tool",
            ),
        )
        direct = item.get("direct_template")
        if isinstance(direct, dict):
            compact["direct_template"] = _copy_present(
                direct,
                (
                    "template_id",
                    "name",
                    "scope",
                    "source",
                    "description",
                    "inputs",
                    "required_inputs",
                    "missing_inputs",
                    "input_fields",
                    "input_questions",
                    "recommended_tool",
                    "next_action",
                ),
            )
        return compact

    skills = result.get("skills")
    compact_skills = [
        compact_skill(item)
        for item in (skills[:8] if isinstance(skills, list) else [])
        if isinstance(item, dict)
    ]
    groups = result.get("groups")
    compact_groups: list[dict[str, Any]] = []
    if isinstance(groups, list):
        for group in groups[:6]:
            if not isinstance(group, dict):
                continue
            group_skills = group.get("skills")
            compact_groups.append({
                "query": group.get("query"),
                "total": group.get("total"),
                "skills": [
                    compact_skill(item)
                    for item in (group_skills[:5] if isinstance(group_skills, list) else [])
                    if isinstance(item, dict)
                ],
            })
    payload = {
        "ok": result.get("ok"),
        "mode": result.get("mode"),
        "query": result.get("query"),
        "queries": result.get("queries"),
        "category": result.get("category"),
        "scope_filter": result.get("scope_filter"),
        "skills": compact_skills,
        "total": result.get("total"),
        "groups": compact_groups,
        "hint": result.get("hint"),
    }
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def _summarize_tool_describe_result(result: dict[str, Any]) -> dict[str, Any]:
    described = result.get("tools")
    compact_tools: list[dict[str, Any]] = []
    if isinstance(described, list):
        for item in described[:8]:
            if not isinstance(item, dict):
                continue
            schema = item.get("input_schema")
            properties = schema.get("properties") if isinstance(schema, dict) else {}
            required = schema.get("required") if isinstance(schema, dict) else []
            prop_names = list(properties.keys())[:16] if isinstance(properties, dict) else []
            compact: dict[str, Any] = {
                "name": str(item.get("name") or ""),
                "category": str(item.get("category") or ""),
                "description": _truncate_text(item.get("description"), 220),
                "required": required[:12] if isinstance(required, list) else [],
                "properties": [str(name) for name in prop_names],
            }
            hints = item.get("usage_hints")
            if isinstance(hints, list):
                compact["usage_hints"] = [
                    _truncate_text(hint, 180)
                    for hint in hints[:3]
                    if str(hint or "").strip()
                ]
            example = item.get("example")
            if example not in (None, "", [], {}):
                compact["example"] = _truncate_text(example, 220)
            compact_tools.append(compact)
    return {
        "tools": compact_tools,
        "not_found": result.get("not_found") if isinstance(result.get("not_found"), list) else [],
        "next_action": "Call tool.execute with one of these deferred tool names and its input object.",
    }


def _summarize_deferred_workflow_result(result: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "_deferred_tool": result.get("_deferred_tool"),
        "ok": result.get("ok"),
        "status": result.get("status"),
        "template_id": result.get("template_id"),
        "template_name": result.get("template_name"),
        "artifact_ref": result.get("artifact_ref"),
        "instance_id": result.get("instance_id"),
        "total": result.get("total"),
        "returned": result.get("returned"),
        "created_count": result.get("created_count"),
        "edges_count": result.get("edges_count"),
        "deferred_group_count": result.get("deferred_group_count"),
        "decision_hint": result.get("decision_hint"),
        "next_action": result.get("next_action"),
    }
    candidates = result.get("candidates")
    if isinstance(candidates, list):
        payload["candidates"] = [
            _copy_present(
                item,
                (
                    "id",
                    "name",
                    "description",
                    "category",
                    "applies_to",
                    "scope",
                    "source",
                    "inputs",
                    "required_inputs",
                    "missing_inputs",
                    "input_fields",
                    "input_questions",
                    "step_count",
                    "match_score",
                ),
            )
            for item in candidates[:5]
            if isinstance(item, dict)
        ]
    direct = result.get("direct_template")
    if isinstance(direct, dict):
        payload["direct_template"] = _copy_present(
            direct,
            (
                "template_id",
                "id",
                "name",
                "scope",
                "source",
                "inputs",
                "required_inputs",
                "missing_inputs",
                "input_fields",
                "input_questions",
                "recommended_tool",
                "next_action",
            ),
        )
    templates = result.get("templates")
    if isinstance(templates, list):
        payload["templates"] = [
            _copy_present(
                item,
                ("id", "name", "description", "category", "applies_to", "inputs", "required_inputs", "step_count"),
            )
            for item in templates[:8]
            if isinstance(item, dict)
        ]
    nodes = result.get("nodes")
    if isinstance(nodes, list):
        compact_nodes: list[dict[str, Any]] = []
        for item in nodes[:8]:
            if not isinstance(item, dict):
                continue
            compact_nodes.append(_copy_present(item, ("id", "type", "title", "status", "node_id")))
        payload["nodes"] = compact_nodes
    runtime = result.get("runtime")
    if isinstance(runtime, dict):
        payload["runtime"] = _copy_present(runtime, ("instance_id", "template_id", "template_name", "status", "current_step_id", "progress"))
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def _summarize_input_fields(fields: Any, *, limit: int = 12) -> list[dict[str, Any]]:
    if not isinstance(fields, list):
        return []
    summarized: list[dict[str, Any]] = []
    for item in fields[:limit]:
        if not isinstance(item, dict):
            continue
        summarized.append(
            _copy_present(
                item,
                ("id", "label", "type", "required", "missing", "default", "minimum", "unit"),
            )
        )
    return [item for item in summarized if item]


def _summarize_workflow_dry_run(dry_run: Any) -> dict[str, Any]:
    if not isinstance(dry_run, dict):
        return {}
    summary = _copy_present(
        dry_run,
        (
            "status",
            "ok",
            "summary",
            "step_count",
            "executable_step_count",
            "repeat_instance_count",
            "duration_segment_expectation",
            "visible_output_ids",
            "leaf_visible_output_ids",
            "final_output_ids",
            "reachable_final_output_ids",
            "executable_batches",
            "repeat_groups",
        ),
    )
    for key in (
        "duration_segment_expectation",
        "visible_output_ids",
        "leaf_visible_output_ids",
        "final_output_ids",
        "reachable_final_output_ids",
        "repeat_groups",
    ):
        if key in dry_run:
            value = dry_run.get(key)
            if isinstance(value, (list, dict)):
                summary[key] = deepcopy(value)
    return summary


def _summarize_workflow_audit(audit: Any) -> dict[str, Any]:
    if not isinstance(audit, dict):
        return {}
    summary = _copy_present(
        audit,
        (
            "status",
            "ok",
            "can_save",
            "can_run",
            "recommended_use",
            "summary",
            "visible_output_count",
            "severity_counts",
        ),
    )
    dry_run = _summarize_workflow_dry_run(audit.get("dry_run"))
    if dry_run:
        summary["dry_run"] = dry_run
    findings = audit.get("findings")
    if isinstance(findings, list) and findings:
        summary["findings"] = [
            _copy_present(item, ("code", "severity", "message", "path", "step_id"))
            for item in findings[:8]
            if isinstance(item, dict)
        ]
    return {key: value for key, value in summary.items() if value not in (None, "", [], {})}


def _summarize_workflow_spec_write_result(result: dict[str, Any]) -> dict[str, Any]:
    payload = _copy_present(
        result,
        (
            "ok",
            "status",
            "operation",
            "save_target",
            "artifact_ref",
            "template_id",
            "version_id",
            "suggested_next",
            "next_action",
        ),
    )
    preview = result.get("preview")
    if isinstance(preview, dict):
        payload["preview"] = _copy_present(
            preview,
            (
                "id",
                "name",
                "description",
                "workflow_spec_version",
                "step_count",
                "dimension_count",
                "deferred_group_count",
                "reusable",
                "input_ids",
                "required_inputs",
                "audit_status",
                "can_save",
                "can_run",
                "recommended_use",
            ),
        )
        if "description" in payload["preview"]:
            payload["preview"]["description"] = _truncate_text(payload["preview"]["description"], 500)
    input_fields = _summarize_input_fields(result.get("input_fields"))
    if input_fields:
        payload["input_fields"] = input_fields
    validation = result.get("validation")
    if isinstance(validation, dict):
        payload["validation"] = _copy_present(
            validation,
            (
                "ok",
                "workflow_id",
                "step_count",
                "dimension_count",
                "deferred_group_count",
            ),
        )
    audit = _summarize_workflow_audit(result.get("audit"))
    if audit:
        payload["audit"] = audit
    self_check = result.get("self_check")
    if isinstance(self_check, dict):
        payload["self_check"] = _copy_present(self_check, ("passed", "checks", "issues"))
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def _summarize_canvas_nodes(nodes: Any, *, limit: int = 16) -> list[dict[str, Any]]:
    if not isinstance(nodes, list):
        return []
    summarized: list[dict[str, Any]] = []
    for item in nodes[:limit]:
        if not isinstance(item, dict):
            continue
        summarized.append(
            _copy_present(
                item,
                ("id", "step_id", "title", "type", "node_type", "depends_on", "display_source"),
            )
        )
    return [item for item in summarized if item]


def _summarize_workflow_canvas_inspect_result(result: dict[str, Any]) -> dict[str, Any]:
    payload = _copy_present(result, ("ok", "status", "schema_version", "next_action", "suggested_next"))
    source = result.get("source")
    if isinstance(source, dict):
        payload["source"] = _copy_present(
            source,
            ("kind", "template_id", "version_id", "artifact_ref", "repair_ref", "scope", "workflow_id"),
        )
    workflow = result.get("workflow")
    if isinstance(workflow, dict):
        payload["workflow"] = _copy_present(
            workflow,
            ("id", "name", "description", "step_count", "canvas_node_count", "final_output_ids"),
        )
        if "description" in payload["workflow"]:
            payload["workflow"]["description"] = _truncate_text(payload["workflow"]["description"], 500)
        if "final_output_ids" in workflow and "final_output_ids" not in payload["workflow"]:
            value = workflow.get("final_output_ids")
            if isinstance(value, list):
                payload["workflow"]["final_output_ids"] = deepcopy(value)
    inputs = result.get("inputs")
    if isinstance(inputs, dict):
        payload["inputs"] = {
            "fields": _summarize_input_fields(inputs.get("fields")),
            "missing_required": inputs.get("missing_required") if isinstance(inputs.get("missing_required"), list) else [],
        }
    dynamic_inputs = result.get("dynamic_inputs")
    if isinstance(dynamic_inputs, dict):
        payload["dynamic_inputs"] = _copy_present(
            dynamic_inputs,
            ("status", "missing_sample_outputs"),
        )
    flow = result.get("flow")
    if isinstance(flow, dict):
        payload["flow"] = _copy_present(flow, ("executable_batches", "repeat_groups"))
    canvas = result.get("canvas")
    if isinstance(canvas, dict):
        payload["canvas"] = {
            "nodes": _summarize_canvas_nodes(canvas.get("nodes")),
            "edges_count": len(canvas.get("edges")) if isinstance(canvas.get("edges"), list) else 0,
            "final_outputs": _summarize_canvas_nodes(canvas.get("final_outputs")),
        }
    validation = result.get("validation")
    if isinstance(validation, dict):
        payload["validation"] = _copy_present(
            validation,
            ("status", "ok", "can_save", "can_run", "recommended_use", "summary", "severity_counts", "issues"),
        )
        dry_run = _summarize_workflow_dry_run(validation.get("dry_run"))
        if dry_run:
            payload["validation"]["dry_run"] = dry_run
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


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
