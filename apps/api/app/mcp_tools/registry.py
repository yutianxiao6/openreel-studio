"""Tool registry — central catalog of every callable tool the Agent has.

Why:
- One canonical list (name → handler + schema + namespace + description) so the
  planner, the MCP server export, and the docs all read from the same source.
- Skills / plugins can register their own tools by calling `register(...)`
  at import time. The agent's prompt context auto-picks them up.
- A tool can be looked up by full name (`node.run`) and invoked
  with a kwargs dict, regardless of which python module defined it.

Usage:
    from app.mcp_tools.registry import registry
    handler = registry.get("node.run")
    result = await handler(node_id=...)

Skill author:
    from app.mcp_tools.registry import register
    @register("myskill.do_thing", description="...", schema={...})
    async def do_thing(project_id: str, x: int): ...
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from types import UnionType
from typing import Any, Awaitable, Callable, Union, get_args, get_origin, get_type_hints

ToolHandler = Callable[..., Awaitable[Any]]

INTERNAL_RAW_RUNNER_TOOL_NAMES: tuple[str, ...] = ()

UNREGISTERED_DRAMA_RAW_RUNNER_TOOL_NAMES: tuple[str, ...] = ()

UNREGISTERED_MEDIA_RUNNER_TOOL_NAMES: tuple[str, ...] = ()

UNREGISTERED_MEDIA_STATUS_TOOL_NAMES: tuple[str, ...] = (
    "media.get_status",
)

UNREGISTERED_MODEL_CONFIG_TOOL_NAMES: tuple[str, ...] = (
    "model.list_configs",
    "model.get_config",
    "model.set_config",
)

UNREGISTERED_DRAMA_SEGMENT_TOOL_NAMES: tuple[str, ...] = ()

UNREGISTERED_CANVAS_CRUD_TOOL_NAMES: tuple[str, ...] = (
    "canvas.create_node",
    "canvas.update_node",
    "canvas.list_nodes",
    "canvas.list_edges",
    "canvas.get_node",
    "canvas.connect_nodes",
    "canvas.delete_node",
    "canvas.cleanup_test_nodes",
    "canvas.layout_nodes",
)

UNREGISTERED_BLUEPRINT_WRITE_TOOL_NAMES: tuple[str, ...] = (
    "blueprint.get",
    "blueprint.revise",
    "blueprint.save_from_plan",
    "blueprint.render_view_model",
    "blueprint.apply_pending_revision",
    "blueprint.clear",
)

UNREGISTERED_BLUEPRINT_TREE_TOOL_NAMES: tuple[str, ...] = (
    "blueprint.start_tree_draft",
    "blueprint.append_tree_node",
    "blueprint.update_tree_node",
    "blueprint.finalize_tree_draft",
    "blueprint.propose_tree",
    "blueprint.add_child",
    "blueprint.update_node",
    "blueprint.delete_node",
    "blueprint.list_children",
    "blueprint.set_prompt",
)

UNREGISTERED_DEPRECATED_ALIAS_TOOL_NAMES: tuple[str, ...] = (
    "drama.reset_project",
)

UNREGISTERED_TASK_HELPER_TOOL_NAMES: tuple[str, ...] = (
    "task.get",
    "task.list_pending",
)

UNREGISTERED_TASK_WRITE_TOOL_NAMES: tuple[str, ...] = ()

UNREGISTERED_PROJECT_LOW_LEVEL_TOOL_NAMES: tuple[str, ...] = (
    "project.rename",
    "project.delete",
    "project.lock_field",
    "project.unlock_field",
    "project.save_version",
    "project.list_versions",
    "project.restore_version",
    "project.update_state",
)

AGENT_HIDDEN_PROJECT_MODE_TOOL_NAMES: tuple[str, ...] = ()

UNREGISTERED_MEDIA_PROVIDER_WRITE_TOOL_NAMES: tuple[str, ...] = (
    "media.add_provider",
    "media.update_provider",
    "media.remove_provider",
    "media.set_active",
    "media.get_active",
)

AGENT_HIDDEN_MEDIA_PROVIDER_READ_TOOL_NAMES: tuple[str, ...] = (
    "media.get_presets",
    "media.list_providers",
)

UNREGISTERED_ASSET_WRITE_TOOL_NAMES: tuple[str, ...] = (
    "assets.set_library_path",
)

UNREGISTERED_CONFIG_WRITE_TOOL_NAMES: tuple[str, ...] = (
    "config.write_file",
    "config.patch",
    "config.reload",
    "config.list_all",
)

UNREGISTERED_MEMORY_LOW_LEVEL_TOOL_NAMES: tuple[str, ...] = (
    "memory.pin_fact",
    "memory.forget",
    "memory.forget_user",
    "memory.summarize_conversation",
    "memory.record_user_hit",
)

UNREGISTERED_FILE_WRITE_TOOL_NAMES: tuple[str, ...] = (
    "file.write_text",
    "file.save_uploaded",
    "file.delete",
)

UNREGISTERED_PLAN_CONTROL_TOOL_NAMES: tuple[str, ...] = (
    "plan.update_step",
    "plan.approve",
    "plan.reject",
    "plan.clear",
)

UNREGISTERED_AGENT_LOW_LEVEL_TOOL_NAMES: tuple[str, ...] = (
    "agent.subagent_run",
    "agent.subagent_fan_out",
    "agent.subagent_aggregate",
    "agent.export_project_zip",
)

UNREGISTERED_TEAM_TOOL_NAMES: tuple[str, ...] = (
    "team.spawn",
    "team.list",
    "team.remove",
    "team.request_shutdown",
    "team.respond_shutdown",
    "team.submit_plan",
    "team.review_plan",
    "team.auto_claim",
    "team.snapshot",
    "team.restore",
)

UNREGISTERED_DRAMA_DELETE_TOOL_NAMES: tuple[str, ...] = ()

UNREGISTERED_MCP_META_TOOL_NAMES: tuple[str, ...] = (
    "mcp.list_servers",
    "mcp.list_external_tools",
    "mcp.reload_server",
)

UNREGISTERED_NODE_HELPER_TOOL_NAMES: tuple[str, ...] = (
    "node.get_creation_guide",
    "node.check_readiness",
    "node.list_creatable_types",
    "node.list_unfinished",
)

UNREGISTERED_SESSION_TOOL_NAMES: tuple[str, ...] = (
    "session.set_focus",
    "session.get_focus",
    "session.clear_focus",
)

UNREGISTERED_PANEL_TOOL_NAMES: tuple[str, ...] = (
    "panel.get_layout",
    "panel.set_layout",
)

UNREGISTERED_SCENE_SHOT_ASSET_WRITE_TOOL_NAMES: tuple[str, ...] = (
    "scene.create",
    "shot.create",
    "shot.update",
    "asset.register",
    "asset.attach_to_shot",
)

AGENT_HIDDEN_SCENE_SHOT_ASSET_READ_TOOL_NAMES: tuple[str, ...] = (
    "scene.list",
    "shot.list",
    "asset.list",
)

UNREGISTERED_PROMPT_TOOL_NAMES: tuple[str, ...] = (
    "prompt.list",
    "prompt.get",
    "prompt.update_override",
    "prompt.clear_override",
    "prompt.preview",
)

UNREGISTERED_GENERIC_SKILL_TOOL_NAMES: tuple[str, ...] = (
    "skill.list",
    "skill.load_content",
    "skill.create",
    "skill.delete",
    "skill.reload",
)

UNREGISTERED_DOMAIN_SKILL_TOOL_NAMES: tuple[str, ...] = (
    "skill.character_with_reference",
    "skill.hook_punch_review",
)


def _annotation_to_schema(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Parameter.empty:
        return {"type": "string"}
    if annotation is Any:
        return {"type": "string"}

    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is not None:
        if origin in (Union, UnionType):
            non_none_args = [arg for arg in args if arg is not type(None)]
            if len(non_none_args) == 1:
                return _annotation_to_schema(non_none_args[0])
            return {"anyOf": [_annotation_to_schema(arg) for arg in non_none_args]}
        if origin is list:
            item_schema = _annotation_to_schema(args[0]) if args else {"type": "string"}
            return {"type": "array", "items": item_schema}
        if origin is dict:
            return {"type": "object", "additionalProperties": True}
        annotation = origin

    _TYPE_MAP = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }
    schema_type = _TYPE_MAP.get(annotation, "string")
    if schema_type == "array":
        return {"type": "array", "items": {"type": "string"}}
    if schema_type == "object":
        return {"type": "object", "additionalProperties": True}
    return {"type": schema_type}


def _schema_from_handler(handler: ToolHandler) -> dict[str, Any]:
    """Auto-generate a minimal JSON Schema from a handler's type hints."""
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return {"type": "object", "properties": {}}
    try:
        type_hints = get_type_hints(handler)
    except Exception:
        type_hints = {}

    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        properties[name] = _annotation_to_schema(type_hints.get(name, param.annotation))
        if param.default is inspect.Parameter.empty:
            required.append(name)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _compact_agent_schema(schema: Any) -> Any:
    """Drop nested schema prose from always-loaded core tool definitions."""
    if isinstance(schema, dict):
        return {
            key: _compact_agent_schema(value)
            for key, value in schema.items()
            if key != "description"
        }
    if isinstance(schema, list):
        return [_compact_agent_schema(item) for item in schema]
    return schema


_RUNTIME_CONTEXT_SCHEMA_KEYS = {
    "project_id",
    "_state",
    "_user_message",
    "_requires_plan",
}


def _hide_runtime_context_schema(schema: Any) -> Any:
    """Hide parameters that the chat harness injects deterministically."""
    if isinstance(schema, dict):
        normalized = {
            key: _hide_runtime_context_schema(value)
            for key, value in schema.items()
        }
        properties = normalized.get("properties")
        if isinstance(properties, dict):
            for key in _RUNTIME_CONTEXT_SCHEMA_KEYS:
                properties.pop(key, None)
        required = normalized.get("required")
        if isinstance(required, list):
            required = [item for item in required if item not in _RUNTIME_CONTEXT_SCHEMA_KEYS]
            if required:
                normalized["required"] = required
            else:
                normalized.pop("required", None)
        return normalized
    if isinstance(schema, list):
        return [_hide_runtime_context_schema(item) for item in schema]
    return schema


def _llm_compatible_schema(schema: Any) -> Any:
    """Return a provider-safe JSON Schema copy for function declarations."""
    if isinstance(schema, dict):
        normalized = {
            key: _llm_compatible_schema(value)
            for key, value in schema.items()
        }
        if normalized.get("type") == "array" and "items" not in normalized:
            normalized["items"] = {"type": "string"}
        return normalized
    if isinstance(schema, list):
        return [_llm_compatible_schema(item) for item in schema]
    return schema


