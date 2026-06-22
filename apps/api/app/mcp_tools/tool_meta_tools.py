"""按需加载分发器 — Agent 通过 tool.search / describe / execute 使用 Tier 2 工具。

设计参考 Claude Code 的 skill.load_content 机制:
- 启动时 LLM 只看到稳定核心工具的完整 schema
- Tier 2 工具(memory/agent/scene/shot/asset/canvas/media/file 等)留在 deferred pool
- 模型需要时 search → describe → execute

收益:
- tools 数组不随 intent namespace 扩张(prompt cache 命中率更稳定)
- 模型按场景按需拉,不会一上来就被几十个工具的细节淹没
"""
from __future__ import annotations

import inspect
import json
from typing import Any

from app.agent.tool_errors import normalize_tool_result
from app.agent.permission_policy import ToolPermissionContext, decide_tool_permission
from app.mcp_tools.query_match import invalid_regex_response, match_text
from app.mcp_tools.registry import _schema_from_handler, registry, ToolSpec


# 按使用场景把 Tier 2 工具分到几个 category(用户决定)
# Tier 1 工具不进 list,因为它们已经在主工具表里完整可见。
# 低频工具通过 deferred 暴露；被节点协议吸收的旧辅助工具标记 hidden。
_CATEGORIES: dict[str, set[str]] = {
    "guide": {"skill.project_mentor", "skill.story_template_method"},
    "project": {"project.create"},
    "delete": set(),
    "query": {
        "project.list",
        "media.describe_image",
        "events.query", "events.tail",
        "reference.manage",
    },
    "assets": {
        "assets.get_library_path",
        "assets.list_project",
        "assets.list_shared",
        "assets.read_asset",
        "reference.manage",
    },
    "system": {"system.status", "system.models", "feature.list", "feature.is_enabled"},
    "memory": {"memory.save_fact", "memory.save_user_fact", "memory.compact_context", "memory.recall", "reference.manage"},
    "task": {"task.create", "task.delete"},
    "collab": {
        "agent.review",
        "agent.map_reduce", "agent.pipeline",
        "agent.hierarchical",
    },
    "attach": {"file.extract_text_from_upload", "drama.parse_uploaded_script", "media.describe_image", "reference.manage"},
    "control": {"media.cancel_image_generation"},
    "file": {
        "file.list_dir",
        "file.read_text",
        "file.workspace_delete",
        "file.workspace_list",
        "file.workspace_patch",
        "file.workspace_read",
        "file.workspace_search",
        "file.workspace_write",
    },
}


def _tier_of(spec: ToolSpec) -> int:
    """1 = 完整 schema 始终带 / 2 = 按需 / 3 = 隐藏"""
    exposure = registry.tool_exposure(spec.name)
    if exposure in {"hidden", "unregistered"}:
        return 3
    if exposure == "core":
        return 1
    return 2


def _category_of(name: str) -> str | None:
    for cat, names in _CATEGORIES.items():
        if name in names:
            return cat
    return None


