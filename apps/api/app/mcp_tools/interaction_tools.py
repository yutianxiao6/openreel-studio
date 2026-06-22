"""User interaction tools.

These tools do not create project artifacts. They let the agent ask the user
up to six short questions while the frontend renders a reusable card.
"""
from __future__ import annotations

import json
from typing import Any


MAX_QUESTIONS = 6
MAX_OPTIONS = 3


def _clean_text(value: Any, *, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _normalize_options(raw_options: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_options, list):
        return []
    options: list[dict[str, Any]] = []
    for option in raw_options[:MAX_OPTIONS]:
        if not isinstance(option, dict):
            continue
        label = _clean_text(option.get("label"))
        description = _clean_text(option.get("description"))
        if not label:
            continue
        normalized: dict[str, Any] = {
            "label": label,
        }
        if description:
            normalized["description"] = description[:240]
        options.append(normalized)
    return options


def _normalize_questions(raw_questions: Any) -> tuple[list[dict[str, Any]], str | None]:
    if raw_questions in (None, "", []):
        return [], "questions is required"
    if not isinstance(raw_questions, list):
        return [], "questions must be an array"
    if not raw_questions:
        return [], "questions must contain at least one item"
    if len(raw_questions) > MAX_QUESTIONS:
        return [], f"questions may contain at most {MAX_QUESTIONS} items"

    questions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_question in enumerate(raw_questions, start=1):
        if not isinstance(raw_question, dict):
            return [], "question must be an object"
        question_id = _clean_text(raw_question.get("id"), default=f"question_{index}")
        question_id = question_id.lower().replace("-", "_").replace(" ", "_")
        if not question_id or question_id in seen:
            return [], "question.id must be unique"
        header = _clean_text(raw_question.get("header"))
        if not header:
            return [], f"question {question_id}: header is required"
        question_text = _clean_text(raw_question.get("question") or raw_question.get("label"))
        if not question_text:
            return [], f"question {question_id}: question is required"
        options = _normalize_options(raw_question.get("options"))
        if isinstance(raw_question.get("options"), list) and options and not (2 <= len(options) <= 3):
            return [], f"question {question_id}: options must contain 2-3 valid choices when provided"
        question: dict[str, Any] = {
            "id": question_id,
            "header": header[:80],
            "question": question_text,
            "options": options,
        }
        questions.append(question)
        seen.add(question_id)
    return questions, None


async def _read_project_state(project_id: str) -> dict[str, Any]:
    try:
        from app.db.models import Project
        from app.db.session import session_scope

        async with session_scope() as session:
            project = await session.get(Project, project_id)
            if project is None:
                return {}
            payload = json.loads(project.state_json or "{}")
            return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


async def _filter_video_intake_collected_questions(
    project_id: str,
    questions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    from app.agent.video_intake import (
        canonical_video_intake_field_id,
        collected_video_intake_facts_from_state,
    )

    facts = collected_video_intake_facts_from_state(await _read_project_state(project_id))
    if not facts:
        return questions, {}, []

    filtered: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    for question in questions:
        canonical = canonical_video_intake_field_id(
            question.get("id"),
            question.get("header") or question.get("question"),
        )
        if canonical and canonical in facts:
            omitted.append({
                "id": question.get("id"),
                "header": question.get("header"),
                "question": question.get("question"),
                "fact": canonical,
                "value": facts.get(canonical),
            })
            continue
        filtered.append(question)
    return filtered, facts, omitted


async def request_input(
    project_id: str,
    questions: list[dict[str, Any]] | None = None,
    purpose: str = "general",
    stage: str = "general",
    title: str = "",
    description: str = "",
    submit_label: str = "提交",
    summary_text: str = "",
    assistant_text: str = "",
) -> dict[str, Any]:
    """Ask the user for up to six short questions.

    Boundaries:
    - Can request one to six user decisions in one card.
    - Cannot create, update, delete, reset, approve, run, or mutate project artifacts.
    - Cannot be used as a hidden router; it only returns a UI payload and stops
      the current agent turn until the user submits values.
    """
    if not project_id:
        return {"ok": False, "error": "project_id is required", "error_kind": "missing_project_id"}
    normalized_questions, question_error = _normalize_questions(questions)
    if question_error:
        return {"ok": False, "error": question_error, "error_kind": "invalid_questions"}

    title = _clean_text(
        title,
        default=(
            normalized_questions[0].get("header")
            or normalized_questions[0].get("question")
        ),
    )

    clean_purpose = _clean_text(purpose, default="general")
    collected_facts: dict[str, Any] = {}
    omitted_collected_questions: list[dict[str, Any]] = []
    if clean_purpose == "video_blueprint_intake":
        normalized_questions, collected_facts, omitted_collected_questions = await _filter_video_intake_collected_questions(
            project_id,
            normalized_questions,
        )
        if not normalized_questions:
            return {
                "ok": False,
                "status": "already_collected",
                "error": "这些问题已经由用户确认；请直接使用 collected_facts 继续规划，或只询问缺失信息。",
                "error_kind": "intake_questions_already_collected",
                "collected_facts": collected_facts,
                "collected_questions": omitted_collected_questions,
                "hint": "不要重复询问已确认问题；继续构建蓝图草稿或提出新的缺失问题。",
            }
    intake = {
        "purpose": clean_purpose,
        "stage": _clean_text(stage, default="general"),
        "title": title,
        "description": _clean_text(description),
        "submit_label": _clean_text(submit_label, default="提交"),
        "questions": normalized_questions,
    }
    if collected_facts:
        intake["collected_facts"] = collected_facts
    if omitted_collected_questions:
        intake["omitted_collected_questions"] = omitted_collected_questions
    clean_summary = _clean_text(summary_text, default=title)
    clean_assistant_text = _clean_text(assistant_text, default=clean_summary)
    return {
        "ok": True,
        "status": "awaiting_user",
        "summary_text": clean_summary,
        "assistant_text": clean_assistant_text,
        "intake": intake,
        "event": {
            "type": "interaction_input_requested",
            "project_id": project_id,
            "status": "awaiting_user",
            "summary_text": clean_summary,
            "intake": intake,
        },
    }