def _node_reference_array_schema(*, description: str | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "array",
        "items": {
            "oneOf": [
                {"type": "string"},
                {
                    "type": "object",
                    "properties": {
                        "ref": {"type": "string"},
                        "role": {
                            "type": "string",
                            "enum": [
                                "context",
                                "visual_reference",
                                "style_reference",
                                "character_reference",
                                "scene_reference",
                                "storyboard_reference",
                                "source_image",
                            ],
                        },
                    },
                    "required": ["ref"],
                },
            ],
        },
    }
    if description:
        schema["description"] = description
    return schema


def _node_media_field_properties() -> dict[str, Any]:
    """Shared model-visible media generation fields."""
    return {
        "aspect_ratio": {
            "type": "string",
            "description": "画幅比例，如 16:9、9:16、1:1。",
        },
        "resolution": {
            "type": "string",
            "description": "图片精确像素尺寸，格式 <width>x<height>，如 2560x1440。",
        },
        "quality": {
            "type": "string",
            "description": "生成质量，如 high、hd、standard。",
        },
    }


def _node_create_field_properties() -> dict[str, Any]:
    """Fields accepted when creating text/image/video/audio nodes."""
    properties: dict[str, Any] = {
        "title": {"type": "string"},
        "content": {"type": "string"},
        "description": {"type": "string"},
        "prompt": {"type": "string"},
        **_node_media_field_properties(),
        "duration_seconds": {"type": "number"},
        "production_path": {"type": "string"},
        "purpose": {"type": "string"},
        "references": _node_reference_array_schema(
            description="上游引用；字符串或 {ref, role} 对象。"
        ),
    }
    return properties


def _node_update_input_properties() -> dict[str, Any]:
    """Fields accepted under node.update patch.input_json."""
    return {
        **_node_media_field_properties(),
        "references": _node_reference_array_schema(
            description="局部更新上游引用；字符串或 {ref, role} 对象。"
        ),
        "depends_on": _node_reference_array_schema(
            description="局部更新拓扑依赖；字符串或 {ref, role} 对象。"
        ),
        "prompt_source": {"type": "string"},
    }


def _node_object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": properties,
    }


@dataclass
class ToolSpec:
    name: str                       # "node.run"
    handler: ToolHandler
    description: str = ""
    schema: dict[str, Any] = field(default_factory=dict)  # JSON schema of args
    namespace: str = ""
    tags: list[str] = field(default_factory=list)         # e.g. ["drama", "single"]
    requires_node: bool = False     # true → composite wrapper that owns a node
    metadata: dict[str, Any] = field(default_factory=dict) # arbitrary (e.g. SKILL.md frontmatter)
    search_hint: str = ""           # extra deferred-search index text, not shown as full prompt
    usage_hints: list[str] = field(default_factory=list)  # short retrieval-oriented hints
    is_read_only: bool = False
    is_destructive: bool = False
    requires_confirmation: bool = False
    is_concurrency_safe: bool = False
    max_result_size: int | None = None

    @property
    def short_name(self) -> str:
        return self.name.split(".", 1)[1] if "." in self.name else self.name


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        handler: ToolHandler,
        *,
        description: str = "",
        schema: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        requires_node: bool = False,
        metadata: dict[str, Any] | None = None,
        search_hint: str = "",
        usage_hints: list[str] | None = None,
        is_read_only: bool | None = None,
        is_destructive: bool | None = None,
        requires_confirmation: bool | None = None,
        is_concurrency_safe: bool | None = None,
        max_result_size: int | None = None,
        replace: bool = False,
    ) -> ToolSpec:
        if name in self._tools and not replace:
            raise ValueError(f"Tool already registered: {name}")
        namespace = name.split(".", 1)[0] if "." in name else ""
        meta = metadata or {}
        meta_usage_hints = meta.get("usage_hints") or []
        if isinstance(meta_usage_hints, str):
            meta_usage_hints = [meta_usage_hints]
        spec = ToolSpec(
            name=name,
            handler=handler,
            description=description or (inspect.getdoc(handler) or "").strip(),
            schema=schema or {},
            namespace=namespace,
            tags=tags or [],
            requires_node=requires_node,
            metadata=meta,
            search_hint=search_hint or str(meta.get("search_hint") or ""),
            usage_hints=list(usage_hints or meta_usage_hints or []),
            is_read_only=bool(is_read_only) if is_read_only is not None else bool(meta.get("is_read_only", False)),
            is_destructive=bool(is_destructive) if is_destructive is not None else bool(meta.get("is_destructive", False)),
            requires_confirmation=(
                bool(requires_confirmation)
                if requires_confirmation is not None
                else bool(meta.get("requires_confirmation", False))
            ),
            is_concurrency_safe=(
                bool(is_concurrency_safe)
                if is_concurrency_safe is not None
                else bool(meta.get("is_concurrency_safe", False))
            ),
            max_result_size=max_result_size if max_result_size is not None else meta.get("max_result_size"),
        )
        self._tools[name] = spec
        standardizer = globals().get("_standardize_tool_spec")
        if callable(standardizer):
            standardizer(spec, self)
        return spec

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def handler(self, name: str) -> ToolHandler:
        spec = self._tools.get(name)
        if not spec:
            raise KeyError(f"Unknown tool: {name}")
        return spec.handler

    async def call(self, name: str, /, **kwargs) -> Any:
        return await self.handler(name)(**kwargs)

    def list_tools(self, namespace: str | None = None, tag: str | None = None) -> list[ToolSpec]:
        items = list(self._tools.values())
        if namespace:
            items = [t for t in items if t.namespace == namespace]
        if tag:
            items = [t for t in items if tag in t.tags]
        return items

    def registered_tool_names(self) -> set[str]:
        return set(self._tools)

    def tool_exposure(self, name: str) -> str:
        """Return the agent-facing exposure tier for a registered tool."""
        spec = self.get(name)
        if spec is None:
            return "unregistered"
        if name in self._AGENT_HIDDEN:
            return "hidden"
        if name in self._CORE_AGENT_TOOLS:
            return "core"
        if name in self._TIER1_EXTRA or spec.namespace in self._TIER1_NS:
            return "core"
        return "deferred"

    def core_agent_tool_names(self) -> set[str]:
        return {
            name
            for name in self._tools
            if self.tool_exposure(name) == "core"
        }

    def deferred_tool_names(self) -> set[str]:
        return {
            name
            for name in self._tools
            if self.tool_exposure(name) == "deferred"
        }

    def agent_hidden_tool_names(self) -> set[str]:
        return {
            name
            for name in self._tools
            if self.tool_exposure(name) == "hidden"
        }

    def agent_visible_tool_names(self) -> set[str]:
        return self.core_agent_tool_names() | self.deferred_tool_names()

    def namespaces(self) -> list[str]:
        return sorted({t.namespace for t in self._tools.values() if t.namespace})

    def manifest(self) -> list[dict[str, Any]]:
        """JSON manifest suitable for prompt injection / MCP server export."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "namespace": t.namespace,
                "tags": t.tags,
                "requires_node": t.requires_node,
                "schema": t.schema,
                "metadata": t.metadata,
                "search_hint": t.search_hint,
                "usage_hints": t.usage_hints,
                "is_read_only": t.is_read_only,
                "is_destructive": t.is_destructive,
                "requires_confirmation": t.requires_confirmation,
                "is_concurrency_safe": t.is_concurrency_safe,
                "max_result_size": t.max_result_size,
            }
            for t in sorted(self._tools.values(), key=lambda s: s.name)
        ]

    def get_openai_tools(
        self,
        *,
        names: list[str] | None = None,
        tags: list[str] | None = None,
        exclude_tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Export tools as OpenAI function-calling format (compatible with LiteLLM).

        Filtering priority: explicit `names` list > tags/exclude_tags.
        """
        if names is not None:
            specs = [self._tools[n] for n in names if n in self._tools]
        else:
            specs = list(self._tools.values())
            if tags:
                specs = [s for s in specs if any(t in s.tags for t in tags)]
            if exclude_tags:
                specs = [s for s in specs if not any(t in s.tags for t in exclude_tags)]

        result: list[dict[str, Any]] = []
        for spec in specs:
            params = spec.schema if spec.schema else _schema_from_handler(spec.handler)
            params = _hide_runtime_context_schema(params)
            params = _llm_compatible_schema(params)
            result.append({
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description or spec.name,
                    "parameters": params,
                },
            })
        return result

    # ── Tier 设计(参考 Claude Code skill.load_content 机制) ─────────────
    # Tier 1: 完整 schema 始终注入 — 创作主路径,频繁调用,不绕弯
    # Tier 2: 只露 {name, description}; node-first 默认路径不再依赖
    # tool.search/tool.describe/tool.execute 发现业务流程。
    # Tier 3: 完全隐藏,等价 _AGENT_HIDDEN
    _TIER1_NS: set[str] = set()
    _TIER1_EXTRA: set[str] = set()

    # Layer 1 namespaces: always injected with full schema
    _LAYER1_NS = {"node", "tool"}
    # Legacy namespace-filter allowance. Stable-core mode does not expose these
    # directly; attachment ingestion uses the deferred tool loader.
    _LAYER1_EXTRA: set[str] = {"drama.parse_uploaded_script"}
    # Layer 2 namespaces: injected with full schema but lower priority
    _LAYER2_NS = {"project", "memory", "reference", "plan", "task", "agent", "canvas", "scene", "shot",
                  "asset", "media", "file", "skill"}
    # Hidden from Agent Loop —— Agent 不能直接调,统一走 node primitive protocol。
    # 旧的 drama.* / canvas CRUD / media.generate_* /
    # scene/shot/asset 写工具 / 配置类工具全部下沉。HTTP API 仍可调,前端/CLI 不受影响。
    _AGENT_HIDDEN = {
        # canvas CRUD has been absorbed by node/panel APIs and unregistered.
        # Node helper tools have been replaced by the node primitive protocol
        # and unregistered: node.list/get/get_creation_guide/create/run.
        # Raw drama/media generators are internal runner targets; user-facing
        # creation goes through node.create + node.run. They have been moved
        # behind direct Python calls/services and unregistered.
        *INTERNAL_RAW_RUNNER_TOOL_NAMES,
        # drama.parse_uploaded_script 不藏 —— 解析上传脚本的入口
        # Legacy drama destructive wrappers are unregistered. canvas.delete is
        # the single agent-facing destructive canvas primitive.
        # Deprecated aliases have been unregistered.
        # media.describe_image 不藏 —— 识图(用户上传/参考图分析)
        # media.get_status is unregistered; node/run state and debug/trace APIs
        # are the media progress surface.
        # media provider writes/active reads are unregistered; settings/config
        # paths own provider management. media.test_provider remains for the
        # settings panel.
        *AGENT_HIDDEN_MEDIA_PROVIDER_READ_TOOL_NAMES,
        "media.test_provider",
        "image.grid_split",
        "image.grid_combine",
        "image.extract_grid_cell",
        "image.place_grid_cell",
        "image.inpaint_region",
        # scene/shot/asset write tools have been folded into node/front-end
        # workflows and unregistered.
        *AGENT_HIDDEN_SCENE_SHOT_ASSET_READ_TOOL_NAMES,
        # project 写工具 —— Agent 不直接改 state,删项目走 project.reset
        # project.create 是个例外：允许 Agent 通过对话开新项目（会触发前端切换）
        "project.update_state",
        *AGENT_HIDDEN_PROJECT_MODE_TOOL_NAMES,
        # low-level project wrappers are unregistered. Project CRUD uses REST;
        # state reset uses project.reset; blueprint/version behavior is
        # handled by dedicated internal helpers.
        # memory 低频/低层
        # low-level memory mutation/summarization wrappers are unregistered.
        # Orchestrator calls summarization directly; Agent-facing memory stays
        # on memory.recall/compact_context/save_fact/save_user_fact.
        "memory.recall_user",
        # Old plan control wrappers are unregistered. Explicit Plan Mode is
        # handled by deterministic slash commands plus read-only tool policy.
        # task.get/list_pending are folded into task.list and unregistered.
        # session focus tools have been replaced by blueprint/task/runtime
        # context and unregistered.
        # Low-level agent wrappers are unregistered. Keep high-level
        # deferred collaboration wrappers and direct Python helpers.
        # 配置/skill/prompt/mcp/panel 全藏；用户可编辑知识只通过 skills 暴露。
        # model.* config wrappers are unregistered; model information is read
        # through system.models and settings/config APIs.
        "config.read",
        "config.read_file",
        "config.validate",
        # config writes/compat summary are unregistered; settings uses
        # /api/tools/config/* REST endpoints. Keep read/validate for readonly
        # subagents and diagnostics.
        # generic skill management wrappers are unregistered; concrete skills
        # such as skill.project_mentor remain self-contained tools.
        *UNREGISTERED_GENERIC_SKILL_TOOL_NAMES,
        # prompt management wrappers are unregistered; prompt changes are
        # code/docs/test changes or explicit admin API work.
        # mcp.* meta tools are unregistered; external MCP status/management
        # is exposed through /api/tools/mcp/* REST endpoints.
        # panel layout is a frontend REST API, not an Agent tool.
        # file write/delete wrappers are unregistered; file read/extract stays
        # available for readonly/debug and attachment paths.
        # assets 库路径配置仍由设置/资产面板处理；显式保存走 deferred。
        "assets.set_library_path",
        # task.create/delete remain registered for explicit deferred cleanup and
        # backend compatibility, but are no longer part of the default core
        # tool surface.
        # 一次性蓝图提交、低层编辑和 prompt 注入工具已移入内部/测试路径。
        # 蓝图生成由 start/append/finalize 增量原语和 revise 高层原语驱动。
        *UNREGISTERED_BLUEPRINT_TREE_TOOL_NAMES,
        # blueprint write/cleanup wrappers have been internalized and unregistered.
    }

    # Stable core tool surface for the Agent Loop. The node-first path keeps
    # business workflow in skill.video_production and exposes primitives needed
    # to read state, ask users, maintain a lightweight task ledger, and
    # create/update/run/delete nodes.
    _CORE_AGENT_TOOLS: set[str] = {
        "agent.review",
        "canvas.delete",
        "interaction.request_input",
        "node.create",
        "node.get",
        "node.list",
        "node.run",
        "node.update",
        "project.get_state",
        "project.reset",
        "skill.get",
        "skill.search",
        "task.complete",
        "task.create",
        "task.list",
        "task.update",
        "tool.describe",
        "tool.execute",
        "tool.search",
        "vision.view_image",
    }
    _CORE_NS: set[str] = {"agent", "canvas", "interaction", "node", "project", "skill", "task", "tool", "vision"}

    def get_tools_for_agent_loop(
        self,
        namespaces: list[str] | None = None,
        stable_core: bool = True,
    ) -> list[dict[str, Any]]:
        """Export a curated tool list for the Agent Loop.

        Default P1 mode:
        - stable core tools only, with full schema
        - all non-core tools are deferred via tool.search / tool.describe /
          tool.execute

        Legacy mode is still available with stable_core=False for diagnostics.

        Tool names use '__' instead of '.' for LLM API compatibility (DeepSeek etc).
        Use `resolve_tool_name()` to convert back.
        """
        if stable_core:
            specs = [
                spec
                for spec in sorted(self._tools.values(), key=lambda s: s.name)
                if spec.name in self._CORE_AGENT_TOOLS and spec.name not in self._AGENT_HIDDEN
            ]
            result: list[dict[str, Any]] = []
            for spec in specs:
                params = spec.schema if spec.schema else _schema_from_handler(spec.handler)
                params = _compact_agent_schema(params)
                params = _hide_runtime_context_schema(params)
                params = _llm_compatible_schema(params)
                result.append({
                    "type": "function",
                    "function": {
                        "name": spec.name.replace(".", "__"),
                        "description": spec.description or spec.name,
                        "parameters": params,
                    },
                })
            return result

        tier1_specs: list[ToolSpec] = []
        tier2_specs: list[ToolSpec] = []

        allowed_ns: set[str] | None = None
        if namespaces is not None:
            allowed_ns = set(namespaces) | self._CORE_NS

        for spec in self._tools.values():
            if spec.name in self._AGENT_HIDDEN:
                continue

            # Tier 1 判定:命名空间在 _TIER1_NS 或工具名在 _TIER1_EXTRA
            is_tier1 = (
                spec.name in self._TIER1_EXTRA
                or spec.namespace in self._TIER1_NS
            )

            # Layer 控制(老逻辑保留兼容)
            if spec.name in self._LAYER1_EXTRA:
                in_scope = allowed_ns is None or spec.namespace in allowed_ns
                if not in_scope:
                    continue
            elif spec.namespace in self._LAYER1_NS:
                in_scope = allowed_ns is None or spec.namespace in allowed_ns
                if not in_scope:
                    continue
            elif spec.namespace in self._LAYER2_NS:
                in_scope = allowed_ns is None or spec.namespace in allowed_ns
                if not in_scope:
                    continue
            else:
                continue

            if is_tier1:
                tier1_specs.append(spec)
            else:
                tier2_specs.append(spec)

        result: list[dict[str, Any]] = []

        # Tier 1: 完整 schema
        for spec in tier1_specs:
            params = spec.schema if spec.schema else _schema_from_handler(spec.handler)
            params = _hide_runtime_context_schema(params)
            params = _llm_compatible_schema(params)
            result.append({
                "type": "function",
                "function": {
                    "name": spec.name.replace(".", "__"),
                    "description": spec.description or spec.name,
                    "parameters": params,
                },
            })

        # Tier 2: 只 name + description,极简 schema 占位(避免 OpenAI 校验报错)
        for spec in tier2_specs:
            short_desc = (spec.description or spec.name).split("\n")[0][:160]
            result.append({
                "type": "function",
                "function": {
                    "name": spec.name.replace(".", "__"),
                    "description": f"[Tier2 按需] {short_desc} — 调用前先 tool.describe(names=['{spec.name}']) 拿完整参数",
                    "parameters": {"type": "object", "properties": {}},
                },
            })

        return result

    @staticmethod
    def resolve_tool_name(llm_name: str) -> str:
        """Convert LLM-safe name back to registry name: 'drama__generate_characters' → 'drama.generate_characters'"""
        return llm_name.replace("__", ".")


