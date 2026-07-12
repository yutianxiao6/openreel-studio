"""Sub-agent helpers + high-level collaboration tools.

A sub-agent is a short-lived autonomous loop:
  1. high-level collaboration wrappers call `subagent_run(role, task, ...)`
  2. sub-agent gets a scoped system prompt + a tool whitelist
  3. it calls native OpenAI-style tools from that whitelist, or returns a JSON result
  4. result + summary + tool_log come back to the caller

Low-level subagent helpers are direct Python helpers, not registry tools.
Registry exposure is limited to high-level collaboration wrappers.

Design notes:
  - sub-agents are read-only reviewers/debuggers by default.
  - write-capable workers are explicit role presets with a narrow whitelist.
  - sub-agents never touch the DB / network directly — only via
    `registry.call(...)` for whitelisted tools.
  - `project_id` is auto-injected into scoped tool calls so a sub-agent can't
    leak across projects.
  - tool errors are NOT raised — they are written back into the transcript
    so the sub-agent can see "that didn't work" and pick a different path.
  - tool outputs are truncated to ~2000 chars before being fed back, so the
    sub-agent's context doesn't explode.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import uuid
import zipfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agent import context_compact
from app.agent import canvas_workflow_templates, workflow_spec_artifacts
from app.agent import workflow_spec_role
from app.agent.prompt_dump import dump_llm_request, new_run_id
from app.agent.token_usage import build_usage_snapshot
from app.agent.vision_context import redact_image_data_urls
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
    "skill.search",
    "skill.get",
    "workflow.spec.read",
    "workflow.template.resolve",
    "workflow.template.read",
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

IMAGE_EDITOR_ROLE_NAME = "image_editor"
NODE_PRODUCER_ROLE_NAME = "node_producer"
WORKFLOW_SPEC_ROLE_NAME = workflow_spec_role.ROLE_NAME

WORKFLOW_SPEC_SELECTOR_TOOLS: list[str] = workflow_spec_role.SELECTOR_TOOLS
WORKFLOW_SPEC_MAX_OUTPUT_TOKENS = workflow_spec_role.MAX_OUTPUT_TOKENS
AGENT_RUN_ROLE_NAMES: set[str] = {
    NODE_PRODUCER_ROLE_NAME,
    IMAGE_EDITOR_ROLE_NAME,
    WORKFLOW_SPEC_ROLE_NAME,
}
NODE_PRODUCER_NODE_TYPES: set[str] = {"text", "image", "video", "audio"}

ROLE_PRESETS: dict[str, dict[str, Any]] = {
    "researcher": {
        "description": "只读调研:看项目状态、画布、记忆、文件,不写任何东西",
        "task_type": "agent_review",
        "system": (
            "你是 researcher 子 Agent,任务是**只读**调研。你只能调用读类工具"
            "(project.get_state / node.list / node.get / memory.recall 等),"
            "调研完成后用最终 JSON 把答案交回。不要尝试生成、修改、运行或删除任何内容。"
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
        "task_type": "agent_review",
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
            "如果 evidence 已包含待审节点 prompt/fields/references 和检查清单，先直接给出最终 JSON 结论，"
            "只有证据缺失或矛盾时才继续调工具。"
            "禁止创建、修改、删除、运行节点,也禁止调用 drama/media 生成工具。"
        ),
        "allowed_tools": [
            "project.get_state",
            "task.list",
            "node.list",
            "node.get",
            "skill.project_mentor",
            "skill.search",
            "skill.get",
            "file.read_text",
            "memory.recall",
        ],
    },
    "debugger": {
        "description": "只读排障:查看失败节点、trace、事件和项目状态",
        "task_type": "agent_review",
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
        "task_type": "agent_review",
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
        "task_type": "agent_aux",
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
        "task_type": "agent_aux",
        "system": (
            "你是一个只读子 Agent。不调任何工具,直接基于 task 和 inputs 用最终 JSON 回答。"
        ),
        "allowed_tools": [],
    },
    WORKFLOW_SPEC_ROLE_NAME: workflow_spec_role.role_preset(),
    NODE_PRODUCER_ROLE_NAME: {
        "description": "通用节点生产 worker:补全、运行和自检主 Agent 已规划的指定节点",
        "task_type": "subagent_node_producer",
        "readonly": False,
        "include_tool_schemas": True,
        "enforce_max_steps": True,
        "compact_prompt": True,
        "max_steps": 12,
        "scope_hint": "主 Agent 指定作用域内节点",
        "system": (
            "你是 node_producer 子 Agent,负责完成主 Agent 指定的独立节点任务。"
            "主 Agent 会传 node_id/node_ids、allowed_node_types、objective、basis、primary_skill、inline_spec、reference_node_ids 和验收标准；它已经完成流程规划和节点创建。"
            "先读取目标节点,再读取 reference_node_ids 和目标节点依赖的上游节点作为上下文；有 primary_skill 时用 skill.get 读取这一份模块 skill；有 inline_spec 时它优先于通用指南；没有 skill 时按输入事实和通用模型判断完成。"
            "更新 prompt/fields/references 后按需要运行节点；图片结果可用 vision.view_image 自检；失败时优先修同一节点并有限重试。"
            "只处理输入作用域内的节点,上游节点只读引用,不搜索流程,不规划整条视频流程,不新建节点。"
            "最终只交回结构化结果和验证依据。"
        ),
        "allowed_tools": [
            "skill.get",
            "node.get",
            "node.update",
            "node.run",
            "vision.view_image",
        ],
        "result_contract": (
            "result: {status:'completed|blocked', node_ids, completed_node_ids, output_refs, "
            "basis_used, prompt_summary, verification, issues, blocked_reason}。"
            "completed 必须有 node_ids/completed_node_ids 或 output_refs；blocked 必须有 blocked_reason。"
        ),
    },
    IMAGE_EDITOR_ROLE_NAME: {
        "description": "图片编辑 worker:隔离查看图片、生成编辑候选、验证后提交为节点历史",
        "task_type": "subagent_image_editor",
        "readonly": False,
        "include_tool_schemas": True,
        "enforce_max_steps": True,
        "max_steps": 20,
        "system": (
            "你是 image_editor 子 Agent,专门处理当前项目 image 节点的本地图片编辑。"
            "你接收主 Agent 给出的任务、目标节点和补充输入,在隔离上下文中完成编辑。"
            "工作流是读取目标节点,用 vision.view_image 查看原图,用 image.edit action='preview' 生成候选图。"
            "preview 是候选事务,不会覆盖节点；image.edit preview 返回的图片会直接进入下一轮视觉上下文,可直接作为候选图查看结果。"
            "同一 candidate_ref 已由 image.edit preview 进入视觉上下文后,不重复调用 vision.view_image。"
            "候选图符合验收标准后用 image.edit action='commit' 提交；vision.view_image 用于读取原图、旧候选或缺失视觉上下文的图片引用。"
            "原图 node:<node_id> 是回退点,可从原图或通过验证的候选图继续编辑；不合格候选只记录原因,不作为后续源图。"
            "查看候选图后,继续从该候选图编辑会自动保留候选；从其他源重新 preview 会自动丢弃未确认候选。"
            "坐标可以使用 pixel 精确指定,也可以使用 normalized 表达比例位置。"
            "抠主体、透明背景和复杂边缘先用 image.segment 生成 cutout_ref、mask_ref、bbox,再用 image.edit 裁剪、圆角和提交。"
            "精细裁切、图标圆角和边缘背景清理使用 image.edit 的 crop/mask/selection 原语。"
            "图标类任务重点验证主体完整、安全边距、透明背景、角和边缘；边角问题优先使用 mask/rounded_rect/transparent 或分割,大幅 crop 只适合用户明确要求裁掉内容。"
            "简单裁剪通常 4-8 步完成；复杂抠图、透明背景、反复预览可使用更多步骤。"
            "完成时返回结构化结果给主 Agent,包括 status、node_id、candidate_ref、committed_ref、operations_summary、verification 和 issues。"
            "如果图片、节点或编辑目标不足以可靠完成,返回 status='blocked' 并说明缺少的信息或失败步骤。"
        ),
        "allowed_tools": [
            "node.list",
            "node.get",
            "vision.view_image",
            "image.segment",
            "image.edit",
        ],
        "result_contract": (
            "最终 result 使用对象结构: "
            "{status:'completed|blocked', node_id, committed:boolean, candidate_ref, committed_ref, "
            "operations_summary, verification, issues:[string]}。"
            "已提交的结果 status='completed' 且 committed=true；无法完成时 status='blocked'。"
        ),
    },
}

DEFAULT_MAX_STEPS = 4
MAX_SUBAGENT_STEPS = 40
DEFAULT_MAX_CONCURRENCY = 3
TOOL_RESULT_TRUNCATE = 2000
WORKFLOW_SPEC_SKILL_RESULT_TRUNCATE = 24000
WORKFLOW_SPEC_ARTIFACT_TOOL_RESULT_TRUNCATE = 5000
SUBAGENT_IMAGE_CONTEXT_LIMIT = 3
SUBAGENT_PROMPT_SCHEMA_VERSION = "subagent_prompt_v2"
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
    try:
        from app.mcp_tools import skill_tools

        loaded = skill_tools.load_review_skill_by_key(key)
        if loaded.get("ok"):
            return loaded
    except Exception:
        pass
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
        + "最终 result 必须使用对象结构: "
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


def _parse_json_object(raw: str) -> dict | None:
    try:
        obj = json.loads(_strip_fences(str(raw or "")))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _parse_json_action(raw: str) -> dict | None:
    obj = _parse_json_object(raw)
    if isinstance(obj, dict) and "action" in obj:
        return obj
    return None


def _parse_subagent_final_result(raw: str) -> dict[str, Any] | None:
    obj = _parse_json_object(raw)
    if obj is None:
        return None

    if obj.get("action") == "finish":
        result_payload = obj.get("result")
        summary = str(obj.get("summary") or "")
    else:
        result_payload = obj.get("result") if "result" in obj else {
            key: value for key, value in obj.items() if key not in {"summary", "message"}
        }
        summary = str(obj.get("summary") or obj.get("message") or "")
        status = str(obj.get("status") or "").strip()
        if isinstance(result_payload, dict):
            result_payload = dict(result_payload)
            if status and not result_payload.get("status"):
                result_payload["status"] = status
        elif status:
            result_payload = {"status": status, "value": result_payload}

    return {"result": result_payload, "summary": summary}


def _assistant_message_payload(message: Any) -> dict[str, Any]:
    try:
        payload = message.model_dump()
        if isinstance(payload, dict):
            payload.setdefault("role", "assistant")
            return payload
    except Exception:
        pass
    tool_calls: list[dict[str, Any]] = []
    for index, tool_call in enumerate(list(getattr(message, "tool_calls", None) or [])):
        name, arguments = _tool_call_function_payload(tool_call)
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {}, ensure_ascii=False, default=str)
        tool_calls.append({
            "id": _tool_call_id(tool_call, f"subagent-call-{index}"),
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })
    return {
        "role": "assistant",
        "content": getattr(message, "content", None),
        "tool_calls": tool_calls,
    }


def _tool_call_id(tool_call: Any, fallback: str) -> str:
    if isinstance(tool_call, dict):
        return str(tool_call.get("id") or fallback)
    return str(getattr(tool_call, "id", None) or fallback)


def _tool_call_function_payload(tool_call: Any) -> tuple[str, Any]:
    if isinstance(tool_call, dict):
        function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
        return str(function.get("name") or ""), function.get("arguments")
    function = getattr(tool_call, "function", None)
    if isinstance(function, dict):
        return str(function.get("name") or ""), function.get("arguments")
    return str(getattr(function, "name", "") or ""), getattr(function, "arguments", None)


def _coerce_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


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


def _effective_subagent_step_limit(
    *,
    role: str,
    preset: dict[str, Any],
    max_steps: int | None,
) -> int:
    requested = int(max_steps or preset.get("max_steps") or DEFAULT_MAX_STEPS)
    if role == WORKFLOW_SPEC_ROLE_NAME:
        requested = max(requested, int(preset.get("max_steps") or DEFAULT_MAX_STEPS))
    return max(1, min(requested, MAX_SUBAGENT_STEPS))


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
    if isinstance(result, dict) and any(str(key).startswith("_model_content") for key in result):
        result = {
            key: value
            for key, value in result.items()
            if not str(key).startswith("_model_content")
        }
    if tool_name in {"node.get", "node.run", "agent.review"}:
        payload = {
            "compact_summary": True,
            "tool": tool_name,
            "summary": context_compact.summarize_tool_result_for_context(tool_name, result),
        }
        return json.dumps(payload, ensure_ascii=False, default=str)
    return json.dumps(result, ensure_ascii=False, default=str)


def _subagent_tool_result_limit(role: str, tool_name: str) -> int:
    if role == WORKFLOW_SPEC_ROLE_NAME and tool_name == "skill.get":
        return WORKFLOW_SPEC_SKILL_RESULT_TRUNCATE
    if role == WORKFLOW_SPEC_ROLE_NAME and tool_name in {"workflow.spec.read", "workflow.template.read"}:
        return WORKFLOW_SPEC_SKILL_RESULT_TRUNCATE
    if role == WORKFLOW_SPEC_ROLE_NAME and tool_name.startswith("workflow.spec."):
        return WORKFLOW_SPEC_ARTIFACT_TOOL_RESULT_TRUNCATE
    return TOOL_RESULT_TRUNCATE


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


_SUBAGENT_TRACE_SECRET_KEYS = re.compile(
    r"(?i)(api[_-]?key|authorization|secret|password|bearer|(^|[_-])(access|refresh|id|auth|session|api)?[_-]?token($|[_-]))"
)


def _subagent_trace_value(value: Any, *, depth: int = 0) -> Any:
    value = redact_image_data_urls(value)
    if depth >= 4:
        return "<max_depth>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 24:
                out["_truncated_keys"] = len(value) - 24
                break
            key_str = str(key)
            if key_str in {"project_id", "_state"}:
                continue
            if _SUBAGENT_TRACE_SECRET_KEYS.search(key_str):
                out[key_str] = "<redacted>"
                continue
            out[key_str] = _subagent_trace_value(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        out = [_subagent_trace_value(item, depth=depth + 1) for item in value[:16]]
        if len(value) > 16:
            out.append({"_truncated_items": len(value) - 16})
        return out
    if isinstance(value, str):
        return _clip_text(value, 500)
    return value


def _append_subagent_trace(
    trace_log: list[dict[str, Any]],
    *,
    role: str,
    step: int,
    event: str,
    **fields: Any,
) -> None:
    trace_log.append({
        "agent": role,
        "step": step,
        "event": event,
        **{key: _subagent_trace_value(value) for key, value in fields.items() if value not in (None, "", [], {})},
    })
    del trace_log[:-120]


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
        issue = _clip_text(value.get("issue") or value.get("title") or value.get("body"), 260)
        evidence = _clip_text(value.get("evidence"), 180)
        suggested_fix = _clip_text(value.get("suggested_fix") or value.get("suggestion"), 180)
        violated = _clip_text(value.get("violated_requirement"), 160)
    else:
        severity = "low"
        issue = _clip_text(value, 260)
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
    subagent_usage: list[dict[str, Any]] | None = None,
    subagent_trace: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "role": role,
        "result": review_result,
        "summary": _clip_text(summary, 240),
        "review_status": review_result.get("status"),
    }
    if subagent_error:
        envelope["subagent_error"] = subagent_error
    if subagent_usage:
        envelope["_subagent_usage"] = subagent_usage
    if subagent_trace:
        envelope["_subagent_trace"] = subagent_trace
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


def _filter_tools_for_preset(preset: dict[str, Any], tool_names: list[str]) -> list[str]:
    strict_allowlist = bool(preset.get("strict_allowed_tools"))
    if bool(preset.get("readonly", True)) and not strict_allowlist:
        return _filter_readonly_tools(tool_names)
    role_allowlist = set(preset.get("allowed_tools") or [])
    seen: set[str] = set()
    out: list[str] = []
    for name in tool_names:
        if name in seen:
            continue
        seen.add(name)
        if name in role_allowlist:
            out.append(name)
    return out


def _denied_tools_for_preset(preset: dict[str, Any], tool_names: list[str]) -> list[str]:
    allowed = set(_filter_tools_for_preset(preset, tool_names))
    return [name for name in tool_names if name not in allowed]


def _resolve_role(role: str, allowed_tools: list[str] | None) -> dict[str, Any]:
    preset = ROLE_PRESETS.get(role) or ROLE_PRESETS["default"]
    readonly = bool(preset.get("readonly", True))
    if allowed_tools is not None:
        return {
            "description": preset["description"],
            "system": preset["system"],
            "allowed_tools": _filter_tools_for_preset(preset, allowed_tools),
            "denied_tools": _denied_tools_for_preset(preset, allowed_tools),
            "readonly": readonly,
            "include_tool_schemas": bool(preset.get("include_tool_schemas")),
            "enforce_max_steps": bool(preset.get("enforce_max_steps")),
            "max_output_tokens": preset.get("max_output_tokens"),
            "task_type": str(preset.get("task_type") or "agent_loop"),
            "result_contract": str(preset.get("result_contract") or ""),
        }
    return {
        **preset,
        "allowed_tools": _filter_tools_for_preset(preset, list(preset.get("allowed_tools") or [])),
        "denied_tools": _denied_tools_for_preset(preset, list(preset.get("allowed_tools") or [])),
        "readonly": readonly,
        "task_type": str(preset.get("task_type") or "agent_loop"),
    }


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _stable_hash(value: Any) -> str:
    text = value if isinstance(value, str) else _stable_json(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _subagent_tool_description_payload(tool_names: list[str]) -> list[dict[str, Any]]:
    from app.mcp_tools.registry import registry

    payload: list[dict[str, Any]] = []
    for name in tool_names:
        spec = registry.get(name)
        if not spec:
            continue
        payload.append({
            "name": spec.name,
            "description": (spec.description or spec.name).splitlines()[0][:64],
        })
    return payload


def _subagent_tool_description_block(tool_names: list[str]) -> str:
    payload = _subagent_tool_description_payload(tool_names)
    if not payload:
        return "(无工具,直接输出最终 JSON)"
    return "\n".join(f"- `{item['name']}` — {item['description']}" for item in payload)


def _subagent_tool_schema_block(tool_names: list[str]) -> str:
    from app.mcp_tools.registry import registry

    payload: list[dict[str, Any]] = []
    for name in tool_names:
        spec = registry.get(name)
        if not spec:
            continue
        payload.append({
            "name": spec.name,
            "description": (spec.description or spec.name).splitlines()[0][:240],
            "input_schema": spec.schema or {},
        })
    if not payload:
        return ""
    return "\n\n## 工具参数\n" + json.dumps(payload, ensure_ascii=False, default=str)


def _subagent_openai_tools(tool_names: list[str]) -> list[dict[str, Any]]:
    from app.mcp_tools.registry import registry

    tools = registry.get_openai_tools(names=tool_names)
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict):
            function["name"] = str(function.get("name") or "").replace(".", "__")
    return tools


def _subagent_tool_accepts_project_id(tool_name: str) -> bool:
    from app.mcp_tools.registry import registry

    spec = registry.get(tool_name)
    if not spec:
        return False
    try:
        signature = inspect.signature(spec.handler)
    except (TypeError, ValueError):
        return False
    if "project_id" in signature.parameters:
        return True
    return any(
        param.kind == inspect.Parameter.VAR_KEYWORD
        for param in signature.parameters.values()
    )


def _subagent_result_contract_section(preset: dict[str, Any]) -> str:
    result_contract = str(preset.get("result_contract") or "").strip()
    return f"## 返回结构\n{result_contract}" if result_contract else ""


def _subagent_boundary_section(preset: dict[str, Any]) -> str:
    if bool(preset.get("readonly", True)):
        return (
            "## 只读边界\n"
            "你是只读子 Agent。禁止调用任何写入、执行、生成、删除、批准、重置或配置变更工具。"
            "如果需要改动,只能在 finish 里把建议交给主 Agent。"
        )
    return (
        "## 作用域\n"
        "你只使用白名单工具完成本职责内任务。写入能力只限白名单工具表达的目标对象和当前 project_id。"
        "完成后把结果、验证依据和需要主 Agent 注意的事项写进 finish。"
    )


def _subagent_protocol_section(preset: dict[str, Any]) -> str:
    if bool(preset.get("compact_prompt")):
        scope_hint = str(preset.get("scope_hint") or "本职责内对象")
        return (
            "## 协议\n"
            '按需调用白名单工具；完成只输出 JSON: {"status":"completed|blocked","summary":"...","result":{...}}。\n'
            f"写入只限当前 project_id 和{scope_hint}；不要输出 markdown 或解释。"
        )
    return (
        "## 协议\n"
        "需要读取或修改时直接调用白名单工具；每一轮选择必要工具或结束。\n"
        "调用工具时 assistant content 必须只写一句简短当前任务说明；为空则没有进度说明。\n"
        "完成时只输出一个 JSON 对象:"
        '`{"status":"completed|blocked","summary":"一句中文总结","result":<返回结构>}`。\n'
        "最终回复不要 markdown、不要解释。"
    )


def _build_subagent_system_sections(preset: dict[str, Any]) -> list[dict[str, str]]:
    """Build stable sub-agent system sections.

    The task, user facts, review profile, and other per-run data belong in the
    task message. Keeping this list stable lets prompt caches behave like the
    main Agent Loop prefix.
    """
    tool_names = list(preset.get("allowed_tools") or [])
    if bool(preset.get("compact_prompt")):
        tools_block = ", ".join(f"`{name}`" for name in tool_names) or "(无工具)"
    else:
        tools_block = _subagent_tool_description_block(tool_names)

    sections: list[dict[str, str]] = [
        {
            "name": "subagent.role",
            "trigger": "always",
            "tier": "s",
            "source": "preset",
            "text": str(preset.get("system") or "").strip(),
        },
        {
            "name": "subagent.tools",
            "trigger": "always",
            "tier": "s",
            "source": "registry",
            "text": ("## 工具\n" if bool(preset.get("compact_prompt")) else "## 可用工具(白名单)\n") + tools_block,
        },
    ]
    result_section = _subagent_result_contract_section(preset)
    if result_section:
        sections.append({
            "name": "subagent.result_contract",
            "trigger": "always",
            "tier": "s",
            "source": "preset",
            "text": result_section,
        })
    sections.append({
        "name": "subagent.protocol",
        "trigger": "always",
        "tier": "s",
        "source": "static",
        "text": _subagent_protocol_section(preset),
    })
    sections.append({
        "name": "subagent.boundary",
        "trigger": "always",
        "tier": "s",
        "source": "static",
        "text": _subagent_boundary_section(preset),
    })
    return [section for section in sections if section.get("text")]


def _build_subagent_prompt_package(
    role: str,
    preset: dict[str, Any],
    task: str,
    inputs: dict | None,
) -> dict[str, Any]:
    tool_names = list(preset.get("allowed_tools") or [])
    sections = _build_subagent_system_sections(preset)
    system = "\n\n".join(str(section.get("text") or "").rstrip() for section in sections if section.get("text"))
    task_message = _build_subagent_task_message_for_role(role, task, inputs)
    tools = _subagent_openai_tools(tool_names)
    task_type = str(preset.get("task_type") or "agent_loop")
    tool_schema_hash = _stable_hash(tools)
    stable_system_hash = _stable_hash(system)
    stable_payload = {
        "schema_version": SUBAGENT_PROMPT_SCHEMA_VERSION,
        "role": role,
        "task_type": task_type,
        "readonly": bool(preset.get("readonly", True)),
        "system": system,
        "tool_names": tool_names,
        "tool_schema_hash": tool_schema_hash,
    }
    cache_key = f"{SUBAGENT_PROMPT_SCHEMA_VERSION}:{role}:{_stable_hash(stable_payload)}"
    diagnostics = {
        "cache_key": cache_key,
        "schema_version": SUBAGENT_PROMPT_SCHEMA_VERSION,
        "subagent": role,
        "task_type": task_type,
        "readonly": bool(preset.get("readonly", True)),
        "system_chars": len(system),
        "history_chars": len(task_message),
        "stable_system_hash": stable_system_hash,
        "tool_schema_hash": tool_schema_hash,
        "tools_count": len(tools),
        "tool_names": tool_names,
        "section_count": len(sections),
        "sections_by_trigger": {
            trigger: sum(1 for section in sections if section.get("trigger") == trigger)
            for trigger in sorted({str(section.get("trigger") or "") for section in sections})
            if trigger
        },
        "sections_by_tier": {
            tier: sum(1 for section in sections if section.get("tier") == tier)
            for tier in sorted({str(section.get("tier") or "") for section in sections})
            if tier
        },
        "sections": [
            {
                "name": f"{section.get('name')}.{role}",
                "trigger": str(section.get("trigger") or "always"),
                "tier": str(section.get("tier") or "s"),
                "chars": len(str(section.get("text") or "")),
                "source": str(section.get("source") or "static"),
            }
            for section in sections
        ],
    }
    return {
        "system": system,
        "task_message": task_message,
        "tools": tools,
        "tool_names": tool_names,
        "task_type": task_type,
        "cache_key": cache_key,
        "diagnostics": diagnostics,
    }


def _build_subagent_task_message(task: str, inputs: dict | None) -> str:
    return _build_subagent_task_message_for_role("default", task, inputs)


def _subagent_review_context_block(role: str, inputs: dict | None) -> str:
    if role in {"reviewer", "media_prompt_reviewer"}:
        return _review_profile_block(inputs if isinstance(inputs, dict) else {})
    if not isinstance(inputs, dict):
        return ""
    review_keys = {"review_profile", "custom_checklist", "review_skill", "review_skill_key"}
    if any(key in inputs for key in review_keys):
        return _review_profile_block(inputs)
    return ""


def _build_subagent_task_message_for_role(role: str, task: str, inputs: dict | None) -> str:
    if role == NODE_PRODUCER_ROLE_NAME:
        base = _build_node_producer_task_message(task, inputs)
    elif role == IMAGE_EDITOR_ROLE_NAME:
        base = _build_image_editor_task_message(task, inputs)
    elif role == WORKFLOW_SPEC_ROLE_NAME:
        base = _build_workflow_spec_task_message(task, inputs)
    else:
        base = (
            "## 任务\n"
            + str(task or "").strip()
            + "\n\n## 输入\n"
            + json.dumps(inputs or {}, ensure_ascii=False)
        )
    review_context = _subagent_review_context_block(role, inputs)
    return base + review_context


def _build_workflow_spec_task_message(task: str, inputs: dict | None) -> str:
    return workflow_spec_role.build_task_message(task, inputs)


def _build_node_producer_task_message(task: str, inputs: dict | None) -> str:
    raw_task = str(task or "").strip() or "完成主 Agent 委派的节点生产任务。"
    inputs_json = json.dumps(inputs or {}, ensure_ascii=False)
    return (
        "## 用户目标\n"
        + raw_task
        + "\n\n## 输入\n"
        + inputs_json
        + "\n\n## 作用域\n"
        + "- 只处理输入中的 node_id/node_ids/target_node_ids/scoped_node_ids。\n"
        + "- reference_node_ids、fields.references 和 depends_on 是只读上游上下文；读取后用于补全当前节点,不更新这些上游节点。\n"
        + "- allowed_node_types 限定可处理的节点类型；没有传时可处理 text/image/video/audio，但仍受节点作用域限制。\n"
        + "- basis/primary_skill/inline_spec 是本轮判断依据；primary_skill 已由主 Agent 选择，inline_spec 和用户当前明确要求优先。\n"
        + "- 没有 skill 时使用输入事实和通用模型能力完成，并在 basis_used 里记录依据。\n"
        + "\n\n## 节点生命周期\n"
        + "- 读取目标节点，再读取指定上游节点，必要时读取指定 skill。\n"
        + "- 补全或更新 fields.content/prompt/references/必要参数后，按需要运行节点。\n"
        + "- 图片结果需要看图自检；视频/音频异步任务以节点状态和工具返回为准。\n"
        + "- 失败时优先修同一作用域节点并有限重试；信息不足或连续失败时返回 blocked。\n"
        + "\n\n## 返回要求\n"
        + "- completed 返回 node_ids/completed_node_ids 或 output_refs，并写 verification。\n"
        + "- blocked 返回 blocked_reason、已尝试步骤和需要主 Agent 补充或决定的事项。"
    )


def _build_image_editor_task_message(task: str, inputs: dict | None) -> str:
    raw_task = str(task or "").strip() or "完成主 Agent 委派的图片编辑任务。"
    inputs_json = json.dumps(inputs or {}, ensure_ascii=False)
    return (
        "## 用户目标\n"
        + raw_task
        + "\n\n## 输入\n"
        + inputs_json
        + "\n\n## 目标成品\n"
        + "- 在现有 image 节点上完成本地编辑,最终提交为该节点的一条成功历史。\n"
        + "- 成品满足用户当前明确要求,同时保持画面主体完整、比例自然、边缘干净。\n"
        + "- 软件图标类成品应有清晰主体、安全边距、可用透明背景或合理底图、四角和边缘整洁,没有无意义外框或被裁断的主体。\n"
        + "- 任务涉及边角、外框、透明背景、抠图或主体保留时,优先使用分割、mask、rounded_rect、transparent/clear 等局部编辑原语处理。\n"
        + "- crop 只用于安全的小幅构图修整；裁后主体贴边、缺边、缺底、缺角或比例异常的候选视为不合格。\n"
        + "\n\n## 验收标准\n"
        + "- commit 前最终 candidate_ref 已来自 image.edit preview 附加的视觉上下文或 vision.view_image 返回的视觉上下文。\n"
        + "- 最终图没有明显裁断主体,没有因为修边角而牺牲主体完整性。\n"
        + "- 最终图符合用户指定用途；若是图标,在小尺寸下仍可辨识,主体四周有稳定留白。\n"
        + "- 不合格候选已被丢弃,没有把明显错误候选继续作为 source_ref。\n"
        + "- 工具无法可靠完成时返回 blocked,说明已尝试的候选、失败原因和需要主 Agent 补充的信息。\n"
        + "\n\n## 编辑会话规则\n"
        + "- 把 `node:<node_id>` 或输入中的 source_ref 当作 base_ref 回退点。\n"
        + "- preview 得到的 candidate_ref 先作为候选；查看后判断为合格才作为 checkpoint 继续编辑或 commit。\n"
        + "- 查看后发现候选变差,从 base_ref 或最近合格 checkpoint 重新 preview。\n"
        + "- 每次最终 JSON 返回 operations_summary、verification、issues；completed 必须包含 committed_ref。"
    )


def _normalize_image_ref(value: Any) -> str:
    return str(value or "").strip()


def _candidate_record(
    *,
    ref: str,
    status: str,
    source_ref: str = "",
    reason: str = "",
    viewed: bool | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "ref": ref,
        "status": status,
    }
    if source_ref:
        record["source_ref"] = source_ref
    if reason:
        record["reason"] = _clip_text(reason, 260)
    if viewed is not None:
        record["viewed"] = bool(viewed)
    return record


def _new_image_editor_context_state(inputs: dict | None) -> dict[str, Any] | None:
    if not isinstance(inputs, dict):
        inputs = {}
    base_refs: list[str] = []
    node_id = _normalize_image_ref(inputs.get("node_id"))
    if node_id:
        base_refs.extend([node_id, f"node:{node_id}"])
    source_ref = _normalize_image_ref(inputs.get("source_ref"))
    if source_ref:
        base_refs.append(source_ref)
    candidate_ref = _normalize_image_ref(inputs.get("candidate_ref"))
    candidates: dict[str, dict[str, Any]] = {}
    checkpoints: list[str] = []
    if candidate_ref:
        candidates[candidate_ref] = _candidate_record(
            ref=candidate_ref,
            status="checkpoint",
            reason="initial_candidate_ref",
            viewed=False,
        )
        checkpoints.append(candidate_ref)
    return {
        "base_refs": _dedupe_refs(base_refs),
        "candidates": candidates,
        "active_candidate_ref": candidate_ref,
        "checkpoints": checkpoints,
        "rejected_refs": [],
        "deleted_refs": [],
        "committed_candidate_ref": "",
        "committed_ref": "",
        "events": [],
    }


def _dedupe_refs(values: list[Any] | tuple[Any, ...] | set[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        ref = _normalize_image_ref(value)
        if ref and ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def _append_image_editor_lifecycle_event(
    state: dict[str, Any] | None,
    *,
    ref: str,
    status: str,
    reason: str = "",
    source_ref: str = "",
) -> None:
    if state is None:
        return
    events = state.setdefault("events", [])
    if isinstance(events, list):
        events.append({
            "ref": ref,
            "status": status,
            "reason": _clip_text(reason, 260),
            "source_ref": source_ref,
        })
        del events[:-40]


def _mark_image_editor_candidate(
    state: dict[str, Any] | None,
    ref: str,
    status: str,
    *,
    source_ref: str = "",
    reason: str = "",
    viewed: bool | None = None,
) -> None:
    ref = _normalize_image_ref(ref)
    if not ref or state is None:
        return
    candidates = state.setdefault("candidates", {})
    if not isinstance(candidates, dict):
        candidates = {}
        state["candidates"] = candidates
    record = candidates.get(ref) if isinstance(candidates.get(ref), dict) else {"ref": ref}
    record["ref"] = ref
    record["status"] = status
    if source_ref:
        record["source_ref"] = source_ref
    if reason:
        record["reason"] = _clip_text(reason, 260)
    if viewed is not None:
        record["viewed"] = bool(viewed)
    candidates[ref] = record
    if status in {"preview", "viewed"}:
        state["active_candidate_ref"] = ref
    if status == "checkpoint":
        checkpoints = state.setdefault("checkpoints", [])
        if isinstance(checkpoints, list) and ref not in checkpoints:
            checkpoints.append(ref)
            del checkpoints[:-8]
        state["active_candidate_ref"] = ref
    if status == "committed":
        state["committed_candidate_ref"] = ref
        state["active_candidate_ref"] = ""
    if status == "rejected":
        rejected = state.setdefault("rejected_refs", [])
        if isinstance(rejected, list) and ref not in rejected:
            rejected.append(ref)
            del rejected[:-20]
        if state.get("active_candidate_ref") == ref:
            state["active_candidate_ref"] = ""
    _append_image_editor_lifecycle_event(
        state,
        ref=ref,
        status=status,
        reason=reason,
        source_ref=source_ref,
    )


def _message_image_refs(message: dict[str, Any]) -> set[str]:
    refs = message.get("_subagent_image_refs")
    if not isinstance(refs, list):
        return set()
    return {ref for ref in (_normalize_image_ref(item) for item in refs) if ref}


def _drop_subagent_image_context_for_refs(transcript: list[dict], refs: list[str]) -> int:
    targets = set(_dedupe_refs(refs))
    if not targets:
        return 0
    removed = 0
    for index in range(len(transcript) - 1, -1, -1):
        message = transcript[index]
        if not message.get("_subagent_model_content"):
            continue
        if _message_image_refs(message).isdisjoint(targets):
            continue
        del transcript[index]
        removed += 1
    return removed


def _subagent_visual_tail_image_count(visual_tail: list[dict]) -> int:
    count = 0
    for message in visual_tail:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        count += sum(1 for part in content if isinstance(part, dict) and part.get("type") == "image_url")
    return count


def _subagent_messages_for_call(stable_transcript: list[dict], visual_tail: list[dict]) -> list[dict]:
    """Compose an append-only transcript with volatile image context at the end."""
    if not visual_tail:
        return list(stable_transcript)
    return [*stable_transcript, *visual_tail]


async def _delete_rejected_image_editor_candidate_file(project_id: str, ref: str) -> bool:
    ref = _normalize_image_ref(ref)
    if not project_id or not ref:
        return False
    try:
        from app.services import image_operations

        path = await image_operations.resolve_image_path(project_id, ref)
        if not path or not path.exists() or not path.is_file():
            return False
        storage_root = image_operations.settings.storage_path_resolved / project_id / "generated_images" / "image_ops"
        resolved = path.resolve()
        root = storage_root.resolve()
        if root not in resolved.parents:
            return False
        if not resolved.name.startswith("edit-preview-"):
            return False
        resolved.unlink(missing_ok=True)
        return True
    except Exception:
        return False


async def _reject_image_editor_candidate(
    *,
    project_id: str,
    visual_tail: list[dict],
    state: dict[str, Any] | None,
    ref: str,
    reason: str,
) -> None:
    ref = _normalize_image_ref(ref)
    if not ref or state is None:
        return
    current = state.get("candidates", {}).get(ref) if isinstance(state.get("candidates"), dict) else {}
    if isinstance(current, dict) and current.get("status") == "committed":
        return
    removed_context = _drop_subagent_image_context_for_refs(visual_tail, [ref])
    deleted = await _delete_rejected_image_editor_candidate_file(project_id, ref)
    _mark_image_editor_candidate(state, ref, "rejected", reason=reason, viewed=current.get("viewed") if isinstance(current, dict) else None)
    if removed_context:
        state.setdefault("events", []).append({"ref": ref, "status": "context_removed", "count": removed_context})
    if deleted:
        deleted_refs = state.setdefault("deleted_refs", [])
        if isinstance(deleted_refs, list) and ref not in deleted_refs:
            deleted_refs.append(ref)
            del deleted_refs[:-20]


def _image_refs_from_vision_result(result: Any) -> list[str]:
    if not isinstance(result, dict):
        return []
    refs: list[str] = []
    images = result.get("images")
    if isinstance(images, list):
        for image in images:
            if not isinstance(image, dict):
                continue
            source = _normalize_image_ref(image.get("source"))
            node_id = _normalize_image_ref(image.get("node_id"))
            refs.extend([source, node_id, f"node:{node_id}" if node_id else ""])
    refs.extend([
        _normalize_image_ref(result.get("source")),
        _normalize_image_ref(result.get("node_id")),
        f"node:{_normalize_image_ref(result.get('node_id'))}" if _normalize_image_ref(result.get("node_id")) else "",
    ])
    return _dedupe_refs(refs)


def _image_editor_candidate_status_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    payload = parsed.get("candidate_status")
    if isinstance(payload, dict):
        return payload
    return {}


def _candidate_status_alias(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"accept", "accepted", "ok", "keep", "checkpoint", "good", "pass"}:
        return "checkpoint"
    if raw in {"reject", "rejected", "bad", "drop", "discard", "failed"}:
        return "rejected"
    return raw


def _tool_input_uses_ref(tool_input: dict[str, Any], ref: str) -> bool:
    ref = _normalize_image_ref(ref)
    if not ref:
        return False
    for key in ("source_ref", "candidate_ref", "source", "ref"):
        if _normalize_image_ref(tool_input.get(key)) == ref:
            return True
    values = tool_input.get("source_refs") or tool_input.get("sources")
    if isinstance(values, list):
        return ref in {_normalize_image_ref(item) for item in values}
    return False


async def _image_editor_before_tool_call(
    *,
    project_id: str,
    visual_tail: list[dict],
    state: dict[str, Any] | None,
    parsed: dict[str, Any],
    tool_name: str,
    tool_input: dict[str, Any],
) -> None:
    if state is None:
        return
    explicit = _image_editor_candidate_status_payload(parsed)
    explicit_ref = _normalize_image_ref(explicit.get("ref") or explicit.get("candidate_ref"))
    explicit_status = _candidate_status_alias(explicit.get("status"))
    explicit_reason = _normalize_image_ref(explicit.get("reason"))
    if explicit_ref and explicit_status == "checkpoint":
        _mark_image_editor_candidate(state, explicit_ref, "checkpoint", reason=explicit_reason or "model_marked_checkpoint", viewed=True)
    elif explicit_ref and explicit_status == "rejected":
        await _reject_image_editor_candidate(
            project_id=project_id,
            visual_tail=visual_tail,
            state=state,
            ref=explicit_ref,
            reason=explicit_reason or "model_marked_rejected",
        )

    active = _normalize_image_ref(state.get("active_candidate_ref"))
    if not active:
        return
    current = state.get("candidates", {}).get(active) if isinstance(state.get("candidates"), dict) else {}
    if isinstance(current, dict) and current.get("status") in {"checkpoint", "committed", "rejected"}:
        return

    if _tool_input_uses_ref(tool_input, active) and tool_name != "vision.view_image":
        _mark_image_editor_candidate(
            state,
            active,
            "checkpoint",
            reason=f"used_by_{tool_name}",
            viewed=current.get("viewed") if isinstance(current, dict) else None,
        )
        return

    if tool_name == "image.edit" and str(tool_input.get("action") or "preview").lower() == "preview":
        await _reject_image_editor_candidate(
            project_id=project_id,
            visual_tail=visual_tail,
            state=state,
            ref=active,
            reason="new_preview_started_from_different_source",
        )


def _image_editor_after_tool_call(
    *,
    state: dict[str, Any] | None,
    tool_name: str,
    tool_input: dict[str, Any],
    result: Any,
) -> None:
    if state is None or not isinstance(result, dict) or result.get("ok") is False:
        return
    if tool_name == "image.edit":
        action = str(result.get("action") or tool_input.get("action") or "preview").strip().lower()
        if action == "preview":
            ref = _normalize_image_ref(result.get("candidate_ref"))
            if ref:
                attached = bool(result.get("_model_content"))
                _mark_image_editor_candidate(
                    state,
                    ref,
                    "viewed" if attached else "preview",
                    source_ref=_normalize_image_ref(result.get("source_ref")),
                    reason="preview_created",
                    viewed=attached,
                )
        elif action == "commit":
            candidate_ref = _normalize_image_ref(tool_input.get("candidate_ref"))
            if candidate_ref:
                _mark_image_editor_candidate(state, candidate_ref, "committed", reason="commit_succeeded", viewed=True)
            committed_ref = _normalize_image_ref(result.get("local_url") or result.get("url"))
            if committed_ref:
                state["committed_ref"] = committed_ref
    elif tool_name == "vision.view_image":
        refs = _image_refs_from_vision_result(result)
        candidates = state.get("candidates") if isinstance(state.get("candidates"), dict) else {}
        for ref in refs:
            if ref in candidates:
                current = candidates.get(ref)
                current_status = str(current.get("status") or "") if isinstance(current, dict) else ""
                if current_status in {"checkpoint", "committed"}:
                    _mark_image_editor_candidate(state, ref, current_status, reason="viewed_by_vision", viewed=True)
                else:
                    _mark_image_editor_candidate(state, ref, "viewed", reason="viewed_by_vision", viewed=True)


def _image_editor_context_keep_refs(state: dict[str, Any] | None) -> set[str]:
    if state is None:
        return set()
    keep = set(_dedupe_refs(state.get("base_refs") if isinstance(state.get("base_refs"), list) else []))
    active = _normalize_image_ref(state.get("active_candidate_ref"))
    if active:
        keep.add(active)
    checkpoints = state.get("checkpoints")
    if isinstance(checkpoints, list):
        keep.update(_dedupe_refs(checkpoints[-2:]))
    committed = _normalize_image_ref(state.get("committed_candidate_ref"))
    if committed:
        keep.add(committed)
    return keep


def _prune_image_editor_model_content(transcript: list[dict], state: dict[str, Any] | None) -> None:
    image_messages = [
        index
        for index, message in enumerate(transcript)
        if message.get("_subagent_model_content")
        and isinstance(message.get("content"), list)
        and any(isinstance(part, dict) and part.get("type") == "image_url" for part in message["content"])
    ]
    if len(image_messages) <= SUBAGENT_IMAGE_CONTEXT_LIMIT:
        return
    keep_refs = _image_editor_context_keep_refs(state)
    keep_indexes: set[int] = set()
    if image_messages:
        keep_indexes.add(image_messages[0])
    for index in image_messages:
        if not _message_image_refs(transcript[index]).isdisjoint(keep_refs):
            keep_indexes.add(index)

    for index in reversed(image_messages):
        if len(keep_indexes) >= SUBAGENT_IMAGE_CONTEXT_LIMIT:
            break
        keep_indexes.add(index)

    removable_indexes = [index for index in image_messages if index not in keep_indexes]
    overflow = len(image_messages) - SUBAGENT_IMAGE_CONTEXT_LIMIT
    delete_indexes = removable_indexes[:overflow]
    if len(delete_indexes) < overflow:
        fallback = [index for index in image_messages[1:] if index not in delete_indexes]
        delete_indexes.extend(fallback[: overflow - len(delete_indexes)])
    for index in sorted(set(delete_indexes), reverse=True):
        del transcript[index]


def _image_editor_lifecycle_summary(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if state is None:
        return None
    candidates = state.get("candidates") if isinstance(state.get("candidates"), dict) else {}
    return {
        "base_refs": state.get("base_refs") or [],
        "active_candidate_ref": state.get("active_candidate_ref") or "",
        "checkpoints": state.get("checkpoints") or [],
        "rejected_refs": state.get("rejected_refs") or [],
        "deleted_refs": state.get("deleted_refs") or [],
        "committed_candidate_ref": state.get("committed_candidate_ref") or "",
        "committed_ref": state.get("committed_ref") or "",
        "candidates": list(candidates.values())[:30],
        "events": (state.get("events") or [])[-30:],
    }


def _subagent_model_content_parts(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    raw = result.get("_model_content")
    if not isinstance(raw, list):
        return []
    parts: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            text = str(item.get("text") or "")
            if text:
                parts.append({"type": "text", "text": text})
        elif kind == "image_url":
            image_url = item.get("image_url")
            if isinstance(image_url, dict):
                url = str(image_url.get("url") or "")
                if url:
                    payload = {"url": url}
                    detail = image_url.get("detail")
                    if detail not in (None, "", [], {}):
                        payload["detail"] = str(detail)
                    parts.append({"type": "image_url", "image_url": payload})
    return parts


def _subagent_model_content_refs(result: Any) -> list[str]:
    if not isinstance(result, dict):
        return []
    refs: list[Any] = []
    model_refs = result.get("_model_content_refs")
    if isinstance(model_refs, list):
        refs.extend(model_refs)
    refs.extend(_image_refs_from_vision_result(result))
    refs.extend([
        result.get("candidate_ref"),
        result.get("local_url"),
        result.get("url"),
    ])
    return _dedupe_refs(refs)


def _append_subagent_model_content(
    transcript: list[dict],
    parts: list[dict[str, Any]],
    *,
    role: str,
    refs: list[str] | None = None,
    image_editor_state: dict[str, Any] | None = None,
) -> None:
    if not parts:
        return
    transcript.append({
        "role": "user",
        "content": parts,
        "_subagent_model_content": True,
        "_subagent_image_refs": _dedupe_refs(refs or []),
    })
    if role == IMAGE_EDITOR_ROLE_NAME:
        _prune_image_editor_model_content(transcript, image_editor_state)


def _build_subagent_system(
    preset: dict[str, Any],
    task: str,
    inputs: dict | None,
) -> str:
    _ = (task, inputs)
    return "\n\n".join(
        str(section.get("text") or "").rstrip()
        for section in _build_subagent_system_sections(preset)
        if section.get("text")
    )


def _coerce_result_ref_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        return [str(item or "").strip() for item in value if str(item or "").strip()]
    return []


def _ensure_node_producer_completion_result(result_payload: dict[str, Any]) -> None:
    status = str(result_payload.get("status") or "").strip().lower()
    node_ids = _coerce_result_ref_list(result_payload.get("node_ids"))
    completed_node_ids = _coerce_result_ref_list(result_payload.get("completed_node_ids"))
    output_refs = _coerce_result_ref_list(result_payload.get("output_refs"))
    if node_ids and not isinstance(result_payload.get("node_ids"), list):
        result_payload["node_ids"] = node_ids
    if completed_node_ids and not isinstance(result_payload.get("completed_node_ids"), list):
        result_payload["completed_node_ids"] = completed_node_ids
    if output_refs and not isinstance(result_payload.get("output_refs"), list):
        result_payload["output_refs"] = output_refs
    if status == "completed" and not (node_ids or completed_node_ids or output_refs):
        result_payload["status"] = "blocked"
        result_payload["blocked_reason"] = "node_producer finished without node_ids, completed_node_ids, or output_refs."
        issues = result_payload.get("issues")
        if not isinstance(issues, list):
            issues = []
        issues.append("node_producer finished without node_ids, completed_node_ids, or output_refs.")
        result_payload["issues"] = issues


def _workflow_spec_input_values(inputs: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    raw_inputs = inputs.get("inputs") if isinstance(inputs.get("inputs"), dict) else {}
    raw_context = inputs.get("context") if isinstance(inputs.get("context"), dict) else {}
    values.update(raw_inputs)
    for key in ("facts", "user_facts", "known_inputs"):
        if isinstance(inputs.get(key), dict):
            values.update(inputs[key])
    if raw_context:
        values["context"] = raw_context
    return values


def _workflow_spec_validation_error(message: str) -> dict[str, Any]:
    return {
        "status": "blocked",
        "blocked_reason": message,
        "validation": {"ok": False, "error": message},
        "issues": [message],
    }


def _workflow_spec_strip_child_questions(result: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(result)
    cleaned.pop("missing_questions", None)
    cleaned.pop("input_questions", None)
    cleaned.pop("missing_inputs", None)
    cleaned.pop("run_ready", None)
    cleaned.pop("input_schema", None)
    cleaned.pop("input_fields", None)
    cleaned.pop("known_input_values", None)
    return cleaned


def _workflow_spec_has_explicit_workflow_source(task: str, inputs: dict[str, Any]) -> bool:
    explicit_keys = {
        "template_id",
        "template",
        "artifact_ref",
        "workflow_skill",
        "workflow_skill_name",
        "skill_name",
        "skill_key",
        "current_workflow",
    }

    def contains_explicit(value: Any) -> bool:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key or "").strip() in explicit_keys and item not in (None, "", [], {}):
                    return True
                if contains_explicit(item):
                    return True
        elif isinstance(value, list):
            return any(contains_explicit(item) for item in value)
        return False

    if contains_explicit(inputs):
        return True
    task_text = str(task or "")
    if "workflow_spec:" in task_text:
        return True
    try:
        template_ids = [str(item.get("id") or "") for item in canvas_workflow_templates.list_template_summaries()]
    except Exception:
        template_ids = []
    return any(template_id and template_id in task_text for template_id in template_ids)


def _workflow_spec_normalize_default_template(
    *,
    task: str,
    inputs: dict[str, Any],
    result_payload: dict[str, Any],
) -> dict[str, Any]:
    template_id = str(result_payload.get("template_id") or "").strip()
    if not template_id:
        return result_payload
    default_id = canvas_workflow_templates.DEFAULT_WORKFLOW_TEMPLATE_ID
    if template_id == default_id or _workflow_spec_has_explicit_workflow_source(task, inputs):
        return result_payload
    try:
        canvas_workflow_templates.get_template(default_id)
    except canvas_workflow_templates.WorkflowTemplateError:
        return result_payload
    normalized = dict(result_payload)
    normalized["template_id"] = default_id
    normalized["version_id"] = ""
    normalized.pop("artifact_ref", None)
    if str(normalized.get("status") or "").strip().lower() != "blocked":
        normalized["decision"] = "reuse_existing"
    return normalized


def _workflow_spec_public_preview(source: dict[str, Any]) -> dict[str, Any]:
    preview = source if isinstance(source, dict) else {}
    allowed = (
        "id",
        "name",
        "description",
        "category",
        "scope",
        "source",
        "version",
        "active_version_id",
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
    )
    result: dict[str, Any] = {}
    for key in allowed:
        value = preview.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, str):
            result[key] = _clip_text(value, 240)
        else:
            result[key] = deepcopy(value)
    return result


_WORKFLOW_SPEC_INPUT_FIELD_PRIVATE_KEYS = {
    "missing",
    "missing_input",
    "missing_inputs",
    "missing_question",
    "missing_questions",
    "input_question",
    "input_questions",
    "question",
    "questions",
    "header",
    "provided",
    "provided_value",
    "current_value",
    "value",
    "run_ready",
    "known",
    "is_known",
    "blocking",
    "blocker",
    "known_value",
    "known_input_values",
    "recommended_default",
    "requires_user_input",
}


def _workflow_spec_public_input_value(value: Any, *, key: str = "") -> Any:
    if isinstance(value, str):
        limit = 360 if key in {"description", "help", "hint"} else 180
        return _clip_text(value, limit)
    if isinstance(value, dict):
        return {
            item_key: _workflow_spec_public_input_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
            if str(item_key) not in _WORKFLOW_SPEC_INPUT_FIELD_PRIVATE_KEYS
            and item_value not in (None, "", [], {})
        }
    if isinstance(value, list):
        items: list[Any] = []
        for item in value[:12]:
            if isinstance(item, dict):
                cleaned_item = _workflow_spec_public_input_value(item)
                if cleaned_item:
                    items.append(cleaned_item)
            elif item not in (None, "", [], {}):
                items.append(_workflow_spec_public_input_value(item, key=key))
        return items
    return deepcopy(value)


def _workflow_spec_public_input_fields(template: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        fields = canvas_workflow_templates.template_input_field_summaries(template, {})
    except Exception:
        fields = []
    result: list[dict[str, Any]] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        cleaned: dict[str, Any] = {}
        for key, value in field.items():
            key_text = str(key or "").strip()
            if not key_text or key_text in _WORKFLOW_SPEC_INPUT_FIELD_PRIVATE_KEYS:
                continue
            if value in (None, "", [], {}):
                continue
            cleaned[key_text] = _workflow_spec_public_input_value(value, key=key_text)
        if cleaned.get("id"):
            result.append(cleaned)
        if len(result) >= 12:
            break
    return result


def _workflow_spec_public_validation(
    validation: dict[str, Any],
    *,
    workflow_id: str,
    step_count: int,
    protocol: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": bool(validation.get("ok", True)),
        "workflow_id": workflow_id,
        "step_count": int(step_count or 0),
        "protocol": {
            key: value
            for key, value in (protocol or {}).items()
            if key in {"workflow_spec_version", "protocol_version", "supported"} and value not in (None, "", [], {})
        },
    }


def _workflow_spec_selector_boundary_error(reason: str) -> dict[str, Any]:
    return _workflow_spec_validation_error(
        reason
        or "workflow_spec 只选择已有工作流模板。"
    )


def _finalize_workflow_spec_result(
    *,
    project_id: str,
    task: str,
    inputs: dict[str, Any],
    result_payload: dict[str, Any],
) -> dict[str, Any]:
    result_payload = _workflow_spec_normalize_default_template(
        task=task,
        inputs=inputs,
        result_payload=result_payload,
    )
    decision = str(result_payload.get("decision") or "").strip().lower()
    if result_payload.get("workflow") not in (None, "", [], {}) or result_payload.get("spec") not in (None, "", [], {}):
        return _workflow_spec_selector_boundary_error(
            "workflow_spec 只选择现有模板，不能返回新 workflow/spec。"
        )
    artifact_ref = str(result_payload.get("artifact_ref") or "").strip()
    if artifact_ref:
        return _workflow_spec_selector_boundary_error(
            "workflow_spec 只选择现有模板，不能返回 artifact_ref。"
        )
    if decision in {"patch_existing", "compile_new"}:
        return _workflow_spec_selector_boundary_error(
            "workflow_spec 只选择现有模板，不能 patch_existing 或 compile_new。"
        )
    status = str(result_payload.get("status") or "").strip().lower()
    input_values = _workflow_spec_input_values(inputs)
    if status == "blocked":
        has_template_or_spec = any(
            result_payload.get(key) not in (None, "", [], {})
            for key in ("template_id", "artifact_ref", "workflow", "spec")
        )
        decision = str(result_payload.get("decision") or "").strip().lower()
        if has_template_or_spec and decision == "ask_user":
            if result_payload.get("template_id"):
                result_payload = {**result_payload, "status": "completed", "_normalized_child_ask_user": True}
                result_payload["decision"] = "reuse_existing"
                status = "completed"
            else:
                result = dict(result_payload)
                result.pop("workflow", None)
                result.pop("spec", None)
                return _workflow_spec_strip_child_questions(result)
        else:
            result = dict(result_payload)
            result.pop("workflow", None)
            result.pop("spec", None)
            return _workflow_spec_strip_child_questions(result)

    template_id = str(result_payload.get("template_id") or "").strip()
    if template_id and not str(result_payload.get("artifact_ref") or "").strip():
        try:
            template = canvas_workflow_templates.get_template(
                template_id,
                input_values=input_values,
            )
        except canvas_workflow_templates.WorkflowTemplateError as exc:
            return _workflow_spec_validation_error(f"workflow template 读取失败: {exc}")
        self_check = result_payload.get("self_check") if isinstance(result_payload.get("self_check"), dict) else {}
        if self_check.get("passed") is False:
            issues = self_check.get("issues") if isinstance(self_check.get("issues"), list) else []
            message = "workflow_spec 子 Agent 自检未通过。"
            if issues:
                message = f"{message} " + "; ".join(str(item) for item in issues[:3])
            return _workflow_spec_validation_error(message)
        result_preview = result_payload.get("preview") if isinstance(result_payload.get("preview"), dict) else {}
        public_spec = template.get("public_spec") if isinstance(template.get("public_spec"), dict) else template
        template_preview = workflow_spec_artifacts.workflow_spec_preview(public_spec, normalized=template)
        preview = {**template_preview, **result_preview}
        validation = result_payload.get("validation") if isinstance(result_payload.get("validation"), dict) else {}
        if validation.get("ok") is False and result_payload.get("_normalized_child_ask_user"):
            validation = {}
        if not validation:
            validation = {
                "ok": True,
                "workflow_id": template.get("id"),
                "step_count": len(template.get("steps") or []),
                "protocol": {
                    "schema": public_spec.get("schema"),
                    "execution_plan_version": template.get("schema"),
                    "plan_hash": template.get("plan_hash"),
                    "requirements": template.get("requirements") or {},
                },
            }
        return {
            "status": "completed",
            "decision": str(result_payload.get("decision") or "reuse_existing").strip() or "reuse_existing",
            "template_id": template_id,
            "version_id": str(result_payload.get("version_id") or template.get("active_version_id") or "").strip(),
            "preview": _workflow_spec_public_preview(preview),
            "input_fields": _workflow_spec_public_input_fields(template),
            "validation": _workflow_spec_public_validation(
                validation,
                workflow_id=str(template.get("id") or template_id),
                step_count=len(template.get("steps") or []),
                protocol=validation.get("protocol") if isinstance(validation.get("protocol"), dict) else {
                    "workflow_spec_version": template.get("workflow_spec_version"),
                },
            ),
            "next_action": "主 Agent 使用 input_fields、用户原话和历史状态判断是否提问；输入齐全后用 template_id 运行 workflow。",
        }

    artifact_ref = str(result_payload.get("artifact_ref") or "").strip()
    if artifact_ref:
        return _workflow_spec_selector_boundary_error(
            "workflow_spec 只选择现有模板，不能返回 artifact_ref。"
        )

    workflow = result_payload.get("workflow")
    if workflow is None:
        workflow = result_payload.get("spec")
    if isinstance(workflow, dict):
        return _workflow_spec_selector_boundary_error(
            "workflow_spec 只选择现有模板，不能返回新 workflow/spec。"
        )
    return _workflow_spec_validation_error("workflow_spec 子 Agent 未返回 template_id。")


async def _record_workflow_spec_authorized_ref(
    *,
    project_id: str,
    result_payload: dict[str, Any],
    task: str,
) -> None:
    if str(result_payload.get("status") or "").strip().lower() != "completed":
        return
    template_id = str(result_payload.get("template_id") or "").strip()
    if not template_id:
        return
    record = {
        "template_id": template_id,
        "artifact_ref": "",
        "decision": str(result_payload.get("decision") or "").strip(),
        "version_id": str(result_payload.get("version_id") or "").strip(),
        "input_fields": result_payload.get("input_fields") if isinstance(result_payload.get("input_fields"), list) else [],
        "authorized_by": WORKFLOW_SPEC_ROLE_NAME,
        "authorized_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "task_hash": _stable_hash(task or ""),
    }
    try:
        async with session_scope() as session:
            project = await session.get(Project, project_id)
            if project is None:
                return
            try:
                state = json.loads(project.state_json or "{}")
            except json.JSONDecodeError:
                state = {}
            if not isinstance(state, dict):
                state = {}
            refs = state.get("_workflow_spec_authorized_refs")
            if not isinstance(refs, list):
                refs = []
            refs = [item for item in refs if isinstance(item, dict)]
            refs.append(record)
            state["_workflow_spec_authorized_refs"] = refs[-20:]
            project.state_json = json.dumps(state, ensure_ascii=False)
            session.add(project)
            await session.commit()
    except Exception:
        return


def _subagent_progress_text(parsed: dict[str, Any], *, role: str, step_no: int, tool_name: str = "") -> str:
    text = str(parsed.get("commentary") or parsed.get("progress") or "").strip()
    return _clip_text(text, 180)


async def _emit_subagent_progress(
    *,
    project_id: str,
    role: str,
    step_no: int,
    content: str,
    tool_name: str = "",
    status: str = "running",
) -> None:
    if not project_id or not content:
        return
    try:
        from app.agent.orchestrator import emit_canvas_event

        await emit_canvas_event(
            {
                "type": "subagent_round",
                "agent": role,
                "step": step_no,
                "content": content,
                "tool": tool_name or None,
                "status": status,
                "source": "model",
            },
            project_id=project_id,
        )
    except Exception:
        pass


def _scope_denied(reason: str, *, tool_name: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error": reason,
        "error_kind": "subagent_scope_denied",
        "tool": tool_name,
    }
    if details:
        payload.update(details)
    return payload


def _coerce_boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value or "").strip().lower()
    return raw in {"1", "true", "yes", "y", "on", "allow", "allowed", "create"}


def _iter_scope_values(value: Any) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, list):
                return _iter_scope_values(parsed)
        return [item for item in re.split(r"[\s,，;；]+", text) if item]
    if isinstance(value, dict):
        for key in ("node_id", "id", "ref", "node"):
            item = str(value.get(key) or "").strip()
            if item:
                return [item]
        return []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(_iter_scope_values(item))
        return out
    text = str(value or "").strip()
    return [text] if text else []


def _normalize_scope_node_id(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("node:"):
        text = text[5:].strip()
    if text.startswith("#"):
        text = text[1:].strip()
    return text


def _scope_node_ids_from_inputs(inputs: dict | None) -> set[str]:
    if not isinstance(inputs, dict):
        return set()
    raw: list[str] = []
    for key in (
        "node_id",
        "node_ids",
        "target_node_id",
        "target_node_ids",
        "scoped_node_id",
        "scoped_node_ids",
        "existing_node_ids",
    ):
        raw.extend(_iter_scope_values(inputs.get(key)))
    return {item for item in (_normalize_scope_node_id(value) for value in raw) if item}


def _allowed_node_types_from_inputs(role: str, inputs: dict | None) -> set[str]:
    if not isinstance(inputs, dict):
        return set(NODE_PRODUCER_NODE_TYPES)
    raw: list[str] = []
    for key in ("allowed_node_types", "node_types", "target_node_types"):
        raw.extend(_iter_scope_values(inputs.get(key)))
    for key in ("node_type", "target_node_type", "type"):
        value = str(inputs.get(key) or "").strip()
        if value:
            raw.append(value)
    allowed = {str(item or "").strip().lower() for item in raw if str(item or "").strip()}
    allowed = {item for item in allowed if item in NODE_PRODUCER_NODE_TYPES}
    return allowed or set(NODE_PRODUCER_NODE_TYPES)


async def _resolve_scope_node_ids(project_id: str, node_ids: set[str]) -> set[str]:
    if not project_id or not node_ids:
        return set()
    from app.mcp_tools import node_universal

    resolved: set[str] = set()
    for node_id in node_ids:
        try:
            internal_id = await node_universal._resolve_agent_node_id(project_id, node_id)  # noqa: SLF001
        except Exception:
            internal_id = ""
        if internal_id:
            resolved.add(internal_id)
    return resolved


async def _new_subagent_write_scope(
    *,
    project_id: str,
    role: str,
    inputs: dict | None,
) -> dict[str, Any] | None:
    if role != NODE_PRODUCER_ROLE_NAME:
        return None
    scoped_node_ids = _scope_node_ids_from_inputs(inputs)
    allowed_types = _allowed_node_types_from_inputs(role, inputs)
    allow_create = False
    if isinstance(inputs, dict):
        allow_create = allow_create or any(
            _coerce_boolish(inputs.get(key))
            for key in ("allow_create", "create_if_missing", "allow_new_nodes", "create_node")
        )
    return {
        "role": role,
        "allowed_node_ids": set(scoped_node_ids),
        "allowed_resolved_node_ids": await _resolve_scope_node_ids(project_id, scoped_node_ids),
        "created_node_ids": set(),
        "created_public_node_ids": set(),
        "allowed_node_types": allowed_types,
        "allow_create": allow_create,
        "require_node_scope": True,
    }


def _node_within_subagent_scope(scope: dict[str, Any] | None, *, node_id: str, resolved_node_id: str) -> bool:
    if not scope:
        return True
    raw = _normalize_scope_node_id(node_id)
    allowed_raw = set(scope.get("allowed_node_ids") or set())
    allowed_resolved = set(scope.get("allowed_resolved_node_ids") or set())
    created_raw = set(scope.get("created_public_node_ids") or set())
    created_resolved = set(scope.get("created_node_ids") or set())
    if raw and (raw in allowed_raw or raw in created_raw):
        return True
    if resolved_node_id and (resolved_node_id in allowed_resolved or resolved_node_id in created_resolved):
        return True
    if not bool(scope.get("require_node_scope")):
        return True
    return False


def _record_subagent_created_nodes(scope: dict[str, Any] | None, result: Any) -> None:
    if not scope or not isinstance(result, dict):
        return
    nodes: list[dict[str, Any]] = []
    if isinstance(result.get("nodes"), list):
        nodes.extend(item for item in result["nodes"] if isinstance(item, dict))
    if result.get("id") or result.get("_canvas_id") or result.get("_canvas_node_id"):
        nodes.append(result)
    for node in nodes:
        internal_id = str(node.get("_canvas_id") or node.get("_canvas_node_id") or "").strip()
        public_id = str(node.get("id") or node.get("node_id") or "").strip()
        if internal_id:
            scope.setdefault("created_node_ids", set()).add(internal_id)
        if public_id:
            scope.setdefault("created_public_node_ids", set()).add(_normalize_scope_node_id(public_id))


def _node_ids_from_subagent_update(tool_input: dict[str, Any]) -> list[str]:
    from app.mcp_tools import node_universal

    ids = node_universal._normalize_node_id_list(  # noqa: SLF001 - role guard mirrors node.update resolution.
        str(tool_input.get("node_id") or ""),
        tool_input.get("node_ids"),
    )
    updates = tool_input.get("updates")
    if isinstance(updates, list):
        for item in updates:
            if isinstance(item, dict):
                node_id = str(item.get("node_id") or "").strip()
                if node_id and node_id not in ids:
                    ids.append(node_id)
    return ids


async def _node_for_subagent_scope(project_id: str, node_id: str) -> tuple[str, str, dict[str, Any] | None]:
    from app.mcp_tools import canvas_tools, node_universal

    resolved = await node_universal._resolve_agent_node_id(project_id, node_id)  # noqa: SLF001
    if not resolved:
        return "", "", _scope_denied(
            "无法解析节点编号，不能执行生产型子 Agent 写入。",
            tool_name="node",
            details={"node_id": node_id},
        )
    node = await canvas_tools.get_node(resolved)
    if not isinstance(node, dict) or node.get("error"):
        return "", "", _scope_denied(
            "节点不存在，不能执行生产型子 Agent 写入。",
            tool_name="node",
            details={"node_id": node_id, "resolved_node_id": resolved},
        )
    if project_id and str(node.get("project_id") or "") != project_id:
        return "", "", _scope_denied(
            "节点不属于当前项目，不能执行生产型子 Agent 写入。",
            tool_name="node",
            details={"node_id": node_id},
        )
    return resolved, str(node.get("type") or ""), None


async def _node_type_for_subagent_scope(project_id: str, node_id: str) -> tuple[str, dict[str, Any] | None]:
    _, node_type, error = await _node_for_subagent_scope(project_id, node_id)
    return node_type, error


def _requested_node_create_types(tool_input: dict[str, Any]) -> list[str]:
    requested_types: list[str] = []
    single_type = str(tool_input.get("type") or "").strip().lower()
    if single_type:
        requested_types.append(single_type)
    nodes = tool_input.get("nodes")
    if isinstance(nodes, list):
        for item in nodes:
            if isinstance(item, dict):
                requested_types.append(str(item.get("type") or "").strip().lower())
    return [item for item in requested_types if item]


def _subagent_patch_changes_type(tool_input: dict[str, Any]) -> bool:
    patch = tool_input.get("patch")
    if isinstance(patch, dict) and "type" in patch:
        return True
    updates = tool_input.get("updates")
    if isinstance(updates, list):
        for item in updates:
            if isinstance(item, dict) and isinstance(item.get("patch"), dict) and "type" in item["patch"]:
                return True
    return False


async def _validate_node_producer_tool_scope(
    project_id: str,
    role: str,
    tool_name: str,
    tool_input: dict[str, Any],
    scope: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if scope is None:
        scope = await _new_subagent_write_scope(project_id=project_id, role=role, inputs={})
    allowed_types = set((scope or {}).get("allowed_node_types") or NODE_PRODUCER_NODE_TYPES)
    role_label = "node_producer"

    if tool_name == "node.create":
        if not bool((scope or {}).get("allow_create")):
            return _scope_denied(
                "node_producer 没有 allow_create/create_if_missing 时不能新建节点；主 Agent 应先搭框架或明确授权创建。",
                tool_name=tool_name,
            )
        requested_types = _requested_node_create_types(tool_input)
        invalid = [item for item in requested_types if item and item not in allowed_types]
        if invalid:
            allowed_label = ", ".join(sorted(allowed_types))
            return _scope_denied(
                f"{role_label} 只能创建允许类型节点: {allowed_label}。",
                tool_name=tool_name,
                details={"requested_types": invalid, "allowed_node_types": sorted(allowed_types)},
            )
        if not requested_types and len(allowed_types) == 1:
            tool_input["type"] = next(iter(allowed_types))
        return None

    if tool_name == "node.update":
        if _subagent_patch_changes_type(tool_input):
            return _scope_denied(
                f"{role_label} 不能修改节点 type。",
                tool_name=tool_name,
            )
        node_ids = _node_ids_from_subagent_update(tool_input)
        if not node_ids:
            return _scope_denied(
                f"{role_label} 更新节点时必须指定 node_id。",
                tool_name=tool_name,
            )
        for node_id in node_ids:
            resolved_id, node_type, error = await _node_for_subagent_scope(project_id, node_id)
            if error:
                error["tool"] = tool_name
                return error
            if node_type not in allowed_types:
                return _scope_denied(
                    f"{role_label} 只能更新允许类型节点。",
                    tool_name=tool_name,
                    details={"node_id": node_id, "node_type": node_type, "allowed_node_types": sorted(allowed_types)},
                )
            if not _node_within_subagent_scope(scope, node_id=node_id, resolved_node_id=resolved_id):
                return _scope_denied(
                    "node_producer 只能更新主 Agent 指定作用域内节点，或本次委派中自己创建的节点。",
                    tool_name=tool_name,
                    details={"node_id": node_id, "node_type": node_type},
                )
        return None

    if tool_name == "node.run":
        node_id = str(tool_input.get("node_id") or "").strip()
        if not node_id:
            return _scope_denied(
                f"{role_label} 运行节点时必须指定 node_id。",
                tool_name=tool_name,
            )
        resolved_id, node_type, error = await _node_for_subagent_scope(project_id, node_id)
        if error:
            error["tool"] = tool_name
            return error
        if node_type not in allowed_types:
            return _scope_denied(
                f"{role_label} 只能运行允许类型节点。",
                tool_name=tool_name,
                details={"node_id": node_id, "node_type": node_type, "allowed_node_types": sorted(allowed_types)},
            )
        if not _node_within_subagent_scope(scope, node_id=node_id, resolved_node_id=resolved_id):
            return _scope_denied(
                "node_producer 只能运行主 Agent 指定作用域内节点，或本次委派中自己创建的节点。",
                tool_name=tool_name,
                details={"node_id": node_id, "node_type": node_type},
            )
        return None

    return None


async def _validate_subagent_tool_scope(
    *,
    project_id: str,
    role: str,
    tool_name: str,
    tool_input: dict[str, Any],
    inputs: dict | None = None,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if role == NODE_PRODUCER_ROLE_NAME:
        return await _validate_node_producer_tool_scope(project_id, role, tool_name, tool_input, scope)
    return None


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
        denied_trace = [
            {
                "agent": role,
                "step": 0,
                "event": "tool_denied",
                "tool": name,
                "error": "readonly_tool_denied",
            }
            for name in denied_tools
        ]
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
            "_subagent_trace": denied_trace,
        }
    prompt_package = _build_subagent_prompt_package(role, preset, task, inputs)
    system = str(prompt_package["system"])
    transcript: list[dict] = [{"role": "user", "content": str(prompt_package["task_message"])}]
    visual_tail: list[dict] = []
    image_editor_state = _new_image_editor_context_state(inputs) if role == IMAGE_EDITOR_ROLE_NAME else None
    write_scope = await _new_subagent_write_scope(project_id=project_id, role=role, inputs=inputs)
    tool_log: list[dict] = []
    usage_log: list[dict] = []
    trace_log: list[dict[str, Any]] = []
    enforce_max_steps = bool(preset.get("enforce_max_steps"))
    step_limit = _effective_subagent_step_limit(role=role, preset=preset, max_steps=max_steps)
    subagent_dump_run_id = f"subagent_{role}_{new_run_id()}"

    async with session_scope() as session:
        svc = LLMService(session)

        step_no = 0
        tools = list(prompt_package["tools"])
        allowed_tool_set = set(preset.get("allowed_tools") or [])
        while True:
            if enforce_max_steps and step_no >= step_limit:
                _append_subagent_trace(
                    trace_log,
                    role=role,
                    step=step_no,
                    event="step_limit",
                    step_limit=step_limit,
                    candidate_lifecycle=_image_editor_lifecycle_summary(image_editor_state),
                )
                return {
                    "role": role,
                    "task": task,
                    "result": {
                        "status": "blocked",
                        "committed": False,
                        "issues": [f"子 Agent 达到最大步骤数 {step_limit}，未完成任务。"],
                        "candidate_lifecycle": _image_editor_lifecycle_summary(image_editor_state),
                    },
                    "summary": f"子 Agent 达到最大步骤数 {step_limit}，未完成任务。",
                    "steps_used": step_no,
                    "tool_log": tool_log,
                    "_subagent_usage": usage_log,
                    "_subagent_trace": trace_log,
                    "error": "subagent_step_limit",
                    "allowed_tools": preset.get("allowed_tools", []),
                }
            step_no += 1
            messages_for_call = _subagent_messages_for_call(transcript, visual_tail)
            dump_llm_request(
                project_id=project_id,
                run_id=subagent_dump_run_id,
                iteration=step_no - 1,
                system=system,
                messages=messages_for_call,
                tools=tools,
                user_message=task if step_no == 1 else None,
                prompt_assembly={
                    **prompt_package["diagnostics"],
                    "step": step_no,
                    "stable_message_count": len(transcript),
                    "visual_tail_message_count": len(visual_tail),
                    "visual_tail_images": _subagent_visual_tail_image_count(visual_tail),
                },
            )
            llm_kwargs = {
                "task_type": str(preset.get("task_type") or "agent_loop"),
                "messages": messages_for_call,
                "tools": tools,
                "system": system,
                "project_id": project_id,
            }
            if preset.get("max_output_tokens") is not None:
                llm_kwargs["max_tokens"] = int(preset["max_output_tokens"])
            response = await svc.generate_with_tools(**llm_kwargs)
            choice = response.choices[0]
            msg = choice.message
            raw = str(getattr(msg, "content", None) or "")
            tool_calls = list(getattr(msg, "tool_calls", None) or [])
            usage = build_usage_snapshot(
                response,
                messages=messages_for_call,
                system=system,
                tools=tools,
            )
            if isinstance(usage, dict) and usage:
                usage_log.append({
                    "agent": role,
                    "step": step_no,
                    "prompt_cache_key": prompt_package["cache_key"],
                    "prompt_schema_version": SUBAGENT_PROMPT_SCHEMA_VERSION,
                    "usage": usage,
                })
            response_payload = _parse_json_object(raw) or {"commentary": raw}
            _append_subagent_trace(
                trace_log,
                role=role,
                step=step_no,
                event="model_response",
                transition_reason="tool_calls" if tool_calls else "final_json",
                tool_call_count=len(tool_calls),
                has_text=bool(raw),
                commentary=response_payload.get("commentary") or response_payload.get("progress") or raw,
                summary=response_payload.get("summary"),
                raw_chars=len(raw),
                finish_reason=str(getattr(choice, "finish_reason", "") or ""),
                model=usage.get("model"),
                prompt_cache_key=prompt_package["cache_key"],
                stable_system_hash=prompt_package["diagnostics"].get("stable_system_hash"),
                stable_message_count=len(transcript),
                visual_tail_message_count=len(visual_tail),
                visual_tail_images=_subagent_visual_tail_image_count(visual_tail),
            )

            if not tool_calls:
                transcript.append({"role": "assistant", "content": raw})
                parsed_final = _parse_subagent_final_result(raw)
                if not parsed_final:
                    _append_subagent_trace(
                        trace_log,
                        role=role,
                        step=step_no,
                        event="model_response_invalid_final_json",
                        raw_chars=len(raw),
                        content_preview=_clip_text(raw, 280),
                    )
                    transcript.append({
                        "role": "user",
                        "content": (
                            "上一步没有调用工具，也不是合法最终 JSON。"
                            '请继续调用白名单工具，或输出 {"status":"completed|blocked","summary":"...","result":{...}}。'
                        ),
                    })
                    continue

                finish_summary = _clip_text(parsed_final.get("summary"), 180)
                if finish_summary:
                    await _emit_subagent_progress(
                        project_id=project_id,
                        role=role,
                        step_no=step_no,
                        content=finish_summary,
                        status="completed",
                    )
                result_payload = parsed_final.get("result")
                if role == IMAGE_EDITOR_ROLE_NAME and isinstance(result_payload, dict):
                    result_payload.setdefault("candidate_lifecycle", _image_editor_lifecycle_summary(image_editor_state))
                    lifecycle = _image_editor_lifecycle_summary(image_editor_state) or {}
                    committed_ref = _normalize_image_ref(
                        result_payload.get("committed_ref")
                        or result_payload.get("local_url")
                        or lifecycle.get("committed_ref")
                    )
                    if committed_ref and not result_payload.get("committed_ref"):
                        result_payload["committed_ref"] = committed_ref
                    status = str(result_payload.get("status") or "").strip().lower()
                    if status == "completed" and not committed_ref and result_payload.get("committed") is not True:
                        result_payload["status"] = "blocked"
                        result_payload["committed"] = False
                        issues = result_payload.get("issues")
                        if not isinstance(issues, list):
                            issues = []
                        issues.append("image_editor finished without a committed image.")
                        result_payload["issues"] = issues
                if role == NODE_PRODUCER_ROLE_NAME and isinstance(result_payload, dict):
                    _ensure_node_producer_completion_result(result_payload)
                _append_subagent_trace(
                    trace_log,
                    role=role,
                    step=step_no,
                    event="finish",
                    result_status=(result_payload or {}).get("status") if isinstance(result_payload, dict) else "",
                    committed=(result_payload or {}).get("committed") if isinstance(result_payload, dict) else None,
                    committed_ref=(result_payload or {}).get("committed_ref") if isinstance(result_payload, dict) else "",
                    candidate_lifecycle=_image_editor_lifecycle_summary(image_editor_state),
                )
                return {
                    "role": role,
                    "task": task,
                    "result": result_payload,
                    "summary": parsed_final.get("summary", ""),
                    "steps_used": step_no,
                    "tool_log": tool_log,
                    "_subagent_usage": usage_log,
                    "_subagent_trace": trace_log,
                    "error": "",
                }

            transcript.append(_assistant_message_payload(msg))
            for index, tool_call in enumerate(tool_calls):
                tool_call_id = _tool_call_id(tool_call, f"subagent-call-{step_no}-{index}")
                llm_tool_name, raw_arguments = _tool_call_function_payload(tool_call)
                tool_name = registry.resolve_tool_name(llm_tool_name)
                tool_input = _coerce_tool_arguments(raw_arguments)

                if tool_name not in allowed_tool_set:
                    denial = f"工具 {tool_name!r} 不在白名单。允许:{preset['allowed_tools']}"
                    tool_log.append({
                        "tool": tool_name, "ok": False, "error": "denied", "step": step_no,
                    })
                    _append_subagent_trace(
                        trace_log,
                        role=role,
                        step=step_no,
                        event="tool_denied",
                        tool=tool_name,
                        allowed_tools=preset["allowed_tools"],
                    )
                    transcript.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": json.dumps({"ok": False, "error": denial}, ensure_ascii=False),
                    })
                    continue

                if role == IMAGE_EDITOR_ROLE_NAME:
                    await _image_editor_before_tool_call(
                        project_id=project_id,
                        visual_tail=visual_tail,
                        state=image_editor_state,
                        parsed=response_payload,
                        tool_name=tool_name,
                        tool_input=tool_input,
                    )

                scope_error = await _validate_subagent_tool_scope(
                    project_id=project_id,
                    role=role,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    inputs=inputs,
                    scope=write_scope,
                )
                if scope_error is not None:
                    tool_log.append({
                        "tool": tool_name,
                        "input": tool_input,
                        "ok": False,
                        "error": scope_error.get("error"),
                        "step": step_no,
                    })
                    _append_subagent_trace(
                        trace_log,
                        role=role,
                        step=step_no,
                        event="tool_scope_denied",
                        tool=tool_name,
                        error=scope_error.get("error"),
                        error_kind=scope_error.get("error_kind"),
                        input=tool_input,
                    )
                    transcript.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": json.dumps(scope_error, ensure_ascii=False),
                    })
                    continue

                progress_text = _subagent_progress_text(response_payload, role=role, step_no=step_no, tool_name=tool_name)
                await _emit_subagent_progress(
                    project_id=project_id,
                    role=role,
                    step_no=step_no,
                    content=progress_text,
                    tool_name=tool_name,
                    status="running",
                )

                if _subagent_tool_accepts_project_id(tool_name):
                    tool_input["project_id"] = project_id
                try:
                    _append_subagent_trace(
                        trace_log,
                        role=role,
                        step=step_no,
                        event="tool_call_requested",
                        tool=tool_name,
                        input=tool_input,
                        candidate_status=_image_editor_candidate_status_payload(response_payload),
                    )
                    result = await registry.call(tool_name, **tool_input)
                    if tool_name == "node.create":
                        _record_subagent_created_nodes(write_scope, result)
                    if role == IMAGE_EDITOR_ROLE_NAME:
                        _image_editor_after_tool_call(
                            state=image_editor_state,
                            tool_name=tool_name,
                            tool_input=tool_input,
                            result=result,
                        )
                    ok = not (isinstance(result, dict) and result.get("error"))
                    rendered = _render_subagent_tool_result(tool_name, result)
                    result_limit = _subagent_tool_result_limit(role, tool_name)
                    if len(rendered) > result_limit:
                        rendered = rendered[:result_limit] + "...<truncated>"
                    tool_entry = {
                        "tool": tool_name,
                        "input": tool_input,
                        "ok": ok,
                        "step": step_no,
                    }
                    if not ok and isinstance(result, dict):
                        tool_entry["error"] = result.get("error")
                        tool_entry["error_kind"] = result.get("error_kind")
                    tool_log.append(tool_entry)
                    _append_subagent_trace(
                        trace_log,
                        role=role,
                        step=step_no,
                        event="tool_result",
                        tool=tool_name,
                        ok=ok,
                        error=result.get("error") if isinstance(result, dict) else "",
                        error_kind=result.get("error_kind") if isinstance(result, dict) else "",
                        result_keys=list(result.keys())[:20] if isinstance(result, dict) else [type(result).__name__],
                        candidate_lifecycle=_image_editor_lifecycle_summary(image_editor_state),
                    )
                    transcript.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": rendered,
                    })
                    model_parts = _subagent_model_content_parts(result)
                    if model_parts:
                        _append_subagent_model_content(
                            visual_tail,
                            model_parts,
                            role=role,
                            refs=_subagent_model_content_refs(result),
                            image_editor_state=image_editor_state,
                        )
                except Exception as exc:
                    tool_log.append({
                        "tool": tool_name,
                        "input": tool_input,
                        "ok": False,
                        "error": str(exc),
                        "step": step_no,
                    })
                    _append_subagent_trace(
                        trace_log,
                        role=role,
                        step=step_no,
                        event="tool_exception",
                        tool=tool_name,
                        error=str(exc),
                    )
                    transcript.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": json.dumps(
                            {"ok": False, "error": f"工具 {tool_name} 抛错:{exc}。换个思路或结束。"},
                            ensure_ascii=False,
                        ),
                    })
            continue


# ── Public tools ────────────────────────────────────────────────────────


def _agent_run_catalog() -> list[dict[str, Any]]:
    return [
        {
            "agent": NODE_PRODUCER_ROLE_NAME,
            "description": ROLE_PRESETS[NODE_PRODUCER_ROLE_NAME]["description"],
            "inputs": [
                "node_id",
                "node_ids",
                "objective",
                "basis",
                "primary_skill",
                "inline_spec",
                "allowed_node_types",
                "reference_node_ids",
                "acceptance_criteria",
                "allow_create",
            ],
            "summary": (
                "首选通用生产 worker；主 Agent 先搭节点框架、指定作用域并传 primary_skill/inline_spec，"
                "worker 读取指定依据、补 prompt/fields、运行并自检。"
            ),
        },
        {
            "agent": WORKFLOW_SPEC_ROLE_NAME,
            "description": ROLE_PRESETS[WORKFLOW_SPEC_ROLE_NAME]["description"],
            "inputs": [
                "workflow_skill",
                "workflow_skill_name",
                "user_goal",
                "natural_language_flow",
                "facts",
                "inputs",
                "context",
            ],
            "summary": (
                "负责选择现有 workflow 模板；隔离读取 skill/template/spec，"
                "返回 template_id，并由 agent.run 随引用补充 input_fields。"
            ),
        },
        {
            "agent": IMAGE_EDITOR_ROLE_NAME,
            "description": ROLE_PRESETS[IMAGE_EDITOR_ROLE_NAME]["description"],
            "inputs": ["node_id", "source_ref", "candidate_ref", "notes"],
            "summary": "编辑已有 image 节点；worker 自己看图、preview、验证并 commit。",
        }
    ]


async def agent_run(
    project_id: str,
    agent: str = "",
    task: str = "",
    inputs: dict | str | None = None,
    max_steps: int = 20,
) -> dict:
    """Delegate a scoped task to a registered specialist sub-agent."""
    agent_key = str(agent or "").strip().lower()
    if not agent_key or agent_key in {"list", "catalog", "agents"}:
        return {
            "ok": True,
            "status": "catalog",
            "available_agents": _agent_run_catalog(),
        }
    if agent_key not in AGENT_RUN_ROLE_NAMES:
        return {
            "ok": False,
            "error": f"未知子 Agent: {agent}",
            "error_kind": "unknown_subagent",
            "available_agents": _agent_run_catalog(),
        }
    normalized_inputs = _coerce_mapping_arg(inputs, fallback_key="text")
    subagent_allowed_tools: list[str] | None = None
    if agent_key == WORKFLOW_SPEC_ROLE_NAME:
        normalized_inputs = {
            **normalized_inputs,
            "_workflow_spec_mode": "selector",
        }
        subagent_allowed_tools = workflow_spec_role.allowed_tools_for_mode("selector")
    subagent_kwargs: dict[str, Any] = {
        "project_id": project_id,
        "role": agent_key,
        "task": str(task or "").strip() or "完成主 Agent 委派的任务。",
        "inputs": normalized_inputs,
        "max_steps": max_steps,
    }
    if subagent_allowed_tools is not None:
        subagent_kwargs["allowed_tools"] = subagent_allowed_tools
    sub_result = await subagent_run(**subagent_kwargs)
    ok = not bool(sub_result.get("error"))
    result = sub_result.get("result") if isinstance(sub_result.get("result"), dict) else {}
    if agent_key == WORKFLOW_SPEC_ROLE_NAME:
        result = _finalize_workflow_spec_result(
            project_id=project_id,
            task=str(task or ""),
            inputs=normalized_inputs,
            result_payload=result,
        )
        sub_result["result"] = result
        await _record_workflow_spec_authorized_ref(
            project_id=project_id,
            result_payload=result,
            task=str(task or ""),
        )
    status = str(result.get("status") or ("completed" if ok else "blocked"))
    if status == "blocked":
        ok = False
    blocked_error = ""
    blocked_hint = ""
    blocked_feedback: dict[str, Any] | None = None
    if not ok and status == "blocked":
        summary = str(sub_result.get("summary") or "子 Agent 无法完成委派任务。").strip()
        blocked_error = summary or "子 Agent 无法完成委派任务。"
        node_id = result.get("node_id") or normalized_inputs.get("node_id")
        evidence: dict[str, Any] = {
            "agent": agent_key,
            "status": status,
            "node_id": node_id,
            "node_ids": result.get("node_ids"),
            "completed_node_ids": result.get("completed_node_ids"),
            "committed": result.get("committed"),
            "candidate_ref": result.get("candidate_ref"),
            "committed_ref": result.get("committed_ref"),
            "image_node_ids": result.get("image_node_ids"),
            "output_refs": result.get("output_refs"),
            "basis_used": result.get("basis_used"),
            "blocked_reason": result.get("blocked_reason"),
            "issues": result.get("issues"),
            "steps_used": sub_result.get("steps_used"),
        }
        evidence = {k: v for k, v in evidence.items() if v not in (None, "", [], {})}
        blocked_hint = (
            "子 Agent 已完成隔离尝试并返回 blocked。向用户说明失败原因、已尝试步骤和可选下一步；"
            "同一素材和同一目标保持停止状态，等待用户调整目标、提供更清晰素材或授权使用更强的分割能力。"
        )
        blocked_feedback = {
            "tool": "agent.run",
            "error_kind": "subagent_blocked",
            "what_went_wrong": blocked_error,
            "how_to_fix": blocked_hint,
            "suggested_next": "report_blocked_to_user",
            "retry_policy": "同一素材和同一目标保持停止状态；向用户报告 blocked 结果。",
            "evidence": evidence,
        }
    return {
        "ok": ok,
        "agent": agent_key,
        "status": status,
        "summary": sub_result.get("summary") or "",
        "result": sub_result.get("result"),
        "steps_used": sub_result.get("steps_used"),
        "tool_log": sub_result.get("tool_log") or [],
        "error": sub_result.get("error") or blocked_error,
        **({
            "error_kind": "subagent_blocked",
            "hint": blocked_hint,
            "suggested_next": "report_blocked_to_user",
            "model_feedback": blocked_feedback,
            "terminal": True,
        } if blocked_feedback else {}),
        "available_agents": _agent_run_catalog(),
        "_subagent_usage": sub_result.get("_subagent_usage") or [],
        "_subagent_trace": sub_result.get("_subagent_trace") or [],
    }


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
    `error` is "" on success. Read-only roles keep their existing outer timeout
    behavior; explicit writer roles may enforce `max_steps`.
    Custom allowed_tools can only narrow the selected role preset.
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
        subagent_usage=result.get("_subagent_usage") if isinstance(result.get("_subagent_usage"), list) else [],
        subagent_trace=result.get("_subagent_trace") if isinstance(result.get("_subagent_trace"), list) else [],
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