_STATIC_SEARCH_HINTS: dict[str, str] = {
    "project.reset": "reset full reset clear failed nodes destructive confirmation 重置项目 清空项目 清理失败节点",
    "project.create": "new project blank project create project 新建项目 空白项目",
    "skill.project_mentor": (
        "guide project mentor video workflow blueprint tree T2V I2V storyboard shot images story template "
        "text-to-video image-to-video keyframes first frame last frame multi reference @图片 uploaded image style reference asset library "
        "blueprint revision audit trace debugging node repair rerun failed node blueprint plan source path production audit model_written "
        "prompt_source dependency_missing pending_video_blueprint_request Claude Code architecture "
        "项目规则 视频工作流 蓝图修订 制作审查 交付审查 "
        "失败节点 原地修复 节点修复 重跑 蓝图执行计划 执行计划 排障 "
        "文生视频 图生视频 首尾帧 多图参考 参考图 风格参考 宫格分镜 单张分镜 "
        "人物图 场景图 分镜图 故事模板图 首帧图 尾帧图 通用制作流程 标准制作流程 通用视频制作流程"
    ),
	    "skill.story_template_method": (
	        "story template story_template image_to_video optional method complex action blocking "
	        "visual development board high resolution 3840x2160 2560x1440 camera map action flow art direction "
	        "故事模板 图生视频 可选制作方法 复杂动作 动作调度 视觉开发板 高分辨率"
	    ),
    "system.models": "model mapping active model provider 模型映射 当前模型 provider",
    "system.status": "system status health tool capability 系统状态 工具 能力",
    "file.list_dir": "list directory files in data or project storage 列目录 遍历文件树",
    "file.read_text": (
        "read explicit user uploaded text file project storage rel_path 读取用户上传或本轮明确路径文本文件 "
        "not guide source docs trace tool_result guide 内容用 skill.project_mentor 节点状态用 node.get"
    ),
    "file.workspace_delete": "delete workspace file directory recursive force no shell codex-like filesystem 删除工作区文件 目录 不执行命令",
    "file.workspace_list": "list workspace files directory recursive no shell codex-like filesystem 列出工作区文件 不执行命令",
    "file.workspace_patch": "patch workspace text file exact replacement no shell codex-like apply patch 修改工作区文本 不执行命令",
    "file.workspace_read": "read workspace file text base64 line range no shell codex-like filesystem 读取工作区文件 不执行命令",
    "file.workspace_search": "search workspace file names content glob recursive no shell fuzzy file search 查找工作区文件 不执行命令",
    "file.workspace_write": "write workspace text file append overwrite create dirs no shell codex-like filesystem 写工作区文件 不执行命令",
    "memory.recall": "recall memory project facts 历史事实 项目记忆 查询记忆",
    "memory.save_fact": "save project memory fact pinned constraint 项目记忆 保存事实 不可变约束",
    "task.create": "create visible progress task manual tracking explicit user request long running multi step 创建任务 任务跟踪 手动跟踪 长耗时 多步骤",
    "task.delete": "delete stale task cleanup residual task explicit user request 清理任务 删除任务 残留任务 过期任务",
    "agent.review": (
        "readonly review checker inspect prompt image storyboard video consistency cinematic design hook punch "
        "review_skill custom_checklist skills/review 检查 审查 提示词 图片一致性 分镜连续性 "
        "视频提示词 分镜一致 影视设计 爆点 钩子 自定义检查 skill"
    ),
    "reference.manage": "reference image @图 @图片 style visual asset analyze visual analysis alias bind blueprint memory asset library register asset_id source_path 参考图 风格图 视觉分析 图片识别 别名 蓝图绑定 资产库 长期风格记忆",
    "assets.get_library_path": "asset library path configured project shared assets 资产库路径 项目素材库 共享素材库",
    "assets.list_project": "list project asset library generated saved image video script 资产库 检索 项目素材 图片 视频 剧本",
    "assets.list_shared": "list shared reusable asset library character scene style 共享素材库 可复用 人物 场景 风格",
    "assets.read_asset": "read asset library file metadata image path text content 读取资产库 图片 文件 元信息",
}

_DEFERRED_CONFIRMATION_TOOLS: set[str] = set()
_DEFERRED_TOOL_ALIASES = {
    "file.read": "file.read_text",
}
_DEFERRED_FAILURE_POLICY = (
    "不要重新搜索同一个工具，也不要用同一参数重复调用。"
    "先根据 error_kind、hint 和当前状态修正参数；无法修正时停止并说明失败原因。"
)