registry = ToolRegistry()


_STANDARD_DESCRIPTION_BASES: dict[str, str] = {
    "agent.hierarchical": "按 split 组织分层只读/协作子任务，并把每个 split 的结果汇总返回",
    "agent.map_reduce": "并行分发多个独立子任务，并可选做聚合摘要",
    "agent.pipeline": "按顺序执行协作阶段，并把上一阶段产出注入下一阶段",
    "agent.review": "隔离运行只读审查子 Agent，按用户需求和证据检查具体错误",
    "asset.list": "读取项目资产记录列表",
    "assets.get_library_path": "读取资产库路径配置",
    "assets.list_project": "读取当前项目资产库文件列表",
    "assets.list_shared": "读取共享资产库文件列表",
    "assets.read_asset": "读取指定资产文件的元信息或文本内容",
    "canvas.delete": "删除指定画布节点或清空画布，并清理节点本地产物",
    "config.read": "读取 runtime 配置结构，默认隐藏敏感密钥",
    "config.read_file": "读取 runtime 配置原始 JSONC、解析结构和校验状态",
    "config.validate": "校验给定配置内容但不写入文件",
    "drama.parse_uploaded_script": "把上传或粘贴的剧本文本解析成结构化剧集、场景和人物草稿",
    "events.query": "按事件类型和时间范围查询项目生命周期事件",
    "events.tail": "读取最近的项目生命周期事件",
    "feature.is_enabled": "查询某个 feature flag 当前是否启用以及是否被 kill switch 关闭",
    "feature.list": "列出 feature flag 和 kill switch 状态",
    "file.extract_text_from_upload": "从 txt、md、docx 等上传文件中抽取纯文本",
    "file.list_dir": "读取允许路径下的目录列表",
    "file.read_text": "读取允许路径下的文本文件",
    "file.workspace_delete": "删除当前 workspace 内的文件或目录",
    "file.workspace_list": "列出当前 workspace 内的文件和目录",
    "file.workspace_patch": "按精确文本替换修改当前 workspace 内的文本文件",
    "file.workspace_read": "读取当前 workspace 内的文件内容",
    "file.workspace_search": "在当前 workspace 内按文件名或文本内容搜索",
    "file.workspace_write": "写入当前 workspace 内的文本文件",
    "image.extract_grid_cell": "把宫格图片节点里的单个 cell 导出成新的图片节点",
    "image.grid_combine": "把多个同规格图片组合成图片节点内部宫格",
    "image.grid_split": "把图片节点切换为宫格编辑态并生成内部裁剪 cell",
    "image.place_grid_cell": "把图片引用放入 image_grid 的指定 cell",
    "image.inpaint_region": "对图片或宫格 cell 的局部 mask 区域发起重绘",
    "interaction.request_input": "用通用问题卡向用户提出最多 6 个短问题并等待提交",
    "media.cancel_image_generation": "取消当前项目正在进行或排队的图片生成步骤",
    "media.describe_image": "识别上传图或已生成图片并返回视觉描述",
    "media.get_presets": "读取图片 provider 推荐参数预设",
    "media.list_providers": "读取已配置的媒体 provider 列表",
    "media.test_provider": "向指定媒体 provider 发送最小真实请求并返回测试结果",
    "reference.manage": "管理项目参考图资产：注册上传图、解析 @图、视觉分析、别名和显式长期保存",
    "memory.compact_context": "压缩当前会话上下文，保存摘要和长期事实",
    "memory.recall": "检索当前项目的相关记忆",
    "memory.recall_user": "检索跨项目用户偏好记忆",
    "memory.save_fact": "保存当前项目级长期事实",
    "memory.save_user_fact": "保存跨项目用户偏好或稳定工作习惯",
    "node.create": "创建一个或少量 text/image/video/audio 创作节点",
    "node.get": "读取一个或多个指定节点的完整输入、输出、提示词、状态、surface 和链接信息",
    "node.list": "列出当前项目画布节点索引，默认返回 20 个节点，可按节点类型、状态或关键词过滤",
    "node.run": "执行指定节点并由后端按节点类型派发 runner、落库状态和产物",
    "node.update": "局部更新一个或少量指定节点的允许字段",
    "project.create": "新建空白项目壳并切换为当前项目",
    "project.get_state": "读取项目 state、节点摘要、待确认输入、安全确认、任务和运行状态",
    "project.list": "读取项目列表",
    "project.reset": "按 scope 清理失败节点或执行已确认的全量项目重置",
    "scene.list": "读取项目场景列表",
    "shot.list": "读取项目镜头列表",
    "skill.get": "读取指定 skill 全文",
    "skill.project_mentor": "查询项目架构、规则、文档入口和排障顺序",
    "skill.search": "搜索 skill；用户本地优先",
    "skill.video_production": "读取节点优先的图片和视频制作流程",
    "system.models": "读取任务类型到模型的当前映射",
    "system.status": "读取系统状态、模型、工具、MCP 和能力摘要",
    "task.complete": "把执行任务标记为 completed 并保存结果摘要",
    "task.create": "创建轻量进度任务/checklist",
    "task.list": "读取当前任务图任务列表，并可按项目过滤",
    "task.update": "更新任务状态、负责人、依赖或执行元数据",
    "tool.describe": "读取 deferred 工具的完整 schema 和使用元数据",
    "tool.execute": "执行已经 search/describe 过的 deferred 工具",
    "tool.search": "列出 visible deferred 工具目录，或按名称、分类、标签和描述搜索 deferred 工具",
    "vision.view_image": "读取项目图片节点或项目存储图片，并把一张或多张图片像素附加给主模型上下文",
}

