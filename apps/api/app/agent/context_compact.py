"""Aggregate conversation compaction.

Per-tool output bounding lives in ``app.agent.model_context``. This module only
manages accumulated history and transcript preservation.
"""
from __future__ import annotations

import json
import math
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.agent.model_context.policy import estimate_text_tokens
from app.agent.model_context.truncate import truncate_text_middle
from app.agent.vision_context import (
    image_token_estimate,
    message_text_for_compare,
    redact_image_data_urls,
    vision_metadata_from_message,
)


TOKEN_THRESHOLD = 50_000
PRESERVED_TAIL_TOKEN_BUDGET = 6_000
CODEX_AUTO_COMPACT_RATIO = 0.9
CODEX_RECENT_USER_TOKEN_BUDGET = 20_000
CODEX_COMPACTION_PROMPT = """You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue

Be concise, structured, and focused on helping the next LLM seamlessly continue the work."""
CODEX_SUMMARY_PREFIX = (
    "Another language model started to solve this problem and produced a summary of its "
    "thinking process. You also have access to the state of the tools that were used by "
    "that language model. Use this to build on the work that has already been done and "
    "avoid duplicating work. Here is the summary produced by the other language model, "
    "use the information in this summary to assist with your own analysis:\n"
)


def transcripts_dir() -> Path:
    from app.config import settings

    path = Path(settings.PROJECT_ROOT) / "data" / "transcripts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def estimate_tokens(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        item_type = message.get("type")
        content = message.get("output", "") if item_type == "function_call_output" else message.get("content", "")
        content_has_images = False
        if isinstance(content, str):
            total += estimate_text_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"text", "input_text", "output_text"}:
                    total += estimate_text_tokens(str(part.get("text") or ""))
                elif part.get("type") in {"image_url", "input_image"}:
                    content_has_images = True
                    total += image_token_estimate()
                else:
                    total += estimate_text_tokens(str(part.get("content") or ""))
        if not content_has_images:
            metadata = message.get("_metadata") if isinstance(message.get("_metadata"), dict) else {}
            payload = vision_metadata_from_message(metadata)
            images = payload.get("images") if isinstance(payload, dict) else None
            if isinstance(images, list):
                total += len(images) * image_token_estimate()
        tool_calls = message.get("tool_calls")
        if tool_calls:
            total += estimate_text_tokens(json.dumps(tool_calls, ensure_ascii=False, default=str))
        if item_type == "function_call":
            total += estimate_text_tokens(json.dumps({
                "name": message.get("name"),
                "arguments": message.get("arguments"),
            }, ensure_ascii=False, default=str))
        elif item_type == "reasoning":
            total += estimate_text_tokens(json.dumps(message, ensure_ascii=False, default=str))
    return total