def _cached_project_mentor_result(
    *,
    target: str,
    kwargs: dict[str, Any],
    state: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if target != "skill.project_mentor" or not isinstance(state, dict):
        return None
    if kwargs.get("force_refresh") is True:
        return None
    topic = str(kwargs.get("topic") or "overview").strip().lower()
    if not topic:
        return None
    requested_detail = str(kwargs.get("detail") or "summary").strip().lower() or "summary"
    cache = state.get("_mentor_guides_loaded")
    cached = cache.get(topic) if isinstance(cache, dict) else None
    if not isinstance(cached, dict):
        return None
    if requested_detail == "full" and not cached.get("has_full_guide"):
        return None
    guidance_summary = str(cached.get("guidance_summary") or "").strip()
    if not guidance_summary:
        return None
    return {
        "ok": True,
        "topic": topic,
        "detail": cached.get("detail") or requested_detail,
        "guidance": guidance_summary,
        "references": [],
        "references_count": int(cached.get("references_count") or 0),
        "reference_policy": "来自 guide cache；无源码路径可读，复用 guidance 和 guidance_hash。",
        "has_full_guide": bool(cached.get("has_full_guide")),
        "from_guide_cache": True,
        "guidance_hash": cached.get("guidance_hash") or "",
        "cache_policy": (
            "当前项目已缓存该 project_mentor 指南；复用摘要/哈希。"
            "只有用户明确要求刷新或本轮状态变化使缓存失效时才设置 force_refresh=true 重新读取。"
        ),
    }


def _split_names(value: str) -> list[str]:
    cleaned = value.replace("\n", ",").replace(";", ",")
    return [item.strip().replace("__", ".") for item in cleaned.split(",") if item.strip()]


def _schema_summary(schema: dict[str, Any]) -> dict[str, Any]:
    params = schema if schema else {"type": "object", "properties": {}}
    properties = params.get("properties") if isinstance(params, dict) else {}
    if not isinstance(properties, dict):
        properties = {}
    required = params.get("required") if isinstance(params, dict) else []
    if not isinstance(required, list):
        required = []
    props: list[dict[str, str]] = []
    for name, prop in list(properties.items())[:12]:
        prop = prop if isinstance(prop, dict) else {}
        props.append({
            "name": name,
            "type": str(prop.get("type") or "string"),
            "description": str(prop.get("description") or "")[:120],
        })
    return {
        "type": str(params.get("type") or "object") if isinstance(params, dict) else "object",
        "required": [str(item) for item in required[:12]],
        "properties": props,
    }


def _tool_example(spec: ToolSpec) -> str:
    for hint in _tool_usage_hints(spec):
        return hint
    if _tier_of(spec) == 1:
        return f"{spec.name}(...)"
    return f"tool.execute(name='{spec.name}', input={{...}})"


def _tool_usage_hints(spec: ToolSpec) -> list[str]:
    hints: list[str] = []
    for item in spec.usage_hints or []:
        text = str(item).strip()
        if text:
            hints.append(text)
    metadata_hints = spec.metadata.get("usage_hints") if isinstance(spec.metadata, dict) else None
    if isinstance(metadata_hints, str):
        metadata_hints = [metadata_hints]
    if isinstance(metadata_hints, list):
        for item in metadata_hints:
            text = str(item).strip()
            if text and text not in hints:
                hints.append(text)
    return hints


def _tool_search_text(spec: ToolSpec, category: str) -> str:
    metadata_text = ""
    if isinstance(spec.metadata, dict):
        metadata_text = " ".join(
            str(value)
            for key, value in spec.metadata.items()
            if key in {"title", "description", "summary", "search_hint", "tags"}
        )
    return " ".join([
        spec.name,
        spec.name.replace(".", " "),
        spec.namespace,
        " ".join(spec.tags or []),
        category,
        spec.description or "",
        spec.search_hint or "",
        " ".join(_tool_usage_hints(spec)),
        _STATIC_SEARCH_HINTS.get(spec.name, ""),
        metadata_text,
    ]).lower()


def _tool_search_item(spec: ToolSpec, *, category: str, detail: bool = False) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": spec.name,
        "namespace": spec.namespace,
        "category": category,
        "tags": spec.tags,
        "description": (spec.description or spec.name).split("\n")[0][:240],
        "boundaries": {
            "is_read_only": bool(spec.is_read_only),
            "is_destructive": bool(spec.is_destructive),
            "requires_confirmation": bool(spec.requires_confirmation),
            "is_concurrency_safe": bool(spec.is_concurrency_safe),
            "max_result_size": spec.max_result_size,
        },
    }
    hints = _tool_usage_hints(spec)
    if hints:
        item["usage_hints"] = hints[:3]
    if detail:
        params = spec.schema if spec.schema else _schema_from_handler(spec.handler)
        item["input_schema_summary"] = _schema_summary(params)
        item["example"] = _tool_example(spec)
    return item