_STANDARD_CANNOT_BY_NAME: dict[str, str] = {
    "agent.review": "不能创建、修改、运行、删除、批准、重置或直接向用户提交；只返回审查结论给主 Agent",
    "canvas.delete": "不能当作 full reset；它不清任务、项目 state 或标题",
    "config.read": "不能写配置；配置修改走设置页或 config REST 控制面",
    "config.read_file": "不能写配置；配置修改走设置页或 config REST 控制面",
    "config.validate": "不能写配置或刷新运行时状态",
    "interaction.request_input": "不能创建、修改、删除、运行、重置或批准任何项目内容；只能请求用户补充信息并等待提交",
    "media.test_provider": "不能生成正式项目资产，也不能修改 provider 配置",
    "node.create": "不能创建未列入公开类型的旧节点或 raw runner 节点，不能运行节点",
    "node.run": "不能绕过节点依赖或 readiness 错误，不能直接调用 raw drama/media runner 替代",
    "node.update": "不能把运行产物写进 prompt，也不能绕过节点字段边界",
    "project.create": "不能代替内容制作流程；用户只是要做内容时不要新建空项目壳",
    "project.get_state": "不能修改项目，也不能把历史上下文当成当前状态",
    "project.reset": "不能在没有当前用户明确请求和必要确认时执行 full reset",
    "skill.get": "不能修改项目；只读取 skill",
    "skill.search": "不能修改项目；只搜索 skill",
    "task.complete": "不能在工具真实成功前标记完成",
    "task.update": "不能篡改任务图结构或绕过用户批准的执行计划",
    "tool.describe": "不能描述隐藏、注销或不存在的工具",
    "tool.execute": "不能执行核心、隐藏或已注销工具，不能绕过 permission policy",
    "tool.search": "不能返回核心、隐藏或已注销工具；目录只包含 visible deferred 工具",
    "vision.view_image": "不能分析图片、生成摘要或替模型做判断；只把图片像素附加给主模型",
}

_STANDARD_CANNOT_BY_NAMESPACE: dict[str, str] = {
    "agent": "不能授权写入、删除、重置或生成媒体；协作子任务仍受只读/权限边界约束",
    "assets": "不能配置、删除或移动资产；保存资产必须来自当前用户明确要求并走 assets.save_to_project/assets.save_to_shared",
    "asset": "不能注册、写入或附加资产；创作资产走节点或资产服务",
    "canvas": "不能创建、删除或修改节点内容；节点 CRUD 走 node.*",
    "config": "不能写配置；配置写入走 REST 控制面",
    "events": "不能修改事件、trace 或项目状态",
    "feature": "不能修改 feature flag 或 kill switch",
    "file": "不能越过 workspace/project 存储边界或执行命令",
    "interaction": "不能执行创作、审批或状态变更；只负责把模型的问题渲染成用户输入卡片",
    "media": "不能直接生成正式图片/视频；生成走 node.run 和媒体 service",
    "memory": "不能把不稳定推测写成长期事实，不能替代任务或节点状态",
    "reference": "不能替代节点执行；长期用户记忆必须有用户明确要求才保存",
    "scene": "不能创建或修改场景；场景创作走 node.*",
    "shot": "不能创建或修改镜头；镜头创作走 node.*",
    "skill": "不能越过项目工具、权限策略或节点规则直接改状态",
    "system": "不能修改模型、工具或 MCP 配置",
    "team": "不能越过主 Agent 权限边界直接改项目核心状态",
}

_STANDARD_USAGE_BY_NAME: dict[str, str] = {
    "interaction.request_input": "questions 提交后本轮停止，等待用户回复。",
    "agent.review": "阶段产出后调用；传目标、需求、摘要和证据；只修有证据的问题。",
    "canvas.delete": "scope='selected' 配 node_ids；scope='all' 清空当前项目画布。",
    "node.create": "单个或少量批量创建；搭框架/低风险可用 nodes，复杂媒体 prompt 或大量节点分批。",
    "node.get": "精确读取节点详情；多个节点一次传 node_ids，只有一个节点才传 node_id。",
    "node.list": "默认返回 20 个节点索引；需要更多传 limit，完整索引用 limit=0；详情批量 node.get。",
    "node.run": "运行前检查内容/prompt/fields/依赖；不符合当前 skill 或用户要求时先 node.update；失败读 error_kind/hint/model_feedback。",
    "node.update": "input_json 与旧 input 局部合并；不同改动用 updates，同一 patch 可配 node_ids；复杂/高风险分批。",
    "project.get_state": "开始、继续、排障或回答状态问题前读取真实项目状态。",
    "skill.search": "制作流程/提示词写法先搜；用户本地 skill 优先。",
    "skill.get": "读取 search 选中项；用户 skill 覆盖默认指南。",
    "skill.video_production": "补全/创建/修复图片/视频生产节点前读取；summary 用于轻量判断，full 用于实际制作。",
    "reference.manage": "处理上传参考图、@图、别名、视觉分析和长期保存时调用。",
    "task.create": "复杂多步用 subject 或 items 建 checklist；简单任务跳过。",
    "task.complete": "任务真实完成并有结果摘要后调用。",
    "task.list": "需要恢复进度、找可执行/失败/阻塞任务或清理残留前调用。",
    "task.update": "任务开始、阻塞、失败或元数据变化时调用；同项目最多一个 in_progress。",
    "tool.describe": "对已发现的 deferred 工具读取完整 schema 和使用元数据。",
    "tool.execute": "core 工具直接调用；deferred 先 search/describe。",
    "tool.search": "query='' 列出 visible deferred 目录；category 可缩小目录；知道名字后用 select:name 精确选择。",
    "vision.view_image": "看已有图片时先定位 node_id；node_ids/sources 可批量附加；工具不做摘要。",
    "project.reset": (
        "scope='failed' 清失败节点；scope='full' 带 reason 返回确认卡，确认后执行。"
    ),
}

_STANDARD_LIMIT_BY_NAME: dict[str, str] = {
    "interaction.request_input": "只请求用户输入，不创建、修改、删除、运行、重置或批准项目内容",
    "agent.review": "只读审查，不创建、修改、运行、删除、批准、重置或直接向用户提交",
    "node.create": "只创建节点，不运行节点",
    "canvas.delete": "破坏性删除，必须来自当前用户明确请求并走确认",
    "node.get": "只读取节点",
    "node.list": "只读取节点列表",
    "node.run": "只运行现有节点，不绕过依赖或 readiness 错误",
    "node.update": "只改允许字段，不写入不属于该节点的产物",
    "project.get_state": "只读取项目状态",
    "project.reset": "full reset 需要当前用户明确请求和确认",
    "skill.get": "只读取 skill 内容",
    "skill.search": "只搜索 skill 索引",
    "skill.video_production": "只读取制作指南，不创建、修改、运行或审批内容",
    "reference.manage": "管理参考图资产，不替代节点执行",
    "task.complete": "只标记真实完成的任务",
    "task.list": "只读取任务列表",
    "task.update": "只更新任务状态和元数据",
    "tool.describe": "只描述 visible deferred 工具",
    "tool.execute": "只执行 deferred 工具并受 permission policy 约束",
    "tool.search": "只列出或搜索 visible deferred 工具元数据",
    "vision.view_image": "只读取并附加图片像素，不创建摘要、不修改项目",
}


def _is_core_tool_name(name: str, target_registry: ToolRegistry) -> bool:
    spec = target_registry.get(name)
    if spec is None or name in target_registry._AGENT_HIDDEN:
        return False
    return (
        name in target_registry._CORE_AGENT_TOOLS
        or name in target_registry._TIER1_EXTRA
        or spec.namespace in target_registry._TIER1_NS
    )


def _tool_usage_line(name: str, target_registry: ToolRegistry) -> str:
    if name in target_registry._AGENT_HIDDEN:
        return "内部或控制面调用；不要把它作为主 Agent 执行路径。"
    if name in _STANDARD_USAGE_BY_NAME:
        return _STANDARD_USAGE_BY_NAME[name]
    if _is_core_tool_name(name, target_registry):
        return "核心工具可直接调用，按 schema 填参；调用前先确认当前项目、任务或节点状态。"
    return "先用 tool.search 缩小范围，再用 tool.describe 读取 schema，最后通过 tool.execute 执行。"


def _tool_limit_line(name: str, spec: ToolSpec) -> str:
    if name in _STANDARD_LIMIT_BY_NAME:
        return _STANDARD_LIMIT_BY_NAME[name].rstrip("。")
    cannot = (
        _STANDARD_CANNOT_BY_NAME.get(name)
        or _STANDARD_CANNOT_BY_NAMESPACE.get(spec.namespace)
        or "不执行 schema、权限和当前用户意图以外的动作"
    )
    return cannot.removeprefix("不能").rstrip("。")