def _estimate_text_tokens(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        item_type = message.get("type")
        content = message.get("output", "") if item_type == "function_call_output" else message.get("content", "")
        if isinstance(content, str):
            total += estimate_text_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"}:
                    total += estimate_text_tokens(str(part.get("text") or ""))
        tool_calls = message.get("tool_calls")
        if tool_calls:
            total += estimate_text_tokens(json.dumps(tool_calls, ensure_ascii=False, default=str))
        if item_type == "function_call":
            total += estimate_text_tokens(str(message.get("arguments") or ""))
    return total


def save_transcript(messages: list[dict], project_id: str = "") -> Path:
    prefix = f"{project_id}_" if project_id else ""
    path = transcripts_dir() / f"{prefix}{time.time_ns()}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for message in messages:
            handle.write(
                json.dumps(redact_image_data_urls(message), ensure_ascii=False, default=str) + "\n"
            )
    return path


def compaction_threshold(
    *,
    context_window_tokens: Any = None,
    max_input_tokens: Any = None,
    explicit_limit: Any = None,
) -> int:
    """Resolve Codex's automatic compaction threshold.

    Codex compacts at 90% of the model context window and lets an explicit
    provider limit lower, but never raise, that boundary. Older providers with
    no capacity metadata retain OpenReel's safe 50k fallback.
    """

    def positive_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    context_window = positive_int(context_window_tokens) or positive_int(max_input_tokens)
    automatic = (
        max(1, math.floor(context_window * CODEX_AUTO_COMPACT_RATIO))
        if context_window
        else TOKEN_THRESHOLD
    )
    configured = positive_int(explicit_limit)
    return min(automatic, configured) if configured else automatic


def estimate_request_tokens(
    messages: list[dict],
    *,
    system: str | None = None,
    tools: list[dict] | None = None,
) -> int:
    total = estimate_tokens(messages)
    if system:
        total += estimate_text_tokens(system)
    if tools:
        total += estimate_text_tokens(json.dumps(tools, ensure_ascii=False, default=str))
    return total


def auto_compact_needed(
    messages: list[dict],
    *,
    threshold: int = TOKEN_THRESHOLD,
    system: str | None = None,
    tools: list[dict] | None = None,
) -> bool:
    return estimate_request_tokens(messages, system=system, tools=tools) >= max(1, threshold)


def compacted_context_message(summary_text: str) -> dict[str, str]:
    return {
        "role": "developer",
        "content": (
            "<compacted_context kind=\"background_summary\">\n"
            "Boundary:\n"
            "- This is historical background, not the latest user instruction.\n"
            "- Project truth lives in runtime state and tools, not in this summary.\n"
            "- The next user message after this block is the active task.\n\n"
            "Summary:\n"
            f"{summary_text.strip()}\n"
            "</compacted_context>"
        ),
    }


def compaction_checkpoint_message(implementation: str) -> dict[str, str]:
    return compacted_context_message(
        "Earlier model-visible history was replaced by a Codex-style context checkpoint "
        f"({implementation}). The canonical checkpoint is stored as typed Responses input."
    )


def _tool_call_ids(message: dict) -> list[str]:
    if message.get("type") == "function_call":
        call_id = message.get("call_id") or message.get("id")
        return [str(call_id)] if call_id else []
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    return [
        str(tool_call.get("id"))
        for tool_call in tool_calls
        if isinstance(tool_call, dict) and tool_call.get("id")
    ]


def _tool_result_id(message: dict) -> str | None:
    if message.get("type") == "function_call_output":
        call_id = message.get("call_id")
        return str(call_id) if call_id else None
    if message.get("role") != "tool":
        return None
    call_id = message.get("tool_call_id")
    return str(call_id) if call_id else None


def _message_role(message: dict) -> str:
    item_type = message.get("type")
    if item_type == "function_call":
        return "assistant"
    if item_type == "function_call_output":
        return "tool"
    if item_type == "reasoning":
        return "assistant"
    return str(message.get("role") or "")


def _message_text(message: dict) -> str:
    item_type = message.get("type")
    if item_type == "function_call_output":
        return str(message.get("output") or "")
    return message_text_for_compare(message.get("content"))


def _is_runtime_wrapper_message(message: dict) -> bool:
    if message.get("_tool_image_context") or message.get("_persisted_vision_context"):
        return True
    stripped = _message_text(message).lstrip()
    return stripped.startswith((
        CODEX_SUMMARY_PREFIX,
        "<system-reminder>",
        "<agent-instructions>",
        "<compacted_context",
        "<execution-checklist>",
        "<runtime-context>",
        "<skills_instructions>",
        "<skill>",
        "<skill-warning>",
    ))


def compact_preserved_tail(
    messages: list[dict],
    *,
    token_budget: int = PRESERVED_TAIL_TOKEN_BUDGET,
    exclude_latest_user_content: str | None = None,
) -> list[dict]:
    if token_budget <= 0:
        return []
    excluded_user_index: int | None = None
    if exclude_latest_user_content is not None:
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if _message_role(message) == "user" and message_text_for_compare(
                message.get("content")
            ).startswith(exclude_latest_user_content):
                excluded_user_index = index
                break

    call_to_assistant_index: dict[str, int] = {}
    for index, message in enumerate(messages):
        if _message_role(message) == "assistant":
            for call_id in _tool_call_ids(message):
                call_to_assistant_index[call_id] = index

    candidate_indices = [
        index
        for index, message in enumerate(messages)
        if index != excluded_user_index
        and _message_role(message) in {"user", "assistant", "tool"}
        and not _is_runtime_wrapper_message(message)
    ]
    selected: list[int] = []
    used_tokens = 0
    for index in reversed(candidate_indices):
        message_tokens = _estimate_text_tokens([messages[index]])
        if used_tokens + message_tokens > token_budget:
            break
        selected.append(index)
        used_tokens += message_tokens
    if not selected:
        return []

    start = min(selected)
    changed = True
    while changed:
        changed = False
        for index in candidate_indices:
            if index < start:
                continue
            call_id = _tool_result_id(messages[index])
            assistant_index = call_to_assistant_index.get(call_id or "")
            if assistant_index is not None and assistant_index < start and assistant_index in candidate_indices:
                start = assistant_index
                changed = True

    tail = [deepcopy(messages[index]) for index in candidate_indices if index >= start]
    included_results = {
        call_id for message in tail if (call_id := _tool_result_id(message))
    }
    complete: list[dict] = []
    dropped_calls: set[str] = set()
    for message in tail:
        if _message_role(message) == "assistant":
            call_ids = _tool_call_ids(message)
            if call_ids and not all(call_id in included_results for call_id in call_ids):
                dropped_calls.update(call_ids)
                continue
        if _message_role(message) == "tool" and _tool_result_id(message) in dropped_calls:
            continue
        complete.append(message)
    return complete


def _real_user_message(message: dict) -> bool:
    return _message_role(message) == "user" and not _is_runtime_wrapper_message(message)


def _truncate_user_message(message: dict, token_budget: int) -> dict | None:
    if token_budget <= 0:
        return None
    item = deepcopy(message)
    content = item.get("content")
    if isinstance(content, str):
        item["content"] = truncate_text_middle(content, token_budget)
        return item
    if not isinstance(content, list):
        return None

    kept: list[dict] = []
    remaining = token_budget
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type") or "")
        if part_type in {"image_url", "input_image"}:
            if remaining >= image_token_estimate():
                kept.append(deepcopy(part))
                remaining -= image_token_estimate()
            continue
        text = str(part.get("text") or part.get("content") or "")
        if not text or remaining <= 0:
            continue
        next_part = deepcopy(part)
        if estimate_text_tokens(text) > remaining:
            text = truncate_text_middle(text, remaining)
        if "text" in next_part or part_type in {"text", "input_text", "output_text"}:
            next_part["text"] = text
        else:
            next_part["content"] = text
        kept.append(next_part)
        remaining -= min(remaining, estimate_text_tokens(text))
    if not kept:
        return None
    item["content"] = kept
    return item


def codex_local_compacted_history(
    messages: list[dict],
    summary_text: str,
    *,
    recent_user_token_budget: int = CODEX_RECENT_USER_TOKEN_BUDGET,
) -> list[dict]:
    """Build the same local-compaction shape as Codex.

    Only recent real user messages survive verbatim. Assistant/reasoning/tool
    items are represented by the checkpoint summary, which is itself a final
    user message prefixed with Codex's canonical handoff text.
    """

    selected_reversed: list[dict] = []
    remaining = max(0, int(recent_user_token_budget))
    for message in reversed(messages):
        if not _real_user_message(message):
            continue
        message_tokens = estimate_tokens([message])
        if message_tokens <= remaining:
            selected_reversed.append(deepcopy(message))
            remaining -= message_tokens
            continue
        partial = _truncate_user_message(message, remaining)
        if partial is not None:
            selected_reversed.append(partial)
        break

    selected = list(reversed(selected_reversed))
    selected.append({
        "role": "user",
        "content": f"{CODEX_SUMMARY_PREFIX}{str(summary_text or '').strip()}",
    })
    return selected


def sanitize_remote_compaction_items(items: list[dict]) -> list[dict]:
    """Keep the provider's canonical compact output minus stale prompt wrappers."""

    sanitized: list[dict] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item = deepcopy(raw)
        if item.get("type") == "compaction":
            sanitized.append(item)
            continue
        if item.get("type") in {
            "reasoning",
            "function_call",
            "function_call_output",
            "computer_call_output",
            "item_reference",
        }:
            continue
        role = _message_role(item)
        if role in {"system", "developer"}:
            continue
        if role == "user" and _is_runtime_wrapper_message(item):
            continue
        if role in {"user", "assistant"}:
            sanitized.append(item)
    return sanitized


def ensure_latest_user_message(
    compacted_items: list[dict],
    source_messages: list[dict],
) -> list[dict]:
    latest = next(
        (deepcopy(message) for message in reversed(source_messages) if _real_user_message(message)),
        None,
    )
    if latest is None:
        return list(compacted_items)
    latest_text = _message_text(latest).strip()
    if any(
        _real_user_message(item) and _message_text(item).strip() == latest_text
        for item in compacted_items
    ):
        return list(compacted_items)
    result = list(compacted_items)
    insert_at = next(
        (index for index, item in enumerate(result) if item.get("type") == "compaction"),
        len(result),
    )
    result.insert(insert_at, latest)
    return result


def remove_oldest_compaction_unit(messages: list[dict]) -> list[dict]:
    """Drop the oldest item and any paired function call/output for retry."""

    if len(messages) <= 1:
        return []
    result = [deepcopy(message) for message in messages]
    removed = result.pop(0)
    paired_ids = set(_tool_call_ids(removed))
    result_id = _tool_result_id(removed)
    if result_id:
        paired_ids.add(result_id)
    if paired_ids:
        result = [
            item
            for item in result
            if not paired_ids.intersection(_tool_call_ids(item))
            and _tool_result_id(item) not in paired_ids
        ]
    return result


def trim_function_outputs_for_compaction(
    messages: list[dict],
    *,
    max_input_tokens: int | None,
    system: str | None = None,
) -> tuple[list[dict], int, int]:
    """Shrink newest tool outputs until a compaction request fits its window."""

    try:
        limit = int(max_input_tokens or 0)
    except (TypeError, ValueError):
        limit = 0
    result = [deepcopy(message) for message in messages]
    before = estimate_request_tokens(result, system=system)
    if limit <= 0 or before <= limit:
        return result, 0, 0

    rewritten = 0
    marker = "Output exceeded the available model context and was truncated"
    for index in range(len(result) - 1, -1, -1):
        message = result[index]
        if message.get("type") == "function_call_output":
            if str(message.get("output") or "") == marker:
                continue
            message["output"] = marker
        elif _message_role(message) == "tool":
            if str(message.get("content") or "") == marker:
                continue
            message["content"] = marker
        else:
            continue
        rewritten += 1
        if estimate_request_tokens(result, system=system) <= limit:
            break
    after = estimate_request_tokens(result, system=system)
    return result, rewritten, max(0, before - after)


def count_input_images(messages: list[dict]) -> int:
    count = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        count += sum(
            1
            for part in content
            if isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}
        )
    return count


def db_safe_compaction_items(items: list[dict]) -> list[dict]:
    """Remove hydrated image bytes while preserving valid typed input items."""

    safe_items: list[dict] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item = deepcopy(raw)
        content = item.get("content")
        if isinstance(content, list):
            safe_content: list[Any] = []
            omitted_images = 0
            for part in content:
                if not isinstance(part, dict):
                    safe_content.append(part)
                    continue
                part_type = str(part.get("type") or "")
                raw_url = part.get("image_url") or part.get("url")
                if isinstance(raw_url, dict):
                    raw_url = raw_url.get("url")
                if (
                    part_type in {"image_url", "input_image"}
                    and isinstance(raw_url, str)
                    and raw_url.startswith("data:image/")
                ):
                    omitted_images += 1
                    continue
                safe_content.append(redact_image_data_urls(part))
            if omitted_images:
                marker_type = "input_text" if _message_role(item) == "user" else "output_text"
                safe_content.append({
                    "type": marker_type,
                    "text": f"[{omitted_images} image input(s) rehydrate from checkpoint metadata]",
                })
            item["content"] = safe_content
        safe_items.append(redact_image_data_urls(item))
    return safe_items