async def tool_list(category: str | None = None) -> dict[str, Any]:
    """列出 Tier 2 按需加载的工具(name + 一句话描述)。

    Args:
      category: 可选,按使用场景过滤。常用值:
        guide / project / delete / query / assets / system / template / memory / task / collab / canvas / attach / control
        不传 = 返回 Tier 2 全集

    Returns: {category, tools: [{name, description}], total}
    """
    out_tools: list[dict[str, str]] = []
    for spec in registry._tools.values():
        if _tier_of(spec) != 2:
            continue
        if category is not None:
            if _category_of(spec.name) != category:
                continue
        first_line = (spec.description or spec.name).split("\n")[0]
        out_tools.append({
            "name": spec.name,
            "description": first_line[:200],
            "category": _category_of(spec.name) or "other",
        })

    out_tools.sort(key=lambda t: t["name"])
    return {
        "category": category,
        "total": len(out_tools),
        "tools": out_tools,
    }


async def tool_describe(names: list[str] | str) -> dict[str, Any]:
    """拉取一批 Tier 2 工具的完整 input_schema。一次可拉多个。

    Args:
      names: 工具名列表(或单个字符串)。例:['memory.save_fact','file.read_text']

    Returns: {tools: [{name, description, input_schema}], not_found: [...]}
    """
    if isinstance(names, str):
        # 容错:LLM 可能传 "a,b,c"
        names = [n.strip() for n in names.split(",") if n.strip()]

    out: list[dict[str, Any]] = []
    not_found: list[str] = []
    for raw in names:
        # 容错:LLM 可能传 LLM-safe 名 "memory__save_fact"
        norm = raw.replace("__", ".")
        spec = registry.get(norm)
        if not spec:
            not_found.append(raw)
            continue
        if _tier_of(spec) == 3:
            not_found.append(f"{raw} (hidden)")
            continue
        params = spec.schema if spec.schema else _schema_from_handler(spec.handler)
        out.append({
            "name": spec.name,
            "description": spec.description or spec.name,
            "input_schema": params,
            "tier": _tier_of(spec),
            "category": _category_of(spec.name) or "other",
            "tags": spec.tags,
            "usage_hints": _tool_usage_hints(spec),
            "example": _tool_example(spec),
            "boundaries": {
                "is_read_only": bool(spec.is_read_only),
                "is_destructive": bool(spec.is_destructive),
                "requires_confirmation": bool(spec.requires_confirmation),
                "is_concurrency_safe": bool(spec.is_concurrency_safe),
                "max_result_size": spec.max_result_size,
            },
        })

    return {"tools": out, "not_found": not_found}


async def tool_search(
    query: str = "",
    category: str | None = None,
    regex: str | list[str] | None = None,
    pattern: str | list[str] | None = None,
    case_sensitive: bool = False,
    limit: int = 8,
) -> dict[str, Any]:
    """Search deferred Tier 2 tools by name, category, hints, tags, or description.

    Query modes:
      - select:project.create,system.models exact deferred tool lookup
      - discover:视频制作 skill              richer result with schema summary/example
      - normal keywords                    ranked lightweight result
      - regex/pattern                      match tool name/category/hints/schema text
    """
    invalid = invalid_regex_response(regex=regex, pattern=pattern)
    if invalid is not None:
        return invalid
    raw_query = (query or "").strip()
    q = raw_query.lower()
    limit = max(1, min(int(limit or 8), 25))

    if q.startswith("select:"):
        names = _split_names(raw_query.split(":", 1)[1])
        tools: list[dict[str, Any]] = []
        not_found: list[str] = []
        for name in names:
            spec = registry.get(name)
            if not spec or _tier_of(spec) != 2:
                not_found.append(name)
                continue
            cat = _category_of(spec.name) or "other"
            if category and cat != category:
                not_found.append(name)
                continue
            tools.append(_tool_search_item(spec, category=cat, detail=True))
        return {
            "query": query,
            "category": category,
            "regex": regex,
            "pattern": pattern,
            "mode": "select",
            "total": len(tools),
            "tools": tools[:limit],
            "not_found": not_found,
        }

    detail = False
    if q.startswith("discover:"):
        raw_query = raw_query.split(":", 1)[1].strip()
        q = raw_query.lower()
        detail = True

    matches: list[tuple[int, dict[str, Any]]] = []

    for spec in registry._tools.values():
        if _tier_of(spec) != 2:
            continue
        cat = _category_of(spec.name) or "other"
        if category and cat != category:
            continue
        haystack = _tool_search_text(spec, cat)
        search_match = match_text(
            haystack,
            query="",
            regex=regex,
            pattern=pattern,
            case_sensitive=case_sensitive,
        )
        regex_matched = bool((regex or pattern) and search_match.get("matched"))
        if not q:
            score = 90 if regex_matched else 1
        elif q == spec.name.lower() or q == spec.name.replace(".", "__").lower():
            score = 150
        elif q in spec.name.lower():
            score = 120
        elif q in haystack:
            score = 70
        else:
            words = [w for w in q.split() if w]
            score = 0
            matched_words = 0
            matched_category_word = False
            strong_name_match = False
            for word in words:
                if word in spec.name.lower():
                    score += 30
                    matched_words += 1
                    strong_name_match = True
                elif word in haystack:
                    score += 12
                    matched_words += 1
                if word in {cat.lower(), spec.namespace.lower()}:
                    matched_category_word = True
            if len(words) > 1 and matched_words < 2 and not matched_category_word and not strong_name_match:
                score = 0
        if regex_matched:
            score = max(score, 90)
        if score <= 0:
            continue
        item = _tool_search_item(spec, category=cat, detail=detail)
        if regex_matched:
            item["match"] = {
                key: value
                for key, value in search_match.items()
                if key in {"mode", "matched_patterns"} and value not in (None, "", [], {})
            }
        matches.append((score, item))

    matches.sort(key=lambda item: (-item[0], item[1]["name"]))
    return {
        "query": query,
        "category": category,
        "regex": regex,
        "pattern": pattern,
        "mode": "discover" if detail else "keyword",
        "total": len(matches),
        "tools": [item for _, item in matches[:limit]],
    }