def _standard_agent_tool_description(spec: ToolSpec, target_registry: ToolRegistry) -> str:
    base = _base_description(spec).rstrip("。")
    limit = _tool_limit_line(spec.name, spec)
    usage = _tool_usage_line(spec.name, target_registry).rstrip("。")
    parts = [base, usage]
    if (
        spec.is_destructive
        or spec.requires_confirmation
        or spec.name in {
            "interaction.request_input",
            "node.run",
            "tool.execute",
        }
    ):
        parts.append(limit)
    return " ".join(f"{part}。" for part in parts if part)


def _base_description(spec: ToolSpec) -> str:
    override = _STANDARD_DESCRIPTION_BASES.get(spec.name)
    if override:
        return override
    current = (spec.description or "").strip()
    if current:
        return " ".join(current.split())
    return f"{spec.name} 的工具能力"


def _cannot_description(spec: ToolSpec) -> str:
    text = (
        _STANDARD_CANNOT_BY_NAME.get(spec.name)
        or _STANDARD_CANNOT_BY_NAMESPACE.get(spec.namespace)
        or "执行 schema、权限和当前用户意图以外的动作，不能替代隐藏或已注销工具"
    )
    return text.removeprefix("不能").strip()


_READ_ONLY_TAGS = {"read", "query", "guide"}
_MUTATING_TAGS = {"execute", "write", "control", "destructive"}
_DESTRUCTIVE_NAMES = {"project.reset", "canvas.delete"}
_CONFIRMATION_NAMES = {"project.reset", "canvas.delete"}
_READ_ONLY_VERBS = ("get", "list", "describe", "search", "status", "models", "is_enabled")


def _coerce_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _infer_read_only(spec: ToolSpec) -> bool:
    if spec.is_destructive or "destructive" in spec.tags:
        return False
    if any(tag in _MUTATING_TAGS for tag in spec.tags):
        return False
    if any(tag in _READ_ONLY_TAGS for tag in spec.tags):
        return True
    short = spec.short_name.lower()
    if short.startswith(_READ_ONLY_VERBS):
        return True
    if spec.namespace in {"system", "template", "skill", "feature"}:
        return True
    return bool(spec.is_read_only)


def _apply_tool_boundary_metadata(spec: ToolSpec) -> None:
    spec.is_destructive = bool(
        spec.is_destructive
        or "destructive" in spec.tags
        or spec.name in _DESTRUCTIVE_NAMES
    )
    spec.requires_confirmation = bool(
        spec.requires_confirmation
        or spec.name in _CONFIRMATION_NAMES
        or "requires_confirmation" in spec.tags
    )
    spec.is_read_only = _infer_read_only(spec)
    spec.is_concurrency_safe = bool(spec.is_concurrency_safe or spec.is_read_only)
    spec.max_result_size = _coerce_optional_int(spec.max_result_size)


def _standardize_tool_spec(spec: ToolSpec, target_registry: ToolRegistry | None = None) -> None:
    """Apply boundary metadata and keep core tool descriptions stable."""
    _apply_tool_boundary_metadata(spec)
    target = target_registry or registry
    if _is_core_tool_name(spec.name, target):
        spec.description = _standard_agent_tool_description(spec, target)
    elif not spec.description:
        spec.description = _base_description(spec)


def _apply_standard_tool_descriptions(target_registry: ToolRegistry | None = None) -> None:
    target = target_registry or registry
    for spec in target.list_tools():
        _standardize_tool_spec(spec, target)


def register(
    name: str,
    *,
    description: str = "",
    schema: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    requires_node: bool = False,
    metadata: dict[str, Any] | None = None,
    search_hint: str = "",
    usage_hints: list[str] | None = None,
    is_read_only: bool | None = None,
    is_destructive: bool | None = None,
    requires_confirmation: bool | None = None,
    is_concurrency_safe: bool | None = None,
    max_result_size: int | None = None,
    replace: bool = False,
) -> Callable[[ToolHandler], ToolHandler]:
    """Decorator form. Skills can use this at import time."""

    def decorator(fn: ToolHandler) -> ToolHandler:
        registry.register(
            name,
            fn,
            description=description,
            schema=schema,
            tags=tags,
            requires_node=requires_node,
            metadata=metadata,
            search_hint=search_hint,
            usage_hints=usage_hints,
            is_read_only=is_read_only,
            is_destructive=is_destructive,
            requires_confirmation=requires_confirmation,
            is_concurrency_safe=is_concurrency_safe,
            max_result_size=max_result_size,
            replace=replace,
        )
        return fn

    return decorator


# ─────────────────────────────────────────────────────────────────────────
# Built-in registration. Done at import time so the agent and planner always
# see the same catalog.
# ─────────────────────────────────────────────────────────────────────────

