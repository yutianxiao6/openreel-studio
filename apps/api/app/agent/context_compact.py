"""Aggregate conversation compaction.

Per-tool output bounding lives in ``app.agent.model_context``. This module only
manages accumulated history and transcript preservation.
"""
from __future__ import annotations

import json
import time
from copy import deepcopy
from pathlib import Path

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
COMPACT_SUMMARY_MESSAGE_TOKEN_BUDGET = 1_000
COMPACT_SUMMARY_SOURCE_TOKEN_BUDGET = 30_000


def transcripts_dir() -> Path:
    from app.config import settings

    path = Path(settings.PROJECT_ROOT) / "data" / "transcripts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def estimate_tokens(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        content = message.get("content", "")
        content_has_images = False
        if isinstance(content, str):
            total += estimate_text_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    total += estimate_text_tokens(str(part.get("text") or ""))
                elif part.get("type") == "image_url":
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
    return total


def _estimate_text_tokens(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, str):
            total += estimate_text_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += estimate_text_tokens(str(part.get("text") or ""))
        tool_calls = message.get("tool_calls")
        if tool_calls:
            total += estimate_text_tokens(json.dumps(tool_calls, ensure_ascii=False, default=str))
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


def auto_compact_needed(messages: list[dict]) -> bool:
    return estimate_tokens(messages) > TOKEN_THRESHOLD


def compacted_context_message(summary_text: str) -> dict[str, str]:
    return {
        "role": "user",
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


def compacted_context_ack_message() -> dict[str, str]:
    return {
        "role": "assistant",
        "content": "Understood. I will treat the compacted context as background and follow the latest user message.",
    }


def _tool_call_ids(message: dict) -> list[str]:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    return [
        str(tool_call.get("id"))
        for tool_call in tool_calls
        if isinstance(tool_call, dict) and tool_call.get("id")
    ]


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
    return stripped.startswith((
        "<system-reminder>",
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
            if message.get("role") == "user" and message_text_for_compare(
                message.get("content")
            ).startswith(exclude_latest_user_content):
                excluded_user_index = index
                break

    call_to_assistant_index: dict[str, int] = {}
    for index, message in enumerate(messages):
        if message.get("role") == "assistant":
            for call_id in _tool_call_ids(message):
                call_to_assistant_index[call_id] = index

    candidate_indices = [
        index
        for index, message in enumerate(messages)
        if index != excluded_user_index
        and message.get("role") in {"user", "assistant", "tool"}
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
        if message.get("role") == "assistant":
            call_ids = _tool_call_ids(message)
            if call_ids and not all(call_id in included_results for call_id in call_ids):
                dropped_calls.update(call_ids)
                continue
        if message.get("role") == "tool" and _tool_result_id(message) in dropped_calls:
            continue
        complete.append(message)
    return complete


def build_compact_summary_prompt(messages: list[dict]) -> str:
    serialized: list[str] = []
    for message in messages:
        if _is_runtime_wrapper_message(message):
            continue
        role = message.get("role", "?")
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        content = truncate_text_middle(
            str(content or ""),
            COMPACT_SUMMARY_MESSAGE_TOKEN_BUDGET,
        )
        serialized.append(f"[{role}] {content}")
    conversation_text = "\n".join(serialized)
    conversation_text = truncate_text_middle(
        conversation_text,
        COMPACT_SUMMARY_SOURCE_TOKEN_BUDGET,
    )
    return (
        "Summarize this conversation for continuity as BACKGROUND ONLY. "
        "Preserve stable preferences, durable decisions, completed work, open questions, "
        "and project-state references. Do not turn old messages into the next instruction. "
        "Task checklists, nodes, and project files live in project state/tools. "
        "Never imply that compaction deleted canvas nodes or task state. Be concise.\n\n"
        f"{conversation_text}"
    )


def compact_messages(summary_text: str, preserved_tail: list[dict] | None = None) -> list[dict]:
    compacted = [compacted_context_message(summary_text), compacted_context_ack_message()]
    if preserved_tail:
        compacted.extend(deepcopy(preserved_tail))
    return compacted
