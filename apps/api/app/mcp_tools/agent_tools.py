"""Sub-agent helpers + high-level collaboration tools.

A sub-agent is a short-lived autonomous loop:
  1. high-level collaboration wrappers call `subagent_run(role, task, ...)`
  2. sub-agent gets a scoped system prompt + a tool whitelist
  3. it picks ONE tool per turn (or `finish`), in a small JSON protocol
  4. result + summary + tool_log come back to the caller

Low-level subagent helpers are direct Python helpers, not registry tools.
Registry exposure is limited to high-level collaboration wrappers.

Design notes:
  - sub-agents are read-only reviewers/debuggers by default. They never write,
    execute media, approve plans, reset projects, or mutate nodes.
  - sub-agents never touch the DB / network directly — only via
    `registry.call(...)` for whitelisted read tools.
  - `project_id` is auto-injected into every tool call so a sub-agent can't
    leak across projects.
  - tool errors are NOT raised — they are written back into the transcript
    so the sub-agent can see "that didn't work" and pick a different path.
  - tool outputs are truncated to ~2000 chars before being fed back, so the
    sub-agent's context doesn't explode.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from app.agent import context_compact
from app.config import settings
from app.db.models import Project
from app.db.session import session_scope
from app.services.llm_service import LLMService


# ── Role presets ────────────────────────────────────────────────────────

READONLY_FORBIDDEN_TAGS: set[str] = {
    "write",
    "destructive",
    "execute",
    "mode",
    "export",
    "protocol",
    "autonomous",
    "isolation",
    "control",
    "provider",
}

READONLY_TOOL_ALLOWLIST: set[str] = {
    "project.get_state",
    "node.list",
    "node.get",
    "task.list",
    "memory.recall",
    "memory.recall_user",
    "assets.get_library_path",
    "assets.list_project",
    "assets.list_shared",
    "assets.read_asset",
    "file.list_dir",
    "file.read_text",
    "file.extract_text_from_upload",
    "system.status",
    "system.models",
    "config.read",
    "config.read_file",
    "config.validate",
    "events.tail",
    "events.query",
    "feature.list",
    "feature.is_enabled",
    "skill.project_mentor",
    "skill.video_production",
    "tool.search",
    "tool.describe",
}

READONLY_ROLE_NAMES: set[str] = {
    "researcher",
    "reviewer",
    "debugger",
    "media_prompt_reviewer",
    "project_mentor",
    "default",
}

ROLE_PRESETS: dict[str, dict[str, Any]] = {
    "researcher": {
        "description": "只读调研:看项目状态、画布、记忆、文件,不写任何东西",
        "system": (
            "你是 researcher 子 Agent,任务是**只读**调研。你只能调用读类工具"
            "(project.get_state / node.list / node.get / memory.recall 等),"
            "调研完成后用 finish 把答案交回。不要尝试生成、修改、运行或删除任何内容。"
        ),
        "allowed_tools": [
            "project.get_state",
            "node.list",
            "node.get",
            "assets.list_project",
            "assets.list_shared",
            "assets.read_asset",
            "memory.recall",
            "memory.recall_user",
            "file.list_dir",
            "file.read_text",
        ],
    },
    "reviewer": {
        "description": "通用只读审查:检查主 Agent 指定的目标、状态、节点、计划、文档或流程风险",
        "system": (
            "你是 reviewer 子 Agent,任务是**只读审查**。"
            "只能读取项目状态、任务、节点、指南、文件或记忆,根据主 Agent 给定的审查目标给出问题、风险和建议。"
            "审查范围可以是节点图、视频流程、提示词、工具选择、trace 摘要、前端问题、配置或其他工程事项。"
            "审查必须以用户当前明确需求、主 Agent 提供的证据和真实项目状态为准；"
            "同时检查主 Agent 的工具选择、修复范围和工作摘要是否偏离用户当前需求或指定 skill；"
            "只指出有具体证据的违反项、遗漏项、冲突项或不可执行项。"
            "不要用个人偏好的剧情、风格、制作路径或指南示例替换已经满足用户需求的方案；"
            "没有证据证明错误时返回 pass 或 low severity 建议。"
            "如果输入 evidence.current_canvas_graph.available=true,优先直接审查该证据包；只有证据缺失或需要核实时才调用工具。"
            "审查制作方案时，以 project.get_state、node.list、node.get 和明确 evidence 为准。"
            "如果 evidence 已包含待审节点 prompt/fields/references 和检查清单，先直接 finish 给结论，"
            "只有证据缺失或矛盾时才继续调工具。"
            "禁止创建、修改、删除、运行节点,也禁止调用 drama/media 生成工具。"
        ),
        "allowed_tools": [
            "project.get_state",
            "task.list",
            "node.list",
            "node.get",
            "skill.project_mentor",
            "skill.video_production",
            "file.read_text",
            "memory.recall",
        ],
    },
    "debugger": {
        "description": "只读排障:查看失败节点、trace、事件和项目状态",
        "system": (
            "你是 debugger 子 Agent,任务是**只读排障**。"
            "读取项目状态、失败节点、事件或 trace 线索,给出最小修复建议。"
            "禁止运行、删除、重置、修改节点或项目。"
        ),
        "allowed_tools": [
            "project.get_state",
            "node.list",
            "node.get",
            "events.tail",
            "events.query",
            "memory.recall",
        ],
    },
    "media_prompt_reviewer": {
        "description": "只读媒体提示词审查:检查 prompt 和 reference_images 一致性",
        "system": (
            "你是 media_prompt_reviewer 子 Agent,任务是**只读检查媒体提示词**。"
            "重点看人物/场景/分镜/首尾帧/视频提示词是否一致,reference_images 是否合理。"
            "只给修改建议,禁止调用任何 media/drama 生成工具或 node.run。"
        ),
        "allowed_tools": [
            "project.get_state",
            "node.list",
            "node.get",
            "assets.list_project",
            "assets.read_asset",
            "memory.recall",
        ],
    },
    "project_mentor": {
        "description": "只读项目导师:解释架构、规则、文档入口和下一步顺序",
        "system": (
            "你是 project_mentor 子 Agent,任务是解释项目规则和给出下一步建议。"
            "优先使用 skill.project_mentor,必要时读取项目状态。禁止改项目。"
        ),
        "allowed_tools": [
            "skill.project_mentor",
            "project.get_state",
            "memory.recall",
        ],
    },
    "default": {
        "description": "纯文本回答(不调工具)",
        "system": (
            "你是一个只读子 Agent。不调任何工具,直接基于 task 和 inputs 用一句中文回答,然后 finish。"
        ),
        "allowed_tools": [],
    },
}

DEFAULT_MAX_STEPS = 4
DEFAULT_MAX_CONCURRENCY = 3
TOOL_RESULT_TRUNCATE = 2000
REVIEW_RESULT_SCHEMA_VERSION = "agent_review_result_v1"
REVIEW_PARSE_OK_STATUSES = {"parsed", "repaired"}
REVIEW_SESSION_OK_STATUS = "completed"
REVIEW_TIMEOUT_MAX_SECONDS = 480.0
_REVIEW_SKILL_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,80}$")
_REVIEW_STATUSES = {"pass", "revise_required", "blocked"}
_REVIEW_STATUS_ALIASES = {
    "ok": "pass",
    "passed": "pass",
    "approved": "pass",
    "success": "pass",
    "needs_revision": "revise_required",
    "needs_revise": "revise_required",
    "revision_required": "revise_required",
    "revise": "revise_required",
    "failed": "blocked",
    "error": "blocked",
    "failure": "blocked",
}
_REVIEW_SEVERITIES = {"low", "medium", "high", "blocking"}


def _review_skill_dir() -> Path:
    skills_root = Path(os.environ.get("OPENREEL_SKILLS_DIR") or Path(settings.PROJECT_ROOT) / "skills")
    path = skills_root / "review"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _project_relative_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path(settings.PROJECT_ROOT)))
    except ValueError:
        return str(path)


def _normalize_review_skill_key(value: Any) -> str:
    key = str(value or "").strip().lower().replace(" ", "_")
    return key if _REVIEW_SKILL_KEY_RE.match(key) else ""


def _read_review_skill(key: str) -> dict[str, Any]:
    normalized = _normalize_review_skill_key(key)
    if not normalized:
        return {"ok": False, "error": "invalid_review_skill_key", "key": key}
    path = (_review_skill_dir() / f"{normalized}.md").resolve()
    base = _review_skill_dir().resolve()
    if base not in path.parents or path.suffix.lower() != ".md":
        return {"ok": False, "error": "invalid_review_skill_path", "key": normalized}
    if not path.exists():
        return {"ok": False, "error": "review_skill_not_found", "key": normalized}
    content = path.read_text(encoding="utf-8", errors="replace")
    return {
        "ok": True,
        "key": normalized,
        "path": _project_relative_path(path),
        "content": content[:8000],
        "chars": len(content),
    }


def _read_app_skill(key: str) -> dict[str, Any]:
    normalized = _normalize_review_skill_key(key)
    if not normalized:
        return {"ok": False, "error": "invalid_app_skill_key", "key": key}
    candidates = [
        Path(settings.PROJECT_ROOT) / "apps" / "api" / "app" / "skills" / normalized / "SKILL.md",
        Path(__file__).resolve().parents[1] / "skills" / normalized / "SKILL.md",
    ]
    for path in candidates:
        if not path.exists() or not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        try:
            rel_path = str(path.relative_to(Path(settings.PROJECT_ROOT)))
        except ValueError:
            rel_path = str(path)
        return {
            "ok": True,
            "key": normalized,
            "path": rel_path,
            "source": "app_skill",
            "content": content[:8000],
            "chars": len(content),
        }
    return {"ok": False, "error": "app_skill_not_found", "key": normalized}


def _load_review_skill_by_key(key: str) -> dict[str, Any]:
    custom = _read_review_skill(key)
    if custom.get("ok"):
        return custom
    app_skill = _read_app_skill(key)
    if app_skill.get("ok"):
        return app_skill
    return custom


def _coerce_review_skill_arg(review_skill: dict | str | None, review_skill_key: str) -> dict[str, Any] | str:
    if isinstance(review_skill, dict):
        return review_skill
    raw = str(review_skill or "").strip()
    key = _normalize_review_skill_key(review_skill_key or raw)
    if key:
        loaded = _load_review_skill_by_key(key)
        if loaded.get("ok"):
            return loaded
    return raw if raw else {}


def _review_profile_block(inputs: dict | None) -> str:
    if not isinstance(inputs, dict):
        return ""
    profile = str(inputs.get("review_profile") or "general").strip()[:120] or "general"
    checklist_items: list[str] = []
    custom_checklist = inputs.get("custom_checklist")
    if isinstance(custom_checklist, list):
        for item in custom_checklist[:20]:
            text = str(item or "").strip()
            if text:
                checklist_items.append(text)
    review_skill = inputs.get("review_skill")
    skill_lines: list[str] = []
    if isinstance(review_skill, dict):
        for key_name in ("key", "name", "path", "summary", "when_to_use", "priority", "rules", "content"):
            value = review_skill.get(key_name)
            if value in (None, "", [], {}):
                continue
            rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value)
            skill_lines.append(f"- {key_name}: {rendered[:1200]}")
    elif isinstance(review_skill, str) and review_skill.strip():
        skill_lines.append("- content: " + review_skill.strip()[:2400])
    checklist = "\n".join(f"- {item}" for item in checklist_items) or "- 本轮没有传入自定义检查项；按审查目标、证据包、focus 和真实项目状态做通用只读审查。"
    skill_block = ""
    if skill_lines:
        skill_block = (
            "\n\n自定义审查 skill 是本轮主要检查标准；若与通用审查目标冲突，以用户当前明确指定的 skill 为准。"
            "\n自定义 skill 摘要:\n"
            + "\n".join(skill_lines)
        )
    return (
        "\n\n## 审查 profile\n"
        + f"profile: {profile}\n"
        + "自定义检查项:\n"
        + checklist
        + skill_block
        + "\n\n## 输出要求\n"
        + "finish.result 必须使用对象结构: "
        + "{status, passed, score, safe_to_submit, safe_to_run, findings, missing_evidence, suggested_next}。"
        + "status 只能是 pass / revise_required / blocked；缺失或非法会被后端视为 blocked。"
        + "findings 每项包含 severity、issue、evidence、suggested_fix、violated_requirement。"
        + "只有具体证据能证明违反用户需求、节点事实、依赖关系或执行条件时,才使用 medium/high/blocking。"
        + "偏好型优化、替代创意和指南示例差异只能作为 low severity 建议,不能要求重写正确方案。"
        + "证据不足时 status='blocked'、safe_to_submit=false，并写 missing_evidence。"
    )


# ── Internals ───────────────────────────────────────────────────────────


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines)
    return text.strip()


def _parse_json_action(raw: str) -> dict | None:
    try:
        obj = json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict) and "action" in obj:
        return obj
    return None


def _iter_blueprint_nodes(node: dict[str, Any] | None, parent_id: str | None = None):
    if not isinstance(node, dict):
        return
    yield node, parent_id
    for child in node.get("children") or []:
        if isinstance(child, dict):
            yield from _iter_blueprint_nodes(child, str(node.get("id") or "root"))


def _compact_blueprint_text(value: Any, limit: int = 260) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"


def _current_blueprint_review_evidence(project_id: str) -> dict[str, Any]:
    """Summarize draft/pending/active blueprint state for read-only reviews.

    `agent.review` often runs before blueprint approval, when workflow nodes are
    expected to be empty. Supplying the semantic tree prevents the reviewer from
    mistaking a valid pre-approval draft for missing project content.
    """
    if not project_id:
        return {"available": False, "reason": "missing_project_id"}
    try:
        from app.agent.blueprint_tree import blueprint_root, read_blueprint

        doc = read_blueprint(project_id)
        path = blueprint_root(project_id)
    except Exception as exc:
        return {"available": False, "reason": f"read_failed:{type(exc).__name__}"}

    root = doc.get("root") if isinstance(doc.get("root"), dict) else {}
    nodes: list[dict[str, Any]] = []
    for node, parent_id in _iter_blueprint_nodes(root):
        fields = node.get("fields") if isinstance(node.get("fields"), dict) else {}
        prompt = str(node.get("prompt") or fields.get("prompt") or "").strip()
        is_media = node.get("type") in {"image", "video", "audio"}
        nodes.append({
            "id": node.get("id"),
            "type": node.get("type"),
            "title": node.get("title"),
            "parent_id": parent_id,
            "materialize": node.get("materialize"),
            "prompt_len": len(prompt),
            "prompt_preview": _compact_blueprint_text(prompt, 1400 if is_media else 420),
            "prompt_source": fields.get("prompt_source"),
            "prompt_template": fields.get("prompt_template"),
            "template_selection_reason": _compact_blueprint_text(fields.get("template_selection_reason"), 180),
            "references": node.get("references") or fields.get("references") or [],
            "depends_on": node.get("depends_on") or fields.get("depends_on") or [],
            "content_preview": _compact_blueprint_text(
                node.get("content") or node.get("description") or fields.get("content") or fields.get("description"),
            ),
        })

    media_nodes = [node for node in nodes if node.get("type") in {"image", "video", "audio"}]
    checksum = ""
    try:
        checksum = uuid.uuid5(
            uuid.NAMESPACE_URL,
            json.dumps(root, ensure_ascii=False, sort_keys=True, default=str),
        ).hex[:16]
    except Exception:
        checksum = ""
    return {
        "available": True,
        "file_path": str(path),
        "status": doc.get("status"),
        "title": doc.get("title") or root.get("title"),
        "summary": _compact_blueprint_text(doc.get("summary") or root.get("content"), 500),
        "tree_version": doc.get("tree_version"),
        "checksum": checksum,
        "node_count": max(0, len(nodes) - 1),
        "media_node_count": len(media_nodes),
        "nodes": nodes[:80],
        "pre_approval_note": (
            "If status is drafting or pending_review, workflow node count may be 0 by design; "
            "review this semantic tree instead of requiring materialized nodes."
        ),
    }


def _agent_review_timeout_seconds(_legacy_max_steps: Any = None) -> float:
    return REVIEW_TIMEOUT_MAX_SECONDS


def _review_subject_from_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    current_graph = evidence.get("current_canvas_graph") if isinstance(evidence.get("current_canvas_graph"), dict) else {}
    return {
        "canvas_status": current_graph.get("status"),
        "checksum": current_graph.get("checksum"),
        "node_count": current_graph.get("node_count"),
        "media_node_count": current_graph.get("media_node_count"),
    }


def _review_inputs_summary(review_inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "review_profile": review_inputs.get("review_profile"),
        "review_goal": review_inputs.get("review_goal"),
        "focus": review_inputs.get("focus"),
        "guide_topics": review_inputs.get("guide_topics"),
    }


def _clip_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"


def _render_subagent_tool_result(tool_name: str, result: Any) -> str:
    """Render read-tool output for subagents without hiding media status/URLs."""
    if tool_name in {"node.get", "node.run", "agent.review"}:
        payload = {
            "compact_summary": True,
            "tool": tool_name,
            "summary": context_compact.summarize_tool_result_for_context(tool_name, result),
        }
        return json.dumps(payload, ensure_ascii=False, default=str)
    rendered = json.dumps(result, ensure_ascii=False, default=str)
    if len(rendered) > TOOL_RESULT_TRUNCATE:
        return rendered[:TOOL_RESULT_TRUNCATE] + "...<truncated>"
    return rendered


def _coerce_score(value: Any, default: int) -> int:
    try:
        score = int(float(value))
    except (TypeError, ValueError):
        score = default
    return max(0, min(100, score))


def _coerce_string_list(value: Any, *, limit: int = 20) -> list[str]:
    if isinstance(value, list):
        raw_items = value[:limit]
    elif isinstance(value, str) and value.strip():
        raw_items = [
            item
            for item in re.split(r"[\n,，;；]+", value)
            if item.strip()
        ][:limit]
    else:
        raw_items = []
    out: list[str] = []
    for item in raw_items:
        text = _clip_text(item, 320)
        if text:
            out.append(text)
    return out


def _normalize_review_status(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in _REVIEW_STATUSES:
        return raw
    return _REVIEW_STATUS_ALIASES.get(raw, "")


def _normalize_review_finding(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        severity = str(value.get("severity") or "low").strip().lower()
        if severity not in _REVIEW_SEVERITIES:
            severity = "low"
        issue = _clip_text(value.get("issue") or value.get("title") or value.get("body"), 600)
        evidence = _clip_text(value.get("evidence"), 600)
        suggested_fix = _clip_text(value.get("suggested_fix") or value.get("suggestion"), 600)
        violated = _clip_text(value.get("violated_requirement"), 420)
    else:
        severity = "low"
        issue = _clip_text(value, 600)
        evidence = ""
        suggested_fix = ""
        violated = ""
    if not issue:
        issue = "审查项未说明具体问题。"
    grounded = bool(evidence or violated)
    return {
        "severity": severity,
        "issue": issue,
        "evidence": evidence,
        "suggested_fix": suggested_fix,
        "violated_requirement": violated,
        "grounded": grounded,
    }


def _blocked_review_payload(
    *,
    reason: str,
    detail: str = "",
    session_status: str,
    parse_status: str,
    timed_out: bool = False,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    evidence = _clip_text(reason, 240)
    if detail:
        evidence = (evidence + f"; {detail[:240]}").strip("; ")
    return {
        "schema_version": REVIEW_RESULT_SCHEMA_VERSION,
        "status": "blocked",
        "outcome": "blocked",
        "passed": False,
        "score": 0,
        "safe_to_submit": False,
        "safe_to_run": False,
        "parse_status": parse_status,
        "session_status": session_status,
        "timed_out": bool(timed_out),
        "timeout_seconds": timeout_seconds,
        "failure_reason": reason,
        "findings": [
            {
                "severity": "blocking",
                "issue": "只读审查子 Agent 未能产出可信的结构化审查结论。",
                "evidence": evidence,
                "suggested_fix": "主 Agent 应减少证据范围后重试 agent.review，或向用户说明审查阻塞。",
                "violated_requirement": "复杂制作继续执行前需要可信的 evidence-grounded review。",
                "grounded": True,
            }
        ],
        "missing_evidence": [],
        "suggested_next": "retry_review_or_report_blocked",
    }


def _normalize_review_result(
    value: Any,
    *,
    session_status: str,
    timed_out: bool = False,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return _blocked_review_payload(
            reason="invalid_result_type",
            detail=type(value).__name__,
            session_status=session_status,
            parse_status="invalid_result_type",
            timed_out=timed_out,
            timeout_seconds=timeout_seconds,
        )

    status = _normalize_review_status(value.get("status"))
    if not status:
        return _blocked_review_payload(
            reason="missing_or_invalid_status",
            detail=_clip_text(value.get("status"), 120),
            session_status=session_status,
            parse_status="invalid_status",
            timed_out=timed_out,
            timeout_seconds=timeout_seconds,
        )

    parse_status = "parsed"
    raw_findings = value.get("findings")
    if isinstance(raw_findings, list):
        findings = [_normalize_review_finding(item) for item in raw_findings[:40]]
    elif raw_findings in (None, ""):
        findings = []
    else:
        findings = [_normalize_review_finding(raw_findings)]
        parse_status = "repaired"

    grounded_blocking = any(
        finding.get("severity") == "blocking" and finding.get("grounded")
        for finding in findings
    )
    grounded_revise = any(
        finding.get("severity") in {"medium", "high", "blocking"} and finding.get("grounded")
        for finding in findings
    )

    if status == "blocked":
        passed = False
    elif isinstance(value.get("passed"), bool):
        passed = bool(value.get("passed"))
    else:
        passed = status == "pass"

    if status == "pass":
        default_score = 100
    elif status == "revise_required":
        default_score = 60
    else:
        default_score = 0
    score = _coerce_score(value.get("score"), default_score)

    if status == "blocked" or grounded_blocking:
        safe_to_submit = False
        safe_to_run = False
    else:
        raw_safe_to_submit = value.get("safe_to_submit")
        raw_safe_to_run = value.get("safe_to_run")
        if isinstance(raw_safe_to_submit, bool):
            safe_to_submit = bool(raw_safe_to_submit)
        else:
            safe_to_submit = status == "pass" or not grounded_revise
        if isinstance(raw_safe_to_run, bool):
            safe_to_run = bool(raw_safe_to_run)
        else:
            safe_to_run = safe_to_submit and status == "pass"

    return {
        "schema_version": REVIEW_RESULT_SCHEMA_VERSION,
        "status": status,
        "outcome": status,
        "passed": passed,
        "score": score,
        "safe_to_submit": safe_to_submit,
        "safe_to_run": safe_to_run,
        "parse_status": parse_status,
        "session_status": session_status,
        "timed_out": bool(timed_out),
        "timeout_seconds": timeout_seconds,
        "findings": findings,
        "missing_evidence": _coerce_string_list(value.get("missing_evidence")),
        "suggested_next": _clip_text(value.get("suggested_next"), 600)
        or ("continue_or_submit" if status == "pass" else "revise_or_request_evidence"),
    }


def _review_tool_envelope(
    *,
    role: str,
    task: str,
    review_inputs: dict[str, Any],
    review_result: dict[str, Any],
    summary: str,
    steps_used: int = 0,
    tool_log: list[dict[str, Any]] | None = None,
    subagent_error: str = "",
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "role": role,
        "task": task,
        "result": review_result,
        "summary": summary,
        "steps_used": steps_used,
        "tool_log": tool_log or [],
        "review_schema_version": REVIEW_RESULT_SCHEMA_VERSION,
        "review_status": review_result.get("status"),
        "review_subject": _review_subject_from_evidence(
            review_inputs.get("evidence") if isinstance(review_inputs.get("evidence"), dict) else {}
        ),
        "review_inputs_summary": _review_inputs_summary(review_inputs),
    }
    if subagent_error:
        envelope["subagent_error"] = subagent_error
    return envelope


def _is_readonly_tool(name: str) -> bool:
    from app.mcp_tools.registry import registry

    spec = registry.get(name)
    if not spec:
        return False
    tags = set(spec.tags or [])
    if tags & READONLY_FORBIDDEN_TAGS:
        return False
    return name in READONLY_TOOL_ALLOWLIST or "read" in tags


def _filter_readonly_tools(tool_names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for name in tool_names:
        if name in seen:
            continue
        seen.add(name)
        if _is_readonly_tool(name):
            out.append(name)
    return out


def _denied_readwrite_tools(tool_names: list[str]) -> list[str]:
    return [name for name in tool_names if not _is_readonly_tool(name)]


def _resolve_role(role: str, allowed_tools: list[str] | None) -> dict[str, Any]:
    preset = ROLE_PRESETS.get(role) or ROLE_PRESETS["default"]
    if allowed_tools is not None:
        return {
            "description": preset["description"],
            "system": preset["system"],
            "allowed_tools": _filter_readonly_tools(allowed_tools),
            "denied_tools": _denied_readwrite_tools(allowed_tools),
            "readonly": True,
        }
    return {
        **preset,
        "allowed_tools": _filter_readonly_tools(list(preset.get("allowed_tools") or [])),
        "denied_tools": _denied_readwrite_tools(list(preset.get("allowed_tools") or [])),
        "readonly": True,
    }


def _build_subagent_system(
    preset: dict[str, Any],
    task: str,
    inputs: dict | None,
) -> str:
    from app.mcp_tools.registry import registry

    def _short_desc(name: str) -> str:
        spec = registry.get(name)
        if not spec:
            return ""
        first_line = (spec.description or "").splitlines()[0] if spec.description else ""
        return first_line[:80]

    tools_block = "\n".join(
        f"- `{name}` — {_short_desc(name)}"
        for name in preset["allowed_tools"]
    ) or "(无工具,直接 finish 用文本回答)"

    return (
        preset["system"]
        + _review_profile_block(inputs)
        + "\n\n## 任务\n"
        + task
        + "\n\n## 输入\n"
        + json.dumps(inputs or {}, ensure_ascii=False)
        + "\n\n## 可用工具(白名单)\n"
        + tools_block
        + "\n\n## 协议\n"
        + "每一步只输出**一个** JSON 对象,二选一:\n"
        + '  - 调工具:`{"action":"call_tool","tool":"<名字>","input":{...}}`\n'
        + '  - 完成:  `{"action":"finish","result":<任意>,"summary":"一句中文总结"}`\n'
        + "\n## 只读边界\n"
        + "你是只读子 Agent。禁止调用任何写入、执行、生成、删除、批准、重置或配置变更工具。"
        + "如果需要改动,只能在 finish 里把建议交给主 Agent。"
        + "不要输出别的文字、不要 markdown、不要解释。只 JSON。"
    )


async def _subagent_loop(
    project_id: str,
    role: str,
    task: str,
    inputs: dict | None,
    max_steps: int | None,
    allowed_tools: list[str] | None,
) -> dict:
    from app.mcp_tools.registry import registry

    preset = _resolve_role(role, allowed_tools)
    denied_tools = list(preset.get("denied_tools") or [])
    if denied_tools:
        return {
            "role": role,
            "task": task,
            "result": None,
            "summary": "",
            "steps_used": 0,
            "tool_log": [
                {
                    "tool": name,
                    "ok": False,
                    "error": "readonly_tool_denied",
                    "step": 0,
                }
                for name in denied_tools
            ],
            "error": "readonly_tool_denied",
            "denied_tools": denied_tools,
            "allowed_tools": preset.get("allowed_tools", []),
        }
    system = _build_subagent_system(preset, task, inputs)
    transcript: list[dict] = [{"role": "user", "content": "开始你的工作。"}]
    tool_log: list[dict] = []

    async with session_scope() as session:
        svc = LLMService(session)

        step_no = 0
        while True:
            step_no += 1
            response = await svc.generate(
                task_type="agent_loop",
                messages=transcript,
                system=system,
                project_id=project_id,
            )
            raw = response.get("content", "")
            transcript.append({"role": "assistant", "content": raw})

            parsed = _parse_json_action(raw)
            if not parsed:
                transcript.append({
                    "role": "user",
                    "content": '上一步不是合法 JSON。请只输出 {"action":"finish",...} 或 {"action":"call_tool",...}。',
                })
                continue

            action = parsed.get("action")
            if action == "finish":
                return {
                    "role": role,
                    "task": task,
                    "result": parsed.get("result"),
                    "summary": parsed.get("summary", ""),
                    "steps_used": step_no,
                    "tool_log": tool_log,
                    "error": "",
                }

            if action == "call_tool":
                tool_name = parsed.get("tool", "")
                tool_input = parsed.get("input") or {}
                if not isinstance(tool_input, dict):
                    tool_input = {}

                if tool_name not in preset["allowed_tools"]:
                    msg = f"工具 {tool_name!r} 不在白名单。允许:{preset['allowed_tools']}"
                    tool_log.append({
                        "tool": tool_name, "ok": False, "error": "denied", "step": step_no,
                    })
                    transcript.append({"role": "user", "content": msg})
                    continue

                tool_input.setdefault("project_id", project_id)
                try:
                    result = await registry.call(tool_name, **tool_input)
                    ok = not (isinstance(result, dict) and result.get("error"))
                    rendered = _render_subagent_tool_result(tool_name, result)
                    if len(rendered) > TOOL_RESULT_TRUNCATE:
                        rendered = rendered[:TOOL_RESULT_TRUNCATE] + "...<truncated>"
                    tool_log.append({
                        "tool": tool_name,
                        "input": tool_input,
                        "ok": ok,
                        "step": step_no,
                    })
                    transcript.append({
                        "role": "user",
                        "content": f"工具 {tool_name} 返回:\n{rendered}",
                    })
                except Exception as exc:
                    tool_log.append({
                        "tool": tool_name,
                        "input": tool_input,
                        "ok": False,
                        "error": str(exc),
                        "step": step_no,
                    })
                    transcript.append({
                        "role": "user",
                        "content": f"工具 {tool_name} 抛错:{exc}。换个思路或 finish。",
                    })
                continue

            transcript.append({
                "role": "user",
                "content": f'未知 action {action!r}。请用 finish 或 call_tool。',
            })


# ── Public tools ────────────────────────────────────────────────────────


async def subagent_run(
    project_id: str,
    role: str,
    task: str,
    inputs: dict | None = None,
    max_steps: int = 6,
    allowed_tools: list[str] | None = None,
) -> dict:
    """Run one sub-agent to completion.

    Returns {role, task, result, summary, steps_used, tool_log, error}.
    `error` is "" on success. `max_steps` is accepted for old callers but is
    not used as a hard cap; the loop runs until finish, cancellation, or the
    caller's outer runtime timeout.
    Sub-agent roles are read-only reviewers/debuggers; custom allowed_tools may
    only narrow the read-only whitelist, never add write/execute/destructive tools.
    """
    if role not in ROLE_PRESETS and allowed_tools is None:
        return {
            "role": role,
            "task": task,
            "error": f"Unknown role {role!r}. Pick one of {list(ROLE_PRESETS)} "
                     f"or pass allowed_tools=[...]",
            "result": None, "summary": "", "steps_used": 0, "tool_log": [],
        }
    return await _subagent_loop(
        project_id=project_id,
        role=role,
        task=task,
        inputs=inputs,
        max_steps=max_steps,
        allowed_tools=allowed_tools,
    )


async def subagent_fan_out(
    project_id: str,
    role: str,
    tasks: list | str,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    max_steps: int = DEFAULT_MAX_STEPS,
    allowed_tools: list[str] | None = None,
) -> list[dict]:
    """Spawn N sub-agents in parallel; each entry is {task, inputs?}.

    A single failure doesn't kill siblings — each item comes back with its
    own `error` field. Concurrency is bounded by `max_concurrency`.
    """
    if isinstance(tasks, str):
        try:
            tasks = json.loads(tasks)
        except (json.JSONDecodeError, TypeError):
            return [{"error": "tasks must be a JSON array of objects"}]
    if not isinstance(tasks, list):
        return [{"error": "tasks must be a list"}]
    # Normalize: if items are strings, wrap them as {task: str}
    normalized = []
    for t in tasks:
        if isinstance(t, str):
            normalized.append({"task": t})
        elif isinstance(t, dict):
            normalized.append(t)
        else:
            normalized.append({"task": str(t)})
    tasks = normalized

    sem = asyncio.Semaphore(max(1, max_concurrency))

    async def bounded(item: dict) -> dict:
        async with sem:
            task_str = item.get("task", "") if isinstance(item, dict) else str(item)
            try:
                return await subagent_run(
                    project_id=project_id,
                    role=role,
                    task=task_str,
                    inputs=item.get("inputs") if isinstance(item, dict) else None,
                    max_steps=max_steps,
                    allowed_tools=allowed_tools,
                )
            except Exception as exc:
                return {
                    "role": role,
                    "task": task_str,
                    "error": str(exc),
                    "result": None, "summary": "", "steps_used": 0, "tool_log": [],
                }

    return list(await asyncio.gather(*[bounded(t) for t in tasks]))


async def subagent_aggregate(
    project_id: str,
    results: list[dict],
    instruction: str = "把这些子 Agent 的结果合成一句中文总结,告诉用户做了什么。",
) -> dict:
    """LLM-squash N sub-agent results into one paragraph + the raw items.

    Returns {text, items}. The main agent typically streams `text` to the
    user and keeps `items` for follow-up reasoning.
    """
    if not results:
        return {"text": "(没有子 Agent 结果)", "items": []}

    summary_in = [
        {
            "role": r.get("role", ""),
            "task": r.get("task", ""),
            "summary": r.get("summary", ""),
            "error": r.get("error", ""),
        }
        for r in results
    ]
    system = (
        "你是聚合器,把若干子 Agent 的结果合成一段简短中文摘要,"
        "口语化、不超过 4 句话、不要列举所有 task 详情。出错的项要点出来。"
    )
    user_msg = (
        instruction
        + "\n\n子 Agent 结果:\n"
        + json.dumps(summary_in, ensure_ascii=False, indent=2)
    )

    async with session_scope() as session:
        svc = LLMService(session)
        out = await svc.generate(
            task_type="agent_loop",
            messages=[{"role": "user", "content": user_msg}],
            system=system,
            project_id=project_id,
        )
    return {"text": (out.get("content") or "").strip(), "items": results}


# ── 四种协作模式（高层 wrapper） ───────────────────────────────────────


def _trace_id() -> str:
    return f"trace-{uuid.uuid4().hex[:12]}"


async def agent_map_reduce(
    project_id: str,
    role: str,
    tasks: list | str,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    max_steps: int = DEFAULT_MAX_STEPS,
    allowed_tools: list[str] | None = None,
    aggregator_instruction: str | None = None,
) -> dict:
    """Map-Reduce 模式:并行扇出 N 个独立子任务,可选 LLM 聚合摘要。

    Returns:
      {
        "mode": "map_reduce",
        "trace_id": str,
        "items": [...],  # 每个子 agent 的完整结果
        "summary": str,  # 聚合文本（aggregator_instruction 不为空时）
        "error_count": int,
      }

    适用:三模型对比、5 张候选图、10 个独立配角等无依赖并行任务。
    """
    trace = _trace_id()
    items = await subagent_fan_out(
        project_id=project_id,
        role=role,
        tasks=tasks,
        max_concurrency=max_concurrency,
        max_steps=max_steps,
        allowed_tools=allowed_tools,
    )
    error_count = sum(1 for r in items if isinstance(r, dict) and r.get("error"))
    out = {
        "mode": "map_reduce",
        "trace_id": trace,
        "items": items,
        "error_count": error_count,
        "summary": "",
    }
    if aggregator_instruction:
        agg = await subagent_aggregate(
            project_id=project_id,
            results=items,
            instruction=aggregator_instruction,
        )
        out["summary"] = agg.get("text", "")
    return out


async def agent_pipeline(
    project_id: str,
    stages: list | str,
    continue_on_error: bool = False,
) -> dict:
    """Pipeline 模式:顺序管道,前一阶段产出按 carry_keys 注入下一阶段。

    stages 每项: {role, task, inputs?, carry_keys?: ["scene_id","character_name"], allowed_tools?}

    Returns:
      {"mode": "pipeline", "trace_id": str, "stages": [...], "final": dict, "broken_at": int|None}
    """
    if isinstance(stages, str):
        try:
            stages = json.loads(stages)
        except (json.JSONDecodeError, TypeError):
            return {"mode": "pipeline", "error": "stages must be a JSON array"}
    if not isinstance(stages, list) or not stages:
        return {"mode": "pipeline", "error": "stages must be non-empty list"}

    trace = _trace_id()
    stage_outputs: list[dict] = []
    carry: dict = {}
    broken_at: int | None = None

    for i, stage in enumerate(stages):
        if not isinstance(stage, dict):
            stage = {"task": str(stage)}
        inputs = dict(stage.get("inputs") or {})
        inputs.update(carry)
        result = await subagent_run(
            project_id=project_id,
            role=stage.get("role", "default"),
            task=stage.get("task", ""),
            inputs=inputs,
            max_steps=stage.get("max_steps", DEFAULT_MAX_STEPS),
            allowed_tools=stage.get("allowed_tools"),
        )
        stage_outputs.append(result)

        if result.get("error"):
            broken_at = i
            if not continue_on_error:
                break

        carry_keys = stage.get("carry_keys") or []
        sub_result = result.get("result")
        if isinstance(sub_result, dict) and carry_keys:
            for k in carry_keys:
                if k in sub_result:
                    carry[k] = sub_result[k]

    return {
        "mode": "pipeline",
        "trace_id": trace,
        "stages": stage_outputs,
        "final": stage_outputs[-1] if stage_outputs else {},
        "broken_at": broken_at,
    }


async def agent_hierarchical(
    project_id: str,
    splits: list | str,
    role_map: dict | None = None,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
) -> dict:
    """Hierarchical 模式:每个 split 内部可继续走 map_reduce / pipeline / hierarchical（≤2 层）。

    splits 每项: {key: str, subplan: {mode: "map_reduce"|"pipeline"|"single", ...}}

    Returns:
      {"mode": "hierarchical", "trace_id": str, "splits": [{"key", "result"}, ...]}
    """
    if isinstance(splits, str):
        try:
            splits = json.loads(splits)
        except (json.JSONDecodeError, TypeError):
            return {"mode": "hierarchical", "error": "splits must be a JSON array"}
    if not isinstance(splits, list) or not splits:
        return {"mode": "hierarchical", "error": "splits must be non-empty list"}

    trace = _trace_id()
    role_map = role_map or {}
    sem = asyncio.Semaphore(max(1, max_concurrency))

    async def run_split(item: dict) -> dict:
        async with sem:
            key = item.get("key", "")
            subplan = item.get("subplan") or {}
            mode = subplan.get("mode", "single")
            role = role_map.get(key, subplan.get("role", "default"))

            if mode == "map_reduce":
                result = await agent_map_reduce(
                    project_id=project_id,
                    role=role,
                    tasks=subplan.get("tasks", []),
                    max_concurrency=subplan.get("max_concurrency", DEFAULT_MAX_CONCURRENCY),
                    aggregator_instruction=subplan.get("aggregator_instruction"),
                    allowed_tools=subplan.get("allowed_tools"),
                )
            elif mode == "pipeline":
                result = await agent_pipeline(
                    project_id=project_id,
                    stages=subplan.get("stages", []),
                    continue_on_error=subplan.get("continue_on_error", False),
                )
            else:
                result = await subagent_run(
                    project_id=project_id,
                    role=role,
                    task=subplan.get("task", ""),
                    inputs=subplan.get("inputs"),
                    max_steps=subplan.get("max_steps", DEFAULT_MAX_STEPS),
                    allowed_tools=subplan.get("allowed_tools"),
                )
            return {"key": key, "mode": mode, "result": result}

    results = await asyncio.gather(*[run_split(s if isinstance(s, dict) else {}) for s in splits])
    return {
        "mode": "hierarchical",
        "trace_id": trace,
        "splits": list(results),
    }


async def agent_review(
    project_id: str,
    review_goal: str = "",
    user_request: str = "",
    work_summary: str = "",
    review_profile: str = "general",
    evidence: dict | None = None,
    custom_checklist: list[str] | None = None,
    review_skill_key: str = "",
    review_skill: dict | str | None = None,
    guide_topics: list[str] | None = None,
    focus: list[str] | None = None,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> dict:
    """Run an isolated read-only review for the main agent.

    The main agent calls this for complex review needs, then decides whether to
    revise, continue, submit, or report based on the returned result.
    """
    guide_topics = _coerce_string_list(guide_topics, limit=8)
    focus = _coerce_string_list(focus, limit=12)
    custom_checklist = _coerce_string_list(custom_checklist, limit=20)
    profile_label = str(review_profile or "general").strip()[:120] or "general"
    evidence = _coerce_mapping_arg(evidence, fallback_key="text")
    if not review_goal:
        review_goal = "检查当前项目工作是否可继续提交或执行"
    if not work_summary:
        work_summary = "未提供工作摘要；请用只读工具核实当前项目状态、节点图和必要指南。"
    loaded_review_skill: dict[str, Any] | str = _coerce_review_skill_arg(
        review_skill,
        review_skill_key,
    )
    if not loaded_review_skill and review_skill_key:
        loaded_review_skill = _load_review_skill_by_key(review_skill_key)
    task = (
        "主 Agent 请求你做一次隔离只读审查。请结合审查目标、用户需求、主 Agent 工作摘要、"
        "审查 profile、证据包、项目真实状态和必要指南检查问题。只返回检查结果给主 Agent；如需修改，只写问题和建议，"
        "不要直接修改项目。还要检查主 Agent 的工具选择和修复范围是否符合最新用户需求与当前 skill。"
    )
    review_inputs = {
        "review_profile": profile_label,
        "review_goal": str(review_goal)[:800],
        "user_request": str(user_request)[:1600],
        "work_summary": str(work_summary)[:2400],
        "evidence": evidence,
        "custom_checklist": (custom_checklist or [])[:20],
        "review_skill_key": _normalize_review_skill_key(review_skill_key),
        "review_skill": loaded_review_skill,
        "guide_topics": guide_topics[:8],
        "focus": focus[:12],
        "expected_output": {
            "status": "pass | revise_required | blocked",
            "passed": "bool",
            "score": "0-100",
            "safe_to_submit": "bool",
            "safe_to_run": "bool",
            "findings": [
                {
                    "severity": "low | medium | high | blocking",
                    "issue": "具体问题",
                    "evidence": "来自节点、项目状态、图片描述、分镜或 prompt 的证据",
                    "suggested_fix": "主 Agent 可执行的修改建议",
                    "violated_requirement": "被违反的用户要求、节点事实、依赖关系或执行条件",
                }
            ],
            "missing_evidence": ["证据不足时列出缺口"],
            "suggested_next": "主 Agent 下一步应修改、补查、提交、继续执行或向用户说明阻塞",
        },
    }
    timeout_seconds = _agent_review_timeout_seconds(max_steps)
    try:
        result = await asyncio.wait_for(
            subagent_run(
                project_id=project_id,
                role="reviewer",
                task=task,
                inputs=review_inputs,
                max_steps=max_steps,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        return _blocked_review_result(
            role="reviewer",
            task=task,
            review_inputs=review_inputs,
            reason="subagent_timeout",
            detail=f"timeout_seconds={timeout_seconds:g}",
            session_status="timeout",
            parse_status="not_run",
            timed_out=True,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        return _blocked_review_result(
            role="reviewer",
            task=task,
            review_inputs=review_inputs,
            reason=f"subagent_exception:{type(exc).__name__}",
            detail=str(exc),
            session_status="exception",
            parse_status="not_run",
            timeout_seconds=timeout_seconds,
        )
    if not isinstance(result, dict):
        return _blocked_review_result(
            role="reviewer",
            task=task,
            review_inputs=review_inputs,
            reason="invalid_subagent_result",
            detail=type(result).__name__,
            session_status="invalid_result",
            parse_status="invalid_result_type",
            timeout_seconds=timeout_seconds,
        )
    if isinstance(result, dict) and result.get("error"):
        error_reason = str(result.get("error") or "subagent_failed")
        return _blocked_review_result(
            role=str(result.get("role") or "reviewer"),
            task=str(result.get("task") or task),
            review_inputs=review_inputs,
            reason=error_reason,
            detail=str(result.get("summary") or ""),
            tool_log=result.get("tool_log") if isinstance(result.get("tool_log"), list) else [],
            steps_used=int(result.get("steps_used") or 0),
            session_status="failed",
            parse_status="not_run",
            timeout_seconds=timeout_seconds,
        )
    review_result = _normalize_review_result(
        result.get("result"),
        session_status=REVIEW_SESSION_OK_STATUS,
        timeout_seconds=timeout_seconds,
    )
    summary = str(result.get("summary") or "")
    if review_result.get("parse_status") not in REVIEW_PARSE_OK_STATUSES:
        summary = f"审查阻塞：{review_result.get('failure_reason') or review_result.get('parse_status')}"
    return _review_tool_envelope(
        role=str(result.get("role") or "reviewer"),
        task=str(result.get("task") or task),
        review_inputs=review_inputs,
        review_result=review_result,
        summary=summary,
        steps_used=int(result.get("steps_used") or 0),
        tool_log=result.get("tool_log") if isinstance(result.get("tool_log"), list) else [],
    )


def _blocked_review_result(
    *,
    role: str,
    task: str,
    review_inputs: dict[str, Any],
    reason: str,
    detail: str = "",
    tool_log: list[dict[str, Any]] | None = None,
    steps_used: int = 0,
    session_status: str = "failed",
    parse_status: str = "not_run",
    timed_out: bool = False,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Return a successful tool envelope for a failed review attempt.

    The review result is explicitly blocked, so callers can record that a
    reviewer was invoked without pretending the reviewed work passed.
    """
    review_result = _blocked_review_payload(
        reason=reason,
        detail=detail,
        session_status=session_status,
        parse_status=parse_status,
        timed_out=timed_out,
        timeout_seconds=timeout_seconds,
    )
    return _review_tool_envelope(
        role=role,
        task=task,
        review_inputs=review_inputs,
        review_result=review_result,
        summary=(f"审查阻塞：{reason}" + (f"；{detail[:240]}" if detail else "")),
        steps_used=steps_used,
        tool_log=tool_log or [],
        subagent_error=reason,
    )


# ── Existing tools (unchanged) ──────────────────────────────────────────


def _coerce_mapping_arg(value: object, *, fallback_key: str | None = None) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        if fallback_key:
            return {fallback_key: value}
    return {}


async def export_project_zip(project_id: str, output_name: str | None = None) -> dict:
    """Package state.json + storage/<project> into a zip under storage/<project>/exports/."""
    from app.mcp_tools.file_tools import _project_dir  # type: ignore

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}
        state = json.loads(project.state_json or "{}")
        title = project.title

    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    name = output_name or f"{title}-{timestamp}.zip"
    project_dir: Path = _project_dir(project_id)
    exports = project_dir / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    zip_path = exports / name

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "project_state.json",
            json.dumps(state, ensure_ascii=False, indent=2),
        )
        for entry in project_dir.rglob("*"):
            if entry.is_file() and exports not in entry.parents and entry != zip_path:
                z.write(entry, entry.relative_to(project_dir))

    return {
        "path": str(zip_path.relative_to(project_dir)).replace("\\", "/"),
        "size": zip_path.stat().st_size,
    }