def _register_builtins(target: ToolRegistry | None = None) -> ToolRegistry:
    from app.mcp_tools import (
        agent_tools,
        asset_library_tools,
        canvas_tools,
        config_tools,
        drama_tools,
        event_tools,
        feature_tools,
        file_tools,
        image_operation_tools,
        interaction_tools,
        media_tools,
        media_provider_tools,
        memory_tools,
        node_universal,
        project_tools,
        reference_tools,
        shot_tools,
        skill_tools,
        system_tools,
        task_tools,
        tool_meta_tools,
        vision_tools,
    )

    target_registry = target or registry
    R = target_registry.register

    # ─────────────────────────────────────────────────────────────────────
    # tool.* —— 元工具,按需加载 Tier 2 工具的完整 schema
    # ─────────────────────────────────────────────────────────────────────
    R("tool.describe", tool_meta_tools.tool_describe, tags=["tool", "meta", "read"],
      description=(
        "读取 deferred/Tier2 工具的 schema 和元数据。只描述可见按需工具；"
        "核心、隐藏和已注销工具不会通过这里展开。"
      ))
    R("tool.search", tool_meta_tools.tool_search, tags=["tool", "meta", "read"],
      description=(
        "列出或搜索 deferred/Tier2 工具目录，用于按需发现指南、系统和低频能力；"
        "query='' 列目录，select:name 精确选择，支持关键词和 regex。"
        "只返回可见按需工具，不替模型做业务判断。"
      ),
      schema={
          "type": "object",
          "properties": {
              "query": {"type": "string", "description": "空字符串列 visible deferred 目录；也支持关键词、select:name,name、discover:能力描述"},
              "category": {"type": "string", "description": "可选分类，如 guide/project/query/assets/system/memory/task/collab/attach/control/file"},
              "regex": {
                  "oneOf": [
                      {"type": "string"},
                      {"type": "array", "items": {"type": "string"}},
                  ],
                  "description": "可选正则或正则列表，匹配工具名/描述/tags/schema/hints。",
              },
              "pattern": {
                  "oneOf": [
                      {"type": "string"},
                      {"type": "array", "items": {"type": "string"}},
                  ],
                  "description": "regex 的别名；用于传一个或多个正则。",
              },
              "case_sensitive": {"type": "boolean", "description": "regex/query 是否大小写敏感，默认 false"},
              "limit": {"type": "integer", "description": "默认 8；传 0 返回完整目录或完整匹配结果"},
          },
      })
    R("tool.execute", tool_meta_tools.tool_execute, tags=["tool", "meta", "execute"],
      description=(
        "执行已经 search/describe 发现的 deferred/Tier2 工具。"
        "执行仍经过 schema、permission policy 和确认边界；失败时按 error_kind/hint 修参或停止。"
      ),
      schema={
          "type": "object",
          "properties": {
              "name": {"type": "string", "description": "目标工具名，如 project.reset"},
              "input": {"type": "object", "description": "目标工具参数，不要包含 project_id"},
          },
          "required": ["name"],
      })

    # ─────────────────────────────────────────────────────────────────────
    # interaction.* —— 通用用户输入/选择卡片
    # ─────────────────────────────────────────────────────────────────────
    R("interaction.request_input", interaction_tools.request_input,
      tags=["interaction", "control"],
      	      description=(
	        "Request user input with one generic card for up to six short questions and wait for submission.\n"
	        "This tool cannot create, modify, delete, reset, run, or approve project content."
      ),
      schema={
          "type": "object",
          "properties": {
              "title": {"type": "string", "description": "Optional card title shown to the user"},
              "purpose": {"type": "string", "description": "用途"},
              "stage": {"type": "string", "description": "阶段"},
              "description": {"type": "string", "description": "说明"},
              "submit_label": {"type": "string", "description": "提交按钮文案"},
              "summary_text": {"type": "string", "description": "状态摘要"},
              "assistant_text": {"type": "string", "description": "同步说明"},
              "questions": {
                  "type": "array",
                  "description": "Questions to show the user. Ask only useful questions and do not exceed 6.",
                  "minItems": 1,
                  "maxItems": 6,
                  "items": {
                      "type": "object",
                      "properties": {
                          "id": {"type": "string", "description": "Stable snake_case id"},
                          "header": {"type": "string", "description": "Short header label shown in the UI"},
                          "question": {"type": "string", "description": "Single-sentence prompt shown to the user"},
                          "options": {
                              "type": "array",
                              "description": "Optional. Provide 2-3 mutually exclusive choices only when the question should be a choice; omit for free text.",
                              "minItems": 2,
                              "maxItems": 3,
                              "items": {
                                  "type": "object",
                                  "properties": {
                                      "label": {"type": "string", "description": "User-facing label"},
                                      "description": {"type": "string"},
                                  },
                                  "required": ["label"],
                              },
                          },
                      },
                      "required": ["id", "header", "question"],
                  },
              },
          },
          "required": ["questions"],
      })

    # ─────────────────────────────────────────────────────────────────────
    # image.* —— 前端图片编辑隐藏工具；Agent 通过 image node operation + node.run 使用
    # ─────────────────────────────────────────────────────────────────────
    R("image.grid_split", image_operation_tools.grid_split,
      tags=["image", "write", "hidden"],
      description="把当前图片节点转换为 image_grid 输出，内部保存裁剪 cell，不自动创建多个画布节点。",
      schema={
          "type": "object",
          "properties": {
              "project_id": {"type": "string"},
              "node_id": {"type": "string"},
              "rows": {"type": "integer"},
              "cols": {"type": "integer"},
              "source_ref": {"type": "string"},
          },
          "required": ["project_id", "node_id", "rows", "cols"],
      })
    R("image.grid_combine", image_operation_tools.grid_combine,
      tags=["image", "write", "hidden"],
      description="把多个图片引用组合为当前图片节点的 image_grid 输出。",
      schema={
          "type": "object",
          "properties": {
              "project_id": {"type": "string"},
              "node_id": {"type": "string"},
              "source_refs": {"type": "array", "items": {"type": "string"}},
              "rows": {"type": "integer"},
              "cols": {"type": "integer"},
              "fit": {"type": "string", "enum": ["cover", "contain"]},
          },
          "required": ["project_id", "node_id", "source_refs", "rows", "cols"],
      })
    R("image.extract_grid_cell", image_operation_tools.extract_grid_cell,
      tags=["image", "write", "hidden"],
      description="把 image_grid 内部 cell 导出为新的普通 image 节点。",
      schema={
          "type": "object",
          "properties": {
              "project_id": {"type": "string"},
              "grid_node_id": {"type": "string"},
              "cell_id": {"type": "string"},
              "x": {"type": "number"},
              "y": {"type": "number"},
              "remove_from_grid": {"type": "boolean"},
          },
          "required": ["project_id", "grid_node_id", "cell_id"],
      })
    R("image.place_grid_cell", image_operation_tools.place_grid_cell,
      tags=["image", "write", "hidden"],
      description="把普通图片节点或图片引用放入 image_grid 指定 cell，可在 UI 移动时删除源节点。",
      schema={
          "type": "object",
          "properties": {
              "project_id": {"type": "string"},
              "grid_node_id": {"type": "string"},
              "cell_id": {"type": "string"},
              "source_ref": {"type": "string"},
              "fit": {"type": "string", "enum": ["cover", "contain"]},
              "remove_source_node": {"type": "boolean"},
          },
          "required": ["project_id", "grid_node_id", "cell_id", "source_ref"],
      })
    R("image.inpaint_region", image_operation_tools.inpaint_region,
      tags=["image", "write", "hidden"],
      description="对图片或宫格 cell 做局部重绘；当前 provider 不支持时返回明确错误。",
      schema={
          "type": "object",
          "properties": {
              "project_id": {"type": "string"},
              "node_id": {"type": "string"},
              "prompt": {"type": "string"},
              "mask_ref": {"type": "string"},
              "mask": {
                  "type": "object",
                  "description": "Normalized edit mask, for example {type:'brush', unit:'normalized', strokes:[{brush_size, points:[{x,y}]}]}.",
              },
	              "cell_id": {"type": "string"},
	          },
          "required": ["project_id", "node_id", "prompt"],
      })

    # ─────────────────────────────────────────────────────────────────────
    # node.* —— 5 个普适工具,Agent 创作的唯一入口
    # type 使用 text / image / video / audio 四类通用节点；具体制作方法写在树和字段里。
    # ─────────────────────────────────────────────────────────────────────
    R("node.create", node_universal.node_create, tags=["node", "write"],
      description=(
        "创建一个或少量 text/image/video/audio 工程节点。制作流程由 active skill 或用户目标指导；"
        "text 节点正文需要模型写进 fields.content；image/video/audio prompt 需要模型显式写入；"
        "image/video/audio 的 duration、aspect、style、production_path 等制作参数也写进 fields。"
        "批量搭框架或少量低风险节点可传 nodes；复杂媒体提示词或大量节点要分批。"
        "parent_node_id 只做画布分组；上游节点、资产或 URL 统一写 fields.references，"
        "role=visual_reference 表示参考生成，role=source_image 表示 image 节点直接采用该图作为输出。"
        "后端自动连线并把可用图片适配成媒体 runner 的图片输入。"
        "修复、降规格或重跑已有节点时用 node.update 原节点，不用本工具新建替代节点。"
        "该工具只创建节点，不批准计划、运行媒体或替模型选择制作策略。"
      ),
      schema={
          "type": "object",
          "properties": {
              "project_id": {"type": "string"},
              "type": {"type": "string", "enum": ["text", "image", "video", "audio"]},
              "fields": _node_object_schema(_node_create_field_properties()),
              "parent_node_id": {"type": "string"},
              "nodes": {
                  "type": "array",
                  "items": {
                      "type": "object",
                      "additionalProperties": True,
                      "properties": {
                          "client_ref": {"type": "string"},
                          "type": {"type": "string", "enum": ["text", "image", "video", "audio"]},
                          "fields": {"type": "object", "additionalProperties": True},
                          "parent_node_id": {"type": "string"},
                      },
                      "required": ["type"],
                  },
              },
          },
          "required": ["project_id"],
      })
    R("node.get", node_universal.node_get, tags=["node", "read"],
      description=(
          "读取节点完整信息(input / output / prompt / status / surface / links)。"
          "已知节点编号 id 时传 node_id/node_ids；只记得标题/描述/错误时传 query 或 regex 先取候选详情。"
      ),
      schema={
          "type": "object",
          "properties": {
              "project_id": {"type": "string"},
              "node_id": {"type": "string", "description": "单个节点 id；只查一个节点时使用"},
              "node_ids": {
                  "type": "array",
                  "items": {"type": "string"},
                  "description": "多个节点 id；需要多个详情时优先一次传入",
              },
              "query": {"type": "string", "description": "模糊查询标题、prompt、状态、错误、input/output 等文本"},
              "regex": {
                  "oneOf": [
                      {"type": "string"},
                      {"type": "array", "items": {"type": "string"}},
                  ],
                  "description": "可选正则或正则列表，用于查候选节点详情。",
              },
              "pattern": {
                  "oneOf": [
                      {"type": "string"},
                      {"type": "array", "items": {"type": "string"}},
                  ],
                  "description": "regex 的别名。",
              },
              "case_sensitive": {"type": "boolean"},
              "limit": {"type": "integer", "description": "query/regex 查询最多读取多少个详情；默认 20，0 为全部。"},
          },
      })
    R("node.update", node_universal.node_update, tags=["node", "write"],
      description=(
          "局部修改一个或少量节点。patch.title/status/prompt 写节点列；patch.input_json 写节点 fields 并与旧 input 局部合并。"
          "多个节点不同改动用 updates；多个节点同一 patch 可传 node_ids。"
          "复杂或高风险更新要分批。"
          "修 image 分辨率必须写精确像素，例如 2560x1440。"
          "降规格、修 prompt/依赖后在同一节点 node.run(action='force')。"
          "output_json 是生成结果，不用于写 prompt。"
      ),
      schema={
          "type": "object",
          "properties": {
              "project_id": {"type": "string"},
              "node_id": {"type": "string"},
              "node_ids": {
                  "type": "array",
                  "items": {"type": "string"},
              },
              "patch": {
                  "type": "object",
                  "additionalProperties": True,
                  "properties": {
                      "title": {"type": "string"},
                      "status": {"type": "string"},
                      "prompt": {"type": "string"},
                      "input_json": _node_object_schema(_node_update_input_properties()),
                      "output_json": {"type": "object", "additionalProperties": True},
                  },
              },
              "updates": {
                  "type": "array",
                  "items": {
                      "type": "object",
                      "additionalProperties": True,
                      "properties": {
                          "node_id": {"type": "string"},
                          "patch": {"type": "object", "additionalProperties": True},
                      },
                      "required": ["node_id", "patch"],
                  },
              },
          },
      })
    R("node.list", node_universal.node_list, tags=["node", "read"],
      description=(
          "列出项目画布节点索引，默认返回 20 个节点的 id/title/status/prompt_preview。"
          "id 是项目内从 0 开始的节点编号。支持 query/regex 模糊找候选；需要更多索引时传 limit；limit=0 返回全部匹配节点；详情用 node.get(node_ids=[...]) 批量读取。"
      ),
      schema={
          "type": "object",
          "properties": {
              "project_id": {"type": "string"},
              "type": {"type": "string", "enum": ["text", "image", "video", "audio"]},
              "status": {"type": "string"},
              "surface": {"type": "string", "enum": ["project_panel", "draft_canvas"]},
              "query": {"type": "string", "description": "模糊查询标题、prompt、状态、错误、input/output 等文本"},
              "regex": {
                  "oneOf": [
                      {"type": "string"},
                      {"type": "array", "items": {"type": "string"}},
                  ],
                  "description": "可选正则或正则列表。",
              },
              "pattern": {
                  "oneOf": [
                      {"type": "string"},
                      {"type": "array", "items": {"type": "string"}},
                  ],
                  "description": "regex 的别名。",
              },
              "case_sensitive": {"type": "boolean"},
              "limit": {
                  "type": "integer",
                  "description": "默认 20；可传更大值，最大 800；传 0 返回全部匹配节点索引。",
              },
          },
      })
    R("vision.view_image", vision_tools.view_image, tags=["vision", "read"],
      description=(
          "读取项目内已有图片并把一张或多张图片像素附加给主模型上下文。"
          "需要看清图片细节时，先用 node.list/node.get 定位 node_id；可用 node_ids/sources 批量查看，工具不输出视觉摘要。"
      ),
      schema={
          "type": "object",
          "properties": {
              "project_id": {"type": "string"},
              "node_id": {"type": "string", "description": "已完成 image 节点 id；优先使用"},
              "node_ids": {
                  "type": "array",
                  "items": {"type": "string"},
                  "description": "多个已完成 image 节点 id，按顺序附加",
              },
              "source": {"type": "string", "description": "项目存储内图片路径、当前项目 /api/media URL 或远程图片 URL"},
              "sources": {
                  "type": "array",
                  "items": {"type": "string"},
                  "description": "多个项目存储图片路径、当前项目 /api/media URL 或远程图片 URL",
              },
              "detail": {"type": "string", "enum": ["high"], "default": "high"},
              "max_images": {"type": "integer", "minimum": 1, "maximum": 32, "description": "本次最多附加图片数，默认 8"},
          },
      })
    R("node.run", node_universal.node_run, tags=["node", "execute"],
      description=(
        "执行已有 text/image/video/audio 节点并保存产物。需要节点已具备可运行输入；"
        "text 节点只保存已有 fields.content，不会替模型起草脚本或提示词；"
        "节点运行前先按当前 skill 和用户要求检查内容/prompt、fields 和依赖；"
        "不符合时先 node.update 修原节点，不要只改无关字段后重跑；"
        "复杂或高风险创作节点可用 agent.review 辅助检查内容、字段和依赖；"
        "action='force' 用于重跑，extra_fields 只对本次运行生效。"
      ))
    # project.*
    R("project.list", project_tools.project_list, tags=["project", "read"])
    R("project.create", project_tools.project_create, tags=["project", "write"],
      description=(
        "新建一个空白项目并自动切换为当前项目。仅在用户明确要求创建/打开新的空项目壳时调用；"
        "用户要制作视频、短剧、分镜、人物或其他创作内容时不要调用。"
        "调用后本轮会立即结束,你只需要回一句\"已为你创建项目 <title>,接下来想做什么?\","
        "不要继续在本轮里调其他创作工具(此时旧的 project_id 已失效)。"
        "默认 episode_count=1、format='竖屏短剧'、budget_level='low',用户没明确说就用默认。"
      ))
    R("project.get_state", project_tools.project_get_state, tags=["project", "read"],
      description="读取项目完整状态：节点、任务、参考图、确认状态和 token 使用。每轮先读。")

    # blueprint.* tools are intentionally not registered for the Agent surface.
    # Current production works directly on canvas nodes; legacy blueprint helpers
    # remain importable Python functions for old data migration and focused tests.

    # drama.* raw runners are intentionally unregistered. node.run calls the
    # internal Python functions directly; prompt templates may still use these
    # names as LLM task keys.
    R("drama.parse_uploaded_script", drama_tools.parse_uploaded_script, tags=["drama", "ingest"])

    # drama.* legacy segment wrappers live in app.services.drama_legacy and are
    # intentionally unregistered.
    # Additional drama raw runners, including segment/storyboard/prompt runners,
    # are intentionally unregistered.

    # Legacy drama destructive wrappers are intentionally unregistered.
    # canvas.delete is the single agent-facing canvas deletion primitive.
    R("project.reset", drama_tools.reset_project,
      tags=["project", "destructive"],
      description=(
        "重置项目。scope='full' 只在当前用户明确要求重置或清空整个项目时使用，"
        "首次调用只创建确认卡，确认后由后端注入安全 token 执行；"
        "scope='failed' 只清理失败或无产出的废节点，不改项目 state。"
      ),
      schema={
          "type": "object",
          "properties": {
              "scope": {
                  "type": "string",
                  "enum": ["failed", "full"],
                  "default": "failed",
                  "description": "failed=只清失败/无产出节点；full=全量重置并需要确认",
              },
              "reason": {"type": "string", "description": "展示给用户的重置原因摘要"},
              "new_theme": {
                  "type": "object",
                  "description": "可选；全量重置后立即应用的新主题字段",
                  "properties": {
                      "title": {"type": "string"},
                      "genre": {"type": "string"},
                      "description": {"type": "string"},
                      "format": {"type": "string"},
                      "episode_count": {"type": "integer"},
                      "duration_per_episode": {"type": "integer"},
                      "budget_level": {"type": "string"},
                  },
              },
          },
      })
    # canvas.* — keep only low-frequency graph operations not covered by node.*.
    # CRUD/list/layout wrappers are intentionally unregistered.
    R("canvas.delete", canvas_tools.delete_canvas, tags=["canvas", "destructive"],
      description=(
        "删除指定画布节点或清空画布,并清理这些节点的本地生成产物。"
        "scope='selected' 时传 node_ids；scope='all' 时清空当前项目画布。"
        "它不清 project state、任务或标题；用户说重置项目才用 project.reset。"
      ))

    # scene / shot / asset
    R("scene.list", shot_tools.list_scenes, tags=["scene", "read"])
    R("shot.list", shot_tools.list_shots, tags=["shot", "read"])
    R("asset.list", shot_tools.list_assets, tags=["asset", "read"])

    # media generation is now an internal service behind node.run. Keep only
    # query/control/provider tools in the registry; raw media.generate_* wrappers
    # are intentionally unregistered.
    R("media.cancel_image_generation", media_tools.cancel_image_generation,
      tags=["media", "control"],
      description=(
        "停止当前项目正在进行的图片生成或后续图片生成步骤。"
        "当用户说停止、取消、中止图片生成时调用。"
      ))
    R("media.describe_image", media_tools.describe_image, tags=["media", "vision"])
    R("media.get_presets", media_tools.get_presets, tags=["media", "read"])

    R("reference.manage", reference_tools.reference_manage,
      tags=["reference", "vision", "memory"],
      schema={
          "type": "object",
          "properties": {
              "project_id": {"type": "string", "description": "项目 ID"},
              "action": {
                  "type": "string",
                  "description": "register|ingest_attachments|list|resolve|get|alias|analyze|bind_to_blueprint|save_to_user_memory",
              },
              "rel_path": {"type": "string", "description": "上传相对路径"},
              "source_path": {"type": "string", "description": "本地文件路径"},
              "library_path": {"type": "string", "description": "资产库路径"},
              "url": {"type": "string", "description": "图片 URL"},
              "asset_id": {"type": "string", "description": "资产 ID"},
              "node_id": {"type": "string", "description": "图片节点 ID"},
              "mention": {"type": "string", "description": "@引用名"},
              "ref_id": {"type": "string", "description": "参考资产 ID"},
              "query": {"type": "string", "description": "按别名/标题/风格检索"},
              "attachments": {
                  "type": "array",
                  "items": {"type": "object", "additionalProperties": True},
                  "description": "上传附件",
              },
              "attachment_aliases": {
                  "type": "array",
                  "items": {"type": "string"},
                  "description": "附件 @别名",
              },
              "attachment_roles": {
                  "type": "array",
                  "items": {"type": "string"},
                  "description": "附件用途",
              },
              "role": {"type": "string", "description": "style_reference|character_reference|scene_reference|composition_reference|visual_reference"},
              "roles": {
                  "type": "array",
                  "items": {"type": "string"},
                  "description": "多个用途",
              },
              "alias": {"type": "string", "description": "新 @别名"},
              "apply_to": {
                  "type": "array",
                  "items": {"type": "string"},
                  "description": "绑定范围",
              },
              "user_context": {"type": "string", "description": "用途上下文"},
              "include_analysis": {"type": "boolean", "description": "返回分析"},
              "save_user_memory": {"type": "boolean", "description": "保存长期记忆"},
              "force": {"type": "boolean", "description": "强制重分析"},
          },
          "required": ["project_id", "action"],
      },
      description=(
          "管理参考图资产、@别名、视觉分析和长期保存。上传附件用 ingest_attachments，"
          "已有图片用 register/alias/resolve/get；写节点时使用返回的 reference_input。"
          "该工具不生成媒体，也不会在未请求时写长期记忆。"
      ))

    # file.*
    R("file.list_dir", file_tools.list_dir, tags=["file", "read"])
    R("file.read_text", file_tools.read_text, tags=["file", "read"],
      description=(
          "读取用户上传文件或用户本轮明确给出的项目存储相对路径。"
          "rel_path 只接受上传结果或用户明确路径，可用 offset/limit 分页；"
          "guide、节点、trace 和 tool result 状态查询使用对应工具。"
      ),
      usage_hints=[
        "file.read_text(project_id=project_id, rel_path='uploads/script.txt', offset=1, limit=50)",
      ])
    R("file.extract_text_from_upload", file_tools.extract_text_from_upload, tags=["file", "read"])
    R("file.workspace_list", file_tools.workspace_list, tags=["file", "read"],
      schema={
          "type": "object",
          "properties": {
              "path": {"type": "string", "description": "workspace 相对路径；空字符串表示项目根目录"},
              "query": {"type": "string", "description": "可选模糊过滤文件/目录条目元信息"},
              "regex": {
                  "oneOf": [
                      {"type": "string"},
                      {"type": "array", "items": {"type": "string"}},
                  ],
                  "description": "可选正则或正则列表，过滤文件/目录条目元信息。",
              },
              "pattern": {
                  "oneOf": [
                      {"type": "string"},
                      {"type": "array", "items": {"type": "string"}},
                  ],
                  "description": "regex 的别名。",
              },
              "case_sensitive": {"type": "boolean", "description": "是否大小写敏感，默认 false"},
              "recursive": {"type": "boolean", "description": "是否递归列出子目录"},
              "max_entries": {"type": "integer", "description": "最多返回条目数，默认 200，上限 2000"},
          },
      },
      description="列出当前 workspace 内的文件和目录，支持 query/regex 过滤，不执行 shell 命令。",
      usage_hints=["tool.execute(name='file.workspace_list', input={'path': 'apps/api', 'recursive': False})"])
    R("file.workspace_search", file_tools.workspace_search, tags=["file", "read"],
      schema={
          "type": "object",
          "properties": {
              "query": {"type": "string", "description": "要搜索的文件名或文本内容；空字符串只按 glob 返回文件"},
              "path": {"type": "string", "description": "workspace 相对起点；空字符串表示项目根目录"},
              "glob": {"type": "string", "description": "文件路径 glob，例如 '*.py' 或 'apps/api/**/*.py'"},
              "regex": {
                  "oneOf": [
                      {"type": "string"},
                      {"type": "array", "items": {"type": "string"}},
                  ],
                  "description": "可选正则或正则列表，匹配文件路径或文本行。",
              },
              "pattern": {
                  "oneOf": [
                      {"type": "string"},
                      {"type": "array", "items": {"type": "string"}},
                  ],
                  "description": "regex 的别名。",
              },
              "case_sensitive": {"type": "boolean", "description": "是否大小写敏感，默认 false"},
              "recursive": {"type": "boolean", "description": "是否递归搜索"},
              "include_content": {"type": "boolean", "description": "是否搜索文本内容"},
              "max_results": {"type": "integer", "description": "最多返回匹配数，默认 50，上限 500"},
              "max_file_bytes": {"type": "integer", "description": "单文件内容搜索字节上限，默认 200000"},
          },
      },
      description="在当前 workspace 内按文件名或文本内容搜索，支持 query/regex，不执行 shell 命令。",
      usage_hints=["tool.execute(name='file.workspace_search', input={'query': 'AgentOrchestrator', 'glob': '*.py'})"])
    R("file.workspace_read", file_tools.workspace_read, tags=["file", "read"],
      schema={
          "type": "object",
          "properties": {
              "path": {"type": "string", "description": "workspace 相对文件路径"},
              "mode": {"type": "string", "description": "text 或 base64，默认 text"},
              "max_bytes": {"type": "integer", "description": "最大读取字节数，默认 1000000，上限 10000000"},
              "offset": {"type": "integer", "description": "按行读取时的起始行，1-based"},
              "limit": {"type": "integer", "description": "按行读取时的最大行数"},
          },
          "required": ["path"],
      },
      description="读取当前 workspace 内的文件内容，支持文本、base64 和行范围，不执行 shell 命令。",
      usage_hints=["tool.execute(name='file.workspace_read', input={'path': 'README.md', 'offset': 1, 'limit': 80})"])
    R("file.workspace_write", file_tools.workspace_write, tags=["file", "write"],
      schema={
          "type": "object",
          "properties": {
              "path": {"type": "string", "description": "workspace 相对文件路径"},
              "content": {"type": "string", "description": "要写入的 UTF-8 文本"},
              "overwrite": {"type": "boolean", "description": "目标存在时是否覆盖，默认 true"},
              "append": {"type": "boolean", "description": "是否追加写入；true 时不覆盖"},
              "create_dirs": {"type": "boolean", "description": "是否自动创建父目录，默认 true"},
          },
          "required": ["path", "content"],
      },
      description="写入当前 workspace 内的文本文件，不执行 shell 命令；拒绝修改 .git。",
      usage_hints=["tool.execute(name='file.workspace_write', input={'path': 'tmp/notes.txt', 'content': 'hello\\n'})"])
    R("file.workspace_patch", file_tools.workspace_patch, tags=["file", "write"],
      schema={
          "type": "object",
          "properties": {
              "path": {"type": "string", "description": "workspace 相对文本文件路径"},
              "old_text": {"type": "string", "description": "要精确匹配替换的旧文本"},
              "new_text": {"type": "string", "description": "替换后的新文本"},
              "occurrence": {"type": "integer", "description": "替换第几处，1-based；0 表示替换全部"},
          },
          "required": ["path", "old_text", "new_text"],
      },
      description="按精确文本替换修改当前 workspace 内的文本文件，不执行 shell 命令；拒绝修改 .git。",
      usage_hints=["tool.execute(name='file.workspace_patch', input={'path': 'tmp/notes.txt', 'old_text': 'old', 'new_text': 'new'})"])
    R("file.workspace_delete", file_tools.workspace_delete, tags=["file", "destructive"],
      schema={
          "type": "object",
          "properties": {
              "path": {"type": "string", "description": "workspace 相对文件或目录路径"},
              "recursive": {"type": "boolean", "description": "删除目录时必须为 true"},
              "force": {"type": "boolean", "description": "路径不存在时是否仍返回 ok"},
          },
          "required": ["path"],
      },
      description="删除当前 workspace 内的文件或目录，不执行 shell 命令；删除目录需 recursive=true，拒绝删除 .git 或 workspace 根目录。",
      usage_hints=["tool.execute(name='file.workspace_delete', input={'path': 'tmp/notes.txt'})"])

    # memory.*
    R("memory.save_fact", memory_tools.memory_save_fact, tags=["memory"])
    R("memory.recall", memory_tools.memory_recall, tags=["memory", "read"])
    R("memory.compact_context", memory_tools.memory_compact_context, tags=["memory"],
      description=(
          "在上下文接近上限时保存 transcript、提炼长期事实，并用背景摘要加 token 预算内的真实尾部替换旧聊天。"
          "通常由 orchestrator 自动触发；target_tail_tokens 只调整尾部 token 预算，不使用固定消息条数窗口。"
      ))
    R("memory.save_user_fact", memory_tools.memory_save_user_fact, tags=["memory", "user"])
    R("memory.recall_user", memory_tools.memory_recall_user, tags=["memory", "user", "read"])

    # config.* — 统一配置总览（LLM / 图片 / 视频 / API Keys）
    # config.* — runtime.jsonc 文件即真相源；唯一对外写入口
    R("config.read", config_tools.config_read, tags=["config", "read"],
      description="读 runtime 配置（结构化），默认 mask api_key")
    R("config.read_file", config_tools.config_read_file, tags=["config", "read"],
      description="读原始 JSONC 文本 + 结构 + 校验状态（UI 编辑器用）")
    R("config.validate", config_tools.config_validate, tags=["config", "read"],
      description="干跑校验给定配置内容，不写入")
    # feature.* — unified feature flags and kill switches
    R("feature.list", feature_tools.feature_list, tags=["feature", "read"],
      description="列出统一 feature flag 和 kill switch 状态。")
    R("feature.is_enabled", feature_tools.feature_is_enabled, tags=["feature", "read"],
      description="查询某个 feature flag 当前是否启用，以及是否被 kill switch 强制关闭。")

    # agent.* — meta + 四种协作模式
    R("agent.map_reduce", agent_tools.agent_map_reduce, tags=["agent", "mode"],
      description="Map-Reduce 模式:并行扇出 N 个独立子任务,可选 LLM 聚合摘要(三模型对比、候选图、独立配角)。")
    R("agent.pipeline", agent_tools.agent_pipeline, tags=["agent", "mode"],
      description="Pipeline 模式:顺序管道,前一阶段产出按 carry_keys 注入下一阶段(场景→分镜→视频提示词)。")
    R("agent.hierarchical", agent_tools.agent_hierarchical, tags=["agent", "mode"],
      description="Hierarchical 模式:每个 split 内部可继续走 map_reduce/pipeline(多集并行,每集再分发段任务)。")
    R("agent.review", agent_tools.agent_review, tags=["agent", "review", "read"],
      description=(
          "隔离运行通用只读审查子 Agent，用真实项目状态、任务、计划、节点、指南和文件审查主 Agent 指定目标。"
          "复杂视频节点批次或任务需要第二视角时传 review_goal、user_request、work_summary、review_profile、evidence、guide_topics/focus。"
          "媒体运行前可用它批量检查 prompt 是否符合 skill、字段是否可执行、依赖是否使用真实 node id。"
          "自定义检查 skill 可放在 skills/review/<key>.md，或通过 review_skill_key 指定。"
          "返回 pass/revise_required/blocked 等结果；主 Agent 只修有 evidence 或 violated_requirement 的具体问题。"
      ))
    # panel.* — project-level panel view (mode/axis switching)
    # media.* — provider configuration (image active; video stub)
    R("media.list_providers", media_provider_tools.media_list_providers, tags=["media", "provider", "read"])
    R("media.test_provider", media_provider_tools.media_test_provider, tags=["media", "provider", "meta"])

    # assets.* — user-designated asset library (project + shared roots)
    R("assets.get_library_path", asset_library_tools.assets_get_library_path, tags=["assets", "read"])
    R(
        "assets.save_to_project",
        asset_library_tools.assets_save_to_project,
        tags=["assets", "write"],
        description="把节点、资产记录或本地文件显式保存到当前项目资产库。",
        usage_hints=[
            "tool.execute(name='assets.save_to_project', input={'episode': 1, 'kind': 'scene', 'source': 'node:12', 'name': '场景名'})",
        ],
    )
    R(
        "assets.save_to_shared",
        asset_library_tools.assets_save_to_shared,
        tags=["assets", "write"],
        description="把人物或场景素材显式保存到共享资产库。",
        usage_hints=[
            "tool.execute(name='assets.save_to_shared', input={'kind': 'character', 'category': 'female_young', 'source': 'node:12', 'name': '角色名'})",
        ],
    )
    R("assets.list_project", asset_library_tools.assets_list_project, tags=["assets", "read"])
    R("assets.list_shared", asset_library_tools.assets_list_shared, tags=["assets", "read"])
    R("assets.read_asset", asset_library_tools.assets_read_asset, tags=["assets", "read"])

    # Legacy generic skill management wrappers are intentionally unregistered,
    # keeping registry focused on concrete skill primitives.
    for legacy_skill_tool_name in UNREGISTERED_GENERIC_SKILL_TOOL_NAMES:
        target_registry.unregister(legacy_skill_tool_name)

    return target_registry