def _normalize_input(input_data: dict[str, Any] | str | None) -> dict[str, Any]:
    if input_data is None:
        return {}
    if isinstance(input_data, dict):
        return dict(input_data)
    if isinstance(input_data, str):
        try:
            parsed = json.loads(input_data)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _filter_kwargs(handler, kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return kwargs
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in params}


def _permission_summary(target: str, decision: Any) -> dict[str, Any]:
    result = decision.result if isinstance(getattr(decision, "result", None), dict) else {}
    return {
        "tool": target,
        "allowed": bool(getattr(decision, "allowed", False)),
        "error_kind": result.get("error_kind"),
    }


def _attach_deferred_failure_policy(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("error") or payload.get("ok") is False:
        payload.setdefault("failure_policy", _DEFERRED_FAILURE_POLICY)
    return payload


async def tool_execute(
    project_id: str,
    name: str,
    input: dict[str, Any] | str | None = None,
    _state: dict[str, Any] | None = None,
    _user_message: str = "",
    _requires_plan: bool = False,
) -> dict[str, Any]:
    """Execute one deferred Tier 2 tool after target-tool permission checks."""
    requested_target = (name or "").strip().replace("__", ".")
    target = _DEFERRED_TOOL_ALIASES.get(requested_target, requested_target)
    spec = registry.get(target)
    if not spec:
        return {
            "ok": False,
            "error": f"Unknown deferred tool: {name}",
            "error_kind": "unknown_deferred_tool",
            "hint": "先用 tool.search 查找真实 deferred 工具名；读取上传文本应通过 tool.execute(name='file.read_text', input={...})。",
            "failure_policy": _DEFERRED_FAILURE_POLICY,
        }
    tier = _tier_of(spec)
    if tier == 3:
        return {
            "ok": False,
            "error": f"Tool is hidden from the agent: {target}",
            "error_kind": "hidden_deferred_tool",
            "failure_policy": _DEFERRED_FAILURE_POLICY,
        }
    if tier == 1:
        return {
            "ok": False,
            "error": f"{target} is a core tool; call it directly instead of tool.execute.",
            "error_kind": "core_tool_should_be_called_directly",
            "tool": target,
            "failure_policy": _DEFERRED_FAILURE_POLICY,
        }
    if target == "tool.execute":
        return {
            "ok": False,
            "error": "Recursive tool.execute is not allowed.",
            "error_kind": "recursive_deferred_tool",
            "failure_policy": _DEFERRED_FAILURE_POLICY,
        }

    decision = decide_tool_permission(
        ToolPermissionContext(
            tool_name=target,
            state=_state or {},
            user_message=_user_message,
            requires_plan=bool(_requires_plan),
            tool_args=_normalize_input(input),
            via_tool_execute=True,
        )
    )
    if not decision.allowed:
        denied = decision.result or {
            "ok": False,
            "error": "Deferred tool target was denied by permission policy.",
            "error_kind": "deferred_permission_denied",
            "tool": target,
        }
        denied = normalize_tool_result(denied, tool_name=target)
        return {
            "_deferred_tool": target,
            "_deferred_permission": _permission_summary(target, decision),
            **_attach_deferred_failure_policy(denied),
        }

    kwargs = _normalize_input(input)
    kwargs["project_id"] = project_id
    cached_result = _cached_project_mentor_result(target=target, kwargs=kwargs, state=_state)
    if cached_result is not None:
        return {
            "_deferred_tool": target,
            "_deferred_alias": {"requested": requested_target, "resolved": target} if requested_target != target else None,
            "_deferred_permission": _permission_summary(target, decision),
            **cached_result,
        }
    if target in _DEFERRED_CONFIRMATION_TOOLS:
        from app.agent.confirmation_protocol import is_pending_confirmation_expired

        pending_tool = (_state or {}).get("_pending_tool_confirm") if isinstance(_state, dict) else None
        pending_target = str(pending_tool.get("target") or "") if isinstance(pending_tool, dict) else ""
        if (
            not isinstance(pending_tool, dict)
            or pending_target != target
            or is_pending_confirmation_expired(pending_tool)
        ):
            return {
                "_deferred_tool": target,
                "_deferred_permission": _permission_summary(target, decision),
                "ok": False,
                "requires_user_confirm": True,
                "action": target,
                "risk": "destructive",
                "reason": "该操作会清空当前画布节点和连线，确认前不会执行。",
                "error_kind": "deferred_tool_requires_confirmation",
                "failure_policy": _DEFERRED_FAILURE_POLICY,
            }
    if target == "project.reset" and kwargs.get("scope") == "full":
        pending_reset = (_state or {}).get("_pending_reset_confirm") if isinstance(_state, dict) else None
        if isinstance(pending_reset, dict) and pending_reset.get("scope") == "full":
            from app.agent.confirmation_protocol import is_pending_confirmation_expired
            from app.agent.reset_flow import make_reset_confirm_token

            if not is_pending_confirmation_expired(pending_reset):
                kwargs["_confirm_token"] = make_reset_confirm_token(project_id)
                kwargs.setdefault("reason", pending_reset.get("reason") or "用户确认重置")
        elif not str(kwargs.get("reason") or "").strip():
            kwargs["reason"] = "全量重置当前项目需要用户确认，确认前不会执行。"
    try:
        result = await registry.call(target, **_filter_kwargs(spec.handler, kwargs))
    except TypeError as exc:
        return _attach_deferred_failure_policy(normalize_tool_result({
            "_deferred_tool": target,
            "_deferred_permission": _permission_summary(target, decision),
            "ok": False,
            "error": f"Bad deferred tool arguments for {target}: {exc}",
            "error_kind": "bad_deferred_tool_arguments",
            "tool": target,
        }, tool_name=target))
    except Exception as exc:
        return _attach_deferred_failure_policy(normalize_tool_result({
            "_deferred_tool": target,
            "_deferred_permission": _permission_summary(target, decision),
            "ok": False,
            "error": str(exc),
            "error_kind": "deferred_tool_failed",
            "tool": target,
        }, tool_name=target))

    if isinstance(result, dict):
        result = normalize_tool_result(result, tool_name=target)
        return {
            "_deferred_tool": target,
            "_deferred_alias": {"requested": requested_target, "resolved": target} if requested_target != target else None,
            "_deferred_permission": _permission_summary(target, decision),
            **_attach_deferred_failure_policy(result),
        }
    return {
        "ok": True,
        "_deferred_tool": target,
        "_deferred_alias": {"requested": requested_target, "resolved": target} if requested_target != target else None,
        "_deferred_permission": _permission_summary(target, decision),
        "result": result,
    }
