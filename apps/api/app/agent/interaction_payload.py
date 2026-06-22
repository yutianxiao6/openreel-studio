"""Typed model-facing payloads for submitted interaction cards."""
from __future__ import annotations

import json
from typing import Any


MAX_OPTIONS = 12


def _short_text(value: Any, limit: int = 600) -> str:
    if isinstance(value, (list, tuple, set)):
        text = "、".join(str(item) for item in value if item not in (None, "", [], {}))
    elif isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value or "")
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def is_interaction_input(decision_inputs: Any) -> bool:
    return isinstance(decision_inputs, dict) and decision_inputs.get("kind") == "interaction_input"


def _field_options(field: dict[str, Any]) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for option in field.get("options") or []:
        if not isinstance(option, dict):
            continue
        label = _short_text(option.get("label"), 160)
        description = _short_text(option.get("description"), 240)
        if not label:
            continue
        item = {"label": label}
        if description:
            item["description"] = description
        options.append(item)
        if len(options) >= MAX_OPTIONS:
            break
    return options


def _payload_questions(decision_inputs: dict[str, Any]) -> list[dict[str, Any]]:
    raw_questions = decision_inputs.get("questions")
    if not isinstance(raw_questions, list):
        return []
    questions: list[dict[str, Any]] = []
    for raw_question in raw_questions:
        if not isinstance(raw_question, dict):
            continue
        question_id = _short_text(raw_question.get("id"), 80)
        question_text = _short_text(raw_question.get("question"), 240)
        if not question_id or not question_text:
            continue
        question: dict[str, Any] = {
            "id": question_id,
            "question": question_text,
        }
        header = _short_text(raw_question.get("header"), 80)
        if header:
            question["header"] = header
        options = _field_options(raw_question)
        if options:
            question["options"] = options
        questions.append(question)
        if len(questions) >= 3:
            break
    return questions


def interaction_agent_payload(
    decision_inputs: dict[str, Any],
    *,
    user_visible_message: str = "",
) -> dict[str, Any]:
    """Build the compact JSON payload shown to the model for an interaction submit."""
    values = decision_inputs.get("values") if isinstance(decision_inputs.get("values"), dict) else {}
    payload: dict[str, Any] = {
        "event": "interaction_input_submitted",
        "kind": "interaction_input",
        "target": _short_text(decision_inputs.get("target") or decision_inputs.get("purpose") or "", 120),
        "action": _short_text(decision_inputs.get("action") or "submit", 80),
        "purpose": _short_text(decision_inputs.get("purpose") or decision_inputs.get("target") or "", 120),
        "stage": _short_text(decision_inputs.get("stage"), 80),
        "title": _short_text(decision_inputs.get("title"), 160),
        "description": _short_text(decision_inputs.get("description"), 300),
        "values": {
            str(key): _short_text(value)
            for key, value in values.items()
            if str(key).strip() and value not in (None, "", [], {})
        },
    }
    questions = _payload_questions(decision_inputs)
    if questions:
        payload["questions"] = questions
    if user_visible_message:
        payload["user_visible_message"] = _short_text(user_visible_message, 1000)

    return payload


def build_interaction_agent_message(
    user_visible_message: str,
    decision_inputs: dict[str, Any] | None,
) -> str:
    if not is_interaction_input(decision_inputs):
        return user_visible_message
    payload = interaction_agent_payload(
        decision_inputs,
        user_visible_message=user_visible_message,
    )
    return (
        "<interaction-input-json>\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n</interaction-input-json>"
    )