_register_builtins()


# ─────────────────────────────────────────────────────────────────────────
# Skill loading. Each skill lives at apps/api/app/skills/<name>/
# with at least a SKILL.md (YAML frontmatter) and a Python entry point
# whose import-time `@register(...)` calls populate the registry.
# Flat single-file modules under skills/*.py are also loaded for backwards
# compatibility.
# ─────────────────────────────────────────────────────────────────────────

from pathlib import Path


def parse_skill_md(text: str) -> dict[str, Any]:
    """Tiny YAML-frontmatter parser (key: value lines + simple lists).
    Avoids a PyYAML dep for what is intentionally a tiny schema."""
    if not text.startswith("---"):
        return {"_body": text}
    end = text.find("\n---", 3)
    if end < 0:
        return {"_body": text}
    head = text[3:end].strip()
    body = text[end + 4 :].lstrip("\n")

    out: dict[str, Any] = {}
    current_list_key: str | None = None
    for raw in head.splitlines():
        if not raw.strip():
            current_list_key = None
            continue
        if raw.startswith("  - ") or raw.startswith("- "):
            if current_list_key is None:
                continue
            value = raw.split("-", 1)[1].strip().strip('"').strip("'")
            out.setdefault(current_list_key, []).append(value)
            continue
        if ":" in raw:
            key, _, value = raw.partition(":")
            key = key.strip()
            value = value.strip()
            if value == "" or value == "[]":
                out[key] = []
                current_list_key = key
            elif value.startswith("[") and value.endswith("]"):
                inner = value[1:-1].strip()
                items = [x.strip().strip('"').strip("'") for x in inner.split(",") if x.strip()]
                out[key] = items
                current_list_key = None
            else:
                out[key] = value.strip('"').strip("'")
                current_list_key = None
    out["_body"] = body
    return out


def _load_skill_dir(package: str, skill_dir: Path) -> str | None:
    """Import skills/<name>/ as a package and return its dotted module name."""
    import importlib

    name = skill_dir.name
    skill_md = skill_dir / "SKILL.md"
    metadata: dict[str, Any] = {}
    if skill_md.exists():
        metadata = parse_skill_md(skill_md.read_text(encoding="utf-8"))

    init_file = skill_dir / "__init__.py"
    if not init_file.exists():
        return None

    full = f"{package}.{name}"
    module = importlib.import_module(full)

    # Attach metadata to any tools the module just registered. We match by
    # tool name == metadata.get("tool_name") OR namespace skill.<name>.
    tool_name = metadata.get("tool_name")
    if tool_name and tool_name in registry._tools:
        spec = registry._tools[tool_name]
        spec.metadata.update(metadata)
        if not spec.search_hint and metadata.get("search_hint"):
            spec.search_hint = str(metadata.get("search_hint") or "")
        if not spec.usage_hints and metadata.get("usage_hints"):
            hints = metadata.get("usage_hints")
            if isinstance(hints, str):
                hints = [hints]
            if isinstance(hints, list):
                spec.usage_hints = [str(item) for item in hints if str(item).strip()]

    return full


def load_skills(package: str = "app.skills") -> list[str]:
    """Import every skill under `package`. Supports two layouts:
      - skills/<name>/  (preferred — has SKILL.md + __init__.py)
      - skills/<name>.py (legacy flat module)
    Returns dotted module names that were loaded.
    """
    import importlib
    import pkgutil

    try:
        pkg = importlib.import_module(package)
    except ModuleNotFoundError:
        return []

    loaded: list[str] = []
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        full = f"{package}.{mod_info.name}"
        if mod_info.ispkg:
            skill_path = Path(pkg.__path__[0]) / mod_info.name
            result = _load_skill_dir(package, skill_path)
            if result:
                loaded.append(result)
        else:
            importlib.import_module(full)
            loaded.append(full)
    return loaded


def reload_skills(package: str = "app.skills") -> list[str]:
    """Drop every previously-registered skill tool and reimport.

    Only tools whose metadata['source'] == 'skill' are removed. Generic skill
    management wrappers are no longer registered; concrete skill tools are
    reloaded from their packages.
    """
    import importlib
    import sys

    to_remove = [
        name for name, spec in registry._tools.items()
        if spec.metadata.get("source") == "skill"
    ]
    for name in to_remove:
        registry.unregister(name)

    # purge cached modules so import re-runs
    prefix = package + "."
    for mod_name in list(sys.modules):
        if mod_name == package or mod_name.startswith(prefix):
            del sys.modules[mod_name]

    loaded = load_skills(package)
    _apply_standard_tool_descriptions()
    return loaded


load_skills()
_apply_standard_tool_descriptions()
