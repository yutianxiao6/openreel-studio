"""Tool registry — central catalog of every callable tool the Agent has.

Why:
- One canonical list (name → handler + schema + namespace + description) so the
  planner, the MCP server export, and the docs all read from the same source.
- Tool modules and plugins can register tools by calling `register(...)`
  at import time. The agent's prompt context auto-picks them up.
- A tool can be looked up by full name (`node.run`) and invoked
  with a kwargs dict, regardless of which python module defined it.

Usage:
    from app.mcp_tools.registry import registry
    handler = registry.get("node.run")
    result = await handler(node_id=...)

Tool author:
    from app.mcp_tools.registry import register
    @register("plugin.do_thing", description="...", schema={...})
    async def do_thing(project_id: str, x: int): ...
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from types import UnionType
from typing import Any, Awaitable, Callable, Union, get_args, get_origin, get_type_hints

from app.agent.model_context.policy import (
    COLLECTION_OUTPUT_POLICY,
    DELEGATED_OUTPUT_POLICY,
    DOCUMENT_OUTPUT_POLICY,
    JSON_OUTPUT_POLICY,
    MULTIMODAL_OUTPUT_POLICY,
    ToolOutputPolicy,
)

ToolHandler = Callable[..., Awaitable[Any]]


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
}


def _hide_runtime_context_schema(schema: Any) -> Any:
    """Hide parameters that the chat harness injects deterministically."""
    if isinstance(schema, dict):
        normalized = {key: _hide_runtime_context_schema(value) for key, value in schema.items()}
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
        normalized = {key: _llm_compatible_schema(value) for key, value in schema.items()}
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
                                "video_reference",
                                "audio_reference",
                                "vision_context",
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
            "description": "图片精确像素尺寸，格式 <width>x<height>，如 1080x1920。",
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
        "generation": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "对最近用户文本执行的完整转换要求；runner 只输出最终正文。",
                },
                "source_message_count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8,
                    "description": "读取最近几条用户消息作为原始素材；同轮素材用 1，上一轮素材加当前要求通常用 2。",
                },
                "source_text": {
                    "type": "string",
                    "description": "直接工具调用时可提供原始文本；Agent 优先使用 source_message_count，避免重复长文本。",
                },
            },
            "required": ["instruction", "source_message_count"],
        },
        **_node_media_field_properties(),
        "duration_seconds": {"type": "number"},
        "production_path": {"type": "string"},
        "purpose": {"type": "string"},
        "references": _node_reference_array_schema(
            description="上游引用；字符串或 {ref, role} 对象。节点用 node:<编号>，上传图用 upload:<rel_path>，资产用 asset:<id> 或资产路径。"
        ),
    }
    return properties


def _node_update_input_properties() -> dict[str, Any]:
    """Fields accepted under node.update patch.input_json."""
    return {
        **_node_media_field_properties(),
        "references": _node_reference_array_schema(
            description="局部更新上游引用；节点用 node:<编号>，上传图用 upload:<rel_path>，资产用 asset:<id> 或资产路径。"
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
    metadata: dict[str, Any] = field(default_factory=dict)
    search_hint: str = ""           # extra deferred-search index text, not shown as full prompt
    usage_hints: list[str] = field(default_factory=list)  # short retrieval-oriented hints
    is_read_only: bool = False
    is_destructive: bool = False
    requires_confirmation: bool = False
    is_concurrency_safe: bool = False
    output_policy: ToolOutputPolicy = JSON_OUTPUT_POLICY

    @property
    def short_name(self) -> str:
        return self.name.split(".", 1)[1] if "." in self.name else self.name


@dataclass(frozen=True)
class ToolRuntimeMetadata:
    name: str
    namespace: str
    exposure: str
    description: str
    tags: tuple[str, ...]
    search_hint: str
    usage_hints: tuple[str, ...]
    is_read_only: bool
    is_destructive: bool
    requires_confirmation: bool
    is_concurrency_safe: bool
    output_policy: ToolOutputPolicy

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "namespace": self.namespace,
            "exposure": self.exposure,
            "description": self.description,
            "tags": list(self.tags),
            "search_hint": self.search_hint,
            "usage_hints": list(self.usage_hints),
            "is_read_only": self.is_read_only,
            "is_destructive": self.is_destructive,
            "requires_confirmation": self.requires_confirmation,
            "is_concurrency_safe": self.is_concurrency_safe,
            "output_policy": self.output_policy.as_dict(),
        }


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
        output_policy: ToolOutputPolicy | None = None,
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
            is_read_only=bool(is_read_only)
            if is_read_only is not None
            else bool(meta.get("is_read_only", False)),
            is_destructive=bool(is_destructive)
            if is_destructive is not None
            else bool(meta.get("is_destructive", False)),
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
            output_policy=output_policy or JSON_OUTPUT_POLICY,
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
        return self.tool_exposure_for_profile(name)

    def tool_exposure_for_profile(self, name: str, profile: str | None = None) -> str:
        """Return exposure tier for a tool under the selected core profile."""
        spec = self.get(name)
        if spec is None:
            return "unregistered"
        if name in self._AGENT_HIDDEN:
            return "hidden"
        if name in self._core_tool_names_for_profile(profile):
            return "core"
        if name in self._TIER1_EXTRA or spec.namespace in self._TIER1_NS:
            return "core"
        return "deferred"

    def runtime_metadata(self, name: str, profile: str | None = None) -> ToolRuntimeMetadata | None:
        spec = self.get(name)
        if spec is None:
            return None
        return ToolRuntimeMetadata(
            name=spec.name,
            namespace=spec.namespace,
            exposure=self.tool_exposure_for_profile(name, profile),
            description=spec.description,
            tags=tuple(spec.tags),
            search_hint=spec.search_hint,
            usage_hints=tuple(spec.usage_hints),
            is_read_only=spec.is_read_only,
            is_destructive=spec.is_destructive,
            requires_confirmation=spec.requires_confirmation,
            is_concurrency_safe=spec.is_concurrency_safe,
            output_policy=spec.output_policy,
        )

    def runtime_manifest(self, profile: str | None = None) -> list[dict[str, Any]]:
        return [
            metadata.as_dict()
            for metadata in (
                self.runtime_metadata(spec.name, profile)
                for spec in sorted(self._tools.values(), key=lambda item: item.name)
            )
            if metadata is not None
        ]

    def core_agent_tool_names(self, profile: str = "default") -> set[str]:
        core_names = self._core_tool_names_for_profile(profile)
        return {
            name for name in self._tools if name in core_names and name not in self._AGENT_HIDDEN
        }

    def deferred_tool_names(self) -> set[str]:
        return {name for name in self._tools if self.tool_exposure(name) == "deferred"}

    def agent_hidden_tool_names(self) -> set[str]:
        return {name for name in self._tools if self.tool_exposure(name) == "hidden"}

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
                "output_policy": t.output_policy.as_dict(),
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
            result.append(
                {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description or spec.name,
                    "parameters": params,
                },
                }
            )
        return result

    # ── Tier 设计 ─────────────────────────────────────────────────────
    # Tier 1: 完整 schema 始终注入 — 创作主路径,频繁调用,不绕弯
    # Tier 2: 只露 {name, description}; node-first 默认路径不再依赖
    # tool.search/tool.describe/tool.execute 发现业务流程。
    # Tier 3: 完全隐藏,等价 _AGENT_HIDDEN
    _TIER1_NS: set[str] = set()
    _TIER1_EXTRA: set[str] = set()

    # Hidden from Agent Loop; these remain available to settings, diagnostics,
    # image editing workers, or workflow inspection paths.
    _AGENT_HIDDEN = {
        "media.get_presets",
        "media.list_providers",
        "media.test_provider",
        "image.edit",
        "image.segment",
        "image.grid_split",
        "image.grid_combine",
        "image.extract_grid_cell",
        "image.place_grid_cell",
        "image.inpaint_region",
        "memory.recall_user",
        "config.read",
        "config.read_file",
        "config.validate",
    }

    # Stable core tool surface for the Agent Loop. The node-first path discovers
    # business workflow through the runtime skill catalog and scoped skill reads, and exposes primitives to
    # read state, ask users, maintain a lightweight task ledger, and
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
        "skills.list",
        "skills.read",
        "task.complete",
        "task.create",
        "task.list",
        "task.update",
        "tool.describe",
        "tool.execute",
        "tool.search",
        "vision.view_image",
    }
    _WORKFLOW_BUILD_CORE_TOOLS: set[str] = {
        "interaction.request_input",
        "project.get_state",
        "skills.list",
        "skills.read",
        "workflow.canvas.inspect",
        "workflow.spec.apply_patch",
        "workflow.spec.read",
        "workflow.template.export",
        "workflow.template.read",
        "workflow.template.resolve",
    }
    _CORE_TOOL_PROFILES: dict[str, set[str]] = {
        "default": _CORE_AGENT_TOOLS,
        "workflow_build": _WORKFLOW_BUILD_CORE_TOOLS,
    }
    @classmethod
    def _core_tool_names_for_profile(cls, profile: str | None = None) -> set[str]:
        key = str(profile or "default").strip().lower() or "default"
        return set(cls._CORE_TOOL_PROFILES.get(key) or cls._CORE_AGENT_TOOLS)

    def get_tools_for_agent_loop(
        self,
        profile: str = "default",
    ) -> list[dict[str, Any]]:
        """Export the selected stable core tool profile for the Agent Loop."""
        core_names = self._core_tool_names_for_profile(profile)
        specs = [
            spec
            for spec in sorted(self._tools.values(), key=lambda item: item.name)
            if spec.name in core_names and spec.name not in self._AGENT_HIDDEN
        ]
        result: list[dict[str, Any]] = []
        for spec in specs:
            params = spec.schema if spec.schema else _schema_from_handler(spec.handler)
            params = _compact_agent_schema(params)
            params = _hide_runtime_context_schema(params)
            params = _llm_compatible_schema(params)
            result.append(
                {
                "type": "function",
                "function": {
                    "name": spec.name.replace(".", "__"),
                    "description": spec.description or spec.name,
                    "parameters": params,
                },
                }
            )
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
    "agent.run": "把一个明确职责的任务委派给专职子 Agent，并返回隔离执行结果",
    "assets.get_library_path": "读取资产库路径配置",
    "assets.list_project": "读取项目资产库文件列表",
    "assets.list_shared": "读取单一本地资产库文件列表",
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
    "image.edit": "对图片节点执行本地裁剪、mask/分割、涂鸦、填充、文字和箭头编辑，先产出候选图再提交",
    "image.segment": "对图片节点或图片引用生成主体分割 mask 和透明 PNG，不直接覆盖节点",
    "image.extract_grid_cell": "把宫格图片节点里的单个 cell 导出成新的图片节点",
    "image.grid_combine": "把多个同规格图片组合成图片节点内部宫格",
    "image.grid_split": "把图片节点切换为宫格编辑态并生成内部裁剪 cell",
    "image.place_grid_cell": "把图片引用放入 image_grid 的指定 cell",
    "image.inpaint_region": "对图片或宫格 cell 的局部 mask 区域发起重绘",
    "interaction.request_input": "用通用问题卡请求 1-6 个简短回答",
    "media.cancel_image_generation": "取消当前项目正在进行或排队的图片生成步骤",
    "media.get_presets": "从图片协议 catalog 读取 provider 推荐参数预设",
    "media.list_providers": "读取已配置的媒体 provider 列表",
    "media.test_provider": "向指定媒体 provider 发送最小真实请求并返回测试结果",
    "memory.compact_context": "把模型可见历史压缩并持久化为上下文检查点",
    "memory.recall": "检索当前项目的相关记忆",
    "memory.recall_user": "检索跨项目用户偏好记忆",
    "memory.save_fact": "保存当前项目级长期事实",
    "memory.save_user_fact": "保存跨项目用户偏好或稳定工作习惯",
    "node.create": "创建一个或少量 text/image/video/audio 创作节点",
    "node.get": "读取一个或多个节点详情",
    "node.list": "列出有界节点索引页",
    "node.run": "执行指定节点并由后端按节点类型派发 runner、落库状态和产物",
    "node.update": "局部更新一个或少量指定节点的允许字段",
    "project.get_state": "读取项目运行状态和画布摘要",
    "project.reset": "按 scope 清理失败节点或执行已确认的全量项目重置",
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
    "workflow.spec.apply_patch": "创建、替换或修订 workflow spec，并返回已校验保存的引用",
}

_STANDARD_CANNOT_BY_NAME: dict[str, str] = {
    "agent.review": "不能创建、修改、运行、删除、批准、重置或直接向用户提交；只返回审查结论给主 Agent",
    "agent.run": "不能绕过子 Agent 白名单、当前项目作用域或权限策略",
    "canvas.delete": "不能当作 full reset；它不清任务、项目 state 或标题",
    "config.read": "不能写配置；配置修改走设置页或 config REST 控制面",
    "config.read_file": "不能写配置；配置修改走设置页或 config REST 控制面",
    "config.validate": "不能写配置或刷新运行时状态",
    "interaction.request_input": "不能创建、修改、删除、运行、重置或批准任何项目内容；只能请求用户补充信息并等待提交",
    "media.test_provider": "不能生成正式项目资产，也不能修改 provider 配置",
    "node.create": "不能创建未列入公开类型的旧节点或 raw runner 节点，不能运行节点",
    "node.run": "不能绕过节点依赖或 readiness 错误，不能直接调用 raw drama/media runner 替代",
    "node.update": "不能把运行产物写进 prompt，也不能绕过节点字段边界",
    "project.get_state": "不能修改项目，也不能把历史上下文当成当前状态",
    "project.reset": "不能在没有当前用户明确请求和必要确认时执行 full reset",
    "task.complete": "不能在工具真实成功前标记完成",
    "task.update": "不能篡改任务图结构或绕过用户批准的执行计划",
    "tool.describe": "不能描述隐藏、注销或不存在的工具",
    "tool.execute": "不能执行核心、隐藏或已注销工具，不能绕过 permission policy",
    "tool.search": "不能返回核心、隐藏或已注销工具；目录只包含 visible deferred 工具",
    "vision.view_image": "不能分析图片、生成摘要或替模型做判断；只把图片像素附加给主模型",
    "workflow.spec.apply_patch": "不能创建画布节点、运行流程或绕过 workflow 协议校验",
}

_STANDARD_CANNOT_BY_NAMESPACE: dict[str, str] = {
    "agent": "不能绕过角色白名单、节点作用域、权限策略或破坏性确认；写能力只来自明确注册的 scoped worker",
    "assets": "不能配置资产库根路径或删除资产；保存、分类、移动和加入画布必须来自当前用户明确要求",
    "asset": "不能注册、写入或附加资产；创作资产走节点或资产服务",
    "canvas": "不能创建、删除或修改节点内容；节点 CRUD 走 node.*",
    "config": "不能写配置；配置写入走 REST 控制面",
    "events": "不能修改事件、trace 或项目状态",
    "feature": "不能修改 feature flag 或 kill switch",
    "file": "不能越过 workspace/project 存储边界或执行命令",
    "interaction": "不能执行创作、审批或状态变更；只负责把模型的问题渲染成用户输入卡片",
    "media": "不能直接生成正式图片/视频；生成走 node.run 和媒体 service",
    "memory": "不能把不稳定推测写成长期事实，不能替代任务或节点状态",
    "scene": "不能创建或修改场景；场景创作走 node.*",
    "shot": "不能创建或修改镜头；镜头创作走 node.*",
    "skills": "不能越过项目工具、权限策略或节点规则直接改状态",
    "system": "不能修改模型、工具或 MCP 配置",
    "team": "不能越过主 Agent 权限边界直接改项目核心状态",
}

_STANDARD_LIMIT_BY_NAME: dict[str, str] = {
    "interaction.request_input": "只请求用户输入，不创建、修改、删除、运行、重置或批准项目内容",
    "agent.review": "只读审查，不创建、修改、运行、删除、批准、重置或直接向用户提交",
    "node.create": "只创建节点，不运行节点",
    "canvas.delete": "删除需当前用户明确请求和结构化确认；不清任务、项目 state 或标题",
    "node.get": "只读取节点",
    "node.list": "只读取节点列表",
    "node.run": "只运行现有节点，不绕过依赖或 readiness 错误",
    "node.update": "只改允许字段，不写入不属于该节点的产物",
    "project.get_state": "只读取项目状态",
    "project.reset": "full reset 需要当前用户明确请求和确认",
    "task.complete": "只标记真实完成的任务",
    "task.list": "只读取任务列表",
    "task.update": "只更新任务状态和元数据",
    "tool.describe": "只描述 visible deferred 工具",
    "tool.execute": "只执行 deferred 工具并受 permission policy 约束",
    "tool.search": "只列出或搜索 visible deferred 工具元数据",
    "vision.view_image": "只读取并附加图片像素，不创建摘要、不修改项目",
    "workflow.spec.apply_patch": "只保存 workflow spec artifact 或用户模板，不物化画布和不执行流程",
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
    parts = [base]
    if (
        spec.is_destructive
        or spec.requires_confirmation
        or spec.name
        in {
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


_READ_ONLY_TAGS = {"read", "query"}
_MUTATING_TAGS = {"execute", "write", "control", "destructive"}
_DESTRUCTIVE_NAMES = {"project.reset", "canvas.delete"}
_CONFIRMATION_NAMES = {"project.reset", "canvas.delete"}
_READ_ONLY_VERBS = ("get", "list", "describe", "search", "status", "models", "is_enabled")


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
    if spec.namespace in {"system", "template", "feature"}:
        return True
    return bool(spec.is_read_only)


def _apply_tool_boundary_metadata(spec: ToolSpec) -> None:
    spec.is_destructive = bool(
        spec.is_destructive or "destructive" in spec.tags or spec.name in _DESTRUCTIVE_NAMES
    )
    spec.requires_confirmation = bool(
        spec.requires_confirmation
        or spec.name in _CONFIRMATION_NAMES
        or "requires_confirmation" in spec.tags
    )
    spec.is_read_only = _infer_read_only(spec)
    spec.is_concurrency_safe = bool(spec.is_concurrency_safe or spec.is_read_only)


def _standardize_tool_spec(spec: ToolSpec, target_registry: ToolRegistry | None = None) -> None:
    """Apply boundary metadata and keep core tool descriptions stable."""
    _apply_tool_boundary_metadata(spec)
    if spec.namespace == "skills":
        # These are Codex protocol tools; preserve their canonical descriptions.
        return
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
    output_policy: ToolOutputPolicy | None = None,
    replace: bool = False,
) -> Callable[[ToolHandler], ToolHandler]:
    """Decorator form for tool modules and plugins."""

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
            output_policy=output_policy,
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
        event_tools as _event_tools,  # noqa: F401 - import-time registration
        feature_tools,
        file_tools,
        image_operation_tools,
        interaction_tools,
        media_tools,
        media_provider_tools,
        memory_tools,
        node_universal,
        project_tools,
        skill_tools as _skill_tools,  # noqa: F401 - import-time registration
        system_tools as _system_tools,  # noqa: F401 - import-time registration
        task_tools as _task_tools,  # noqa: F401 - import-time registration
        tool_meta_tools,
        vision_tools,
        workflow_tools as _workflow_tools,  # noqa: F401 - import-time registration
    )

    target_registry = target or registry
    R = target_registry.register

    # ─────────────────────────────────────────────────────────────────────
    # tool.* —— 元工具,按需加载 Tier 2 工具的完整 schema
    # ─────────────────────────────────────────────────────────────────────
    R(
        "tool.describe",
        tool_meta_tools.tool_describe,
        tags=["tool", "meta", "read"],
        output_policy=COLLECTION_OUTPUT_POLICY,
      description=(
        "读取 deferred/Tier2 工具的 schema 和元数据。只描述可见按需工具；"
        "核心、隐藏和已注销工具不会通过这里展开。"
        ),
    )
    R(
        "tool.search",
        tool_meta_tools.tool_search,
        tags=["tool", "meta", "read"],
        output_policy=COLLECTION_OUTPUT_POLICY,
      description=(
        "列出或搜索 deferred/Tier2 工具目录，用于按需发现系统和低频能力；"
        "query='' 列目录，select:name 精确选择，支持关键词和 regex。"
        "只返回可见按需工具，不替模型做业务判断。"
      ),
      schema={
          "type": "object",
          "properties": {
                "query": {
                    "type": "string",
                    "description": "空字符串列 visible deferred 目录；也支持关键词、select:name,name、discover:能力描述",
                },
                "category": {
                    "type": "string",
                    "description": "可选分类，如 workflow/query/assets/system/memory/task/collab/attach/control/image/file",
                },
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
                "case_sensitive": {
                    "type": "boolean",
                    "description": "regex/query 是否大小写敏感，默认 false",
                },
                "limit": {
                    "type": "integer",
                    "description": "默认 8；传 0 返回完整目录或完整匹配结果",
          },
            },
        },
    )
    R(
        "tool.execute",
        tool_meta_tools.tool_execute,
        tags=["tool", "meta", "execute"],
        output_policy=DELEGATED_OUTPUT_POLICY,
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
        },
    )

    # ─────────────────────────────────────────────────────────────────────
    # interaction.* —— 通用用户输入/选择卡片
    # ─────────────────────────────────────────────────────────────────────
    R(
        "interaction.request_input",
        interaction_tools.request_input,
      tags=["interaction", "control"],
      description=(
          "Ask one generic card with one to six short blocking questions, then wait for submission. "
          "This interaction cannot approve or change project content."
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
                            "header": {
                                "type": "string",
                                "description": "Short header label shown in the UI",
                            },
                            "question": {
                                "type": "string",
                                "description": "Single-sentence prompt shown to the user",
                            },
                          "options": {
                              "type": "array",
                              "description": "Optional. Provide 2-3 mutually exclusive choices only when the question should be a choice; omit for free text.",
                              "minItems": 2,
                              "maxItems": 3,
                              "items": {
                                  "type": "object",
                                  "properties": {
                                        "label": {
                                            "type": "string",
                                            "description": "User-facing label",
                                        },
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
        },
    )

    # ─────────────────────────────────────────────────────────────────────
    # image.* —— 低层图片编辑能力；前端走 REST，Agent 走 agent.run(image_editor)，底层工具保持隐藏。
    # ─────────────────────────────────────────────────────────────────────
    R(
        "image.edit",
        image_operation_tools.edit,
      tags=["image", "write"],
      output_policy=MULTIMODAL_OUTPUT_POLICY,
      description="对图片节点执行本地编辑；preview 产出候选图，commit 才覆盖节点并归档历史。",
      search_hint=(
          "crop brush doodle fill cover mask segment background transparent alpha rounded rectangle annotate text arrow image edit preview commit "
          "裁剪 涂鸦 画笔 覆盖 遮挡 填充 网格 透明 文字 箭头 标注 图片编辑 候选图 提交"
      ),
	      usage_hints=[
	          "action='preview' 会生成候选图并把图片像素附加给下一轮模型上下文。",
	          "preview 返回后直接依据附加的视觉上下文判断；候选图满意后 action='commit' 并传 candidate_ref，不满意则从 base_ref/checkpoint 重新 preview。",
	          "精细透明背景和图标圆角使用 operations=[{'type':'mask','mode':'background'|'shape'|'color','effect':'transparent',...}]。",
          "tool.execute(name='image.edit', input={'node_id':'12','action':'preview','operations':[{'type':'crop','unit':'pixel','rect':{'x':0,'y':0,'width':512,'height':512}}]})",
      ],
      schema={
          "type": "object",
          "properties": {
              "node_id": {"type": "string", "description": "图片节点编号，如 12 或 #12。"},
                "action": {
                    "type": "string",
                    "enum": ["preview", "commit"],
                    "description": "preview 只生成候选图；commit 覆盖节点并写历史。",
                },
                "source_ref": {
                    "type": "string",
                    "description": "可选源图引用；默认使用 node_id 当前输出。",
                },
                "candidate_ref": {
                    "type": "string",
                    "description": "preview 返回的候选图 local_url；commit 时传它。",
                },
              "operations": {
                  "type": "array",
                  "items": {
                      "type": "object",
                      "additionalProperties": True,
                      "properties": {
                            "type": {
                                "type": "string",
                                "enum": [
                                    "crop",
                                    "brush",
                                    "fill",
                                    "mask",
                                    "selection",
                                    "segment",
                                    "text",
                                    "arrow",
                                ],
                            },
                          "unit": {"type": "string", "enum": ["normalized", "pixel"]},
                            "mode": {
                                "type": "string",
                                "description": "mask/selection/segment 模式：shape、background、color、alpha。",
                            },
                            "effect": {
                                "type": "string",
                                "description": "mask 效果：transparent/clear/erase、keep/isolate、fill、opaque。",
                            },
                            "shape": {
                                "type": "string",
                                "description": "shape 模式：rect、rounded_rect、ellipse、polygon、path。",
                            },
                            "tolerance": {
                                "type": "number",
                                "description": "background/color 模式的颜色阈值。",
                            },
                          "feather": {"type": "number", "description": "mask 边缘羽化像素。"},
                            "expand": {
                                "type": "integer",
                                "description": "扩大 mask 像素数，用于清理边缘残留。",
                            },
                          "shrink": {"type": "integer", "description": "缩小 mask 像素数。"},
                      },
                  },
                  "description": "顺序执行的编辑操作；crop/fill 用 rect 或 points；mask 可按 shape/background/color/alpha 生成选择区并透明化、保留或填色；brush 用 points/strokes，text 用 text+position，arrow 用 start/end。",
              },
      },
          "required": ["node_id"],
        },
    )
    R(
        "image.segment",
        image_operation_tools.segment,
      tags=["image", "write", "hidden"],
      description="生成主体分割 mask 和透明 PNG；不覆盖节点，供后续 image.edit 裁剪、圆角和提交。",
      search_hint=(
          "segment cutout mask alpha matte foreground subject background removal transparent png icon crop "
          "抠图 分割 主体 前景 背景移除 透明 png mask 图标 圆角"
      ),
      usage_hints=[
          "先用 image.segment 得到 cutout_ref、mask_ref、bbox，再用 image.edit 处理裁剪、正方形、圆角和 commit。",
          "复杂主体可传 rect/bbox 或 foreground_points/background_points 给 GrabCut 约束。",
      ],
      schema={
          "type": "object",
          "properties": {
                "node_id": {
                    "type": "string",
                    "description": "图片节点编号，如 12 或 #12；node_id 和 source_ref 至少传一个。",
                },
                "source_ref": {
                    "type": "string",
                    "description": "可选图片引用；可用于候选图或本地媒体 URL。",
                },
              "target": {"type": "string", "description": "分割目标，默认 main_subject。"},
                "method": {
                    "type": "string",
                    "enum": ["auto", "alpha", "background", "grabcut"],
                    "description": "分割方式；auto 依次尝试 alpha、背景洪泛和 GrabCut。",
                },
                "unit": {
                    "type": "string",
                    "enum": ["normalized", "pixel"],
                    "description": "rect 和点坐标单位。",
                },
                "rect": {
                    "type": "object",
                    "description": "可选主体大致矩形，用于 GrabCut，例如 {x,y,width,height}。",
                },
              "bbox": {"type": "object", "description": "rect 的别名。"},
                "foreground_points": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "可选前景点，格式 {x,y}。",
                },
                "background_points": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "可选背景点，格式 {x,y}。",
                },
                "background_tolerance": {
                    "type": "integer",
                    "description": "背景洪泛颜色容差，默认 28。",
                },
              "expand": {"type": "integer", "description": "扩大主体 mask 像素数。"},
              "shrink": {"type": "integer", "description": "缩小主体 mask 像素数，用于去边。"},
              "feather": {"type": "number", "description": "主体边缘羽化像素。"},
              "smooth": {"type": "integer", "description": "mask 平滑强度。"},
                "grabcut_iterations": {
                    "type": "integer",
                    "description": "GrabCut 迭代次数，默认 5。",
                },
            },
          },
    )
    R(
        "image.grid_split",
        image_operation_tools.grid_split,
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
        },
    )
    R(
        "image.grid_combine",
        image_operation_tools.grid_combine,
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
        },
    )
    R(
        "image.extract_grid_cell",
        image_operation_tools.extract_grid_cell,
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
        },
    )
    R(
        "image.place_grid_cell",
        image_operation_tools.place_grid_cell,
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
        },
    )
    R(
        "image.inpaint_region",
        image_operation_tools.inpaint_region,
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
        },
    )

    # ─────────────────────────────────────────────────────────────────────
    # node.* —— 5 个普适工具,Agent 创作的唯一入口
    # type 使用 text / image / video / audio 四类通用节点；具体制作方法写在树和字段里。
    # ─────────────────────────────────────────────────────────────────────
    R(
        "node.create",
        node_universal.node_create,
        tags=["node", "write"],
      description=(
        "创建一个或少量 text/image/video/audio 工程节点。制作流程由 active skill 或用户目标指导；"
        "短 text 正文可直接写 fields.content；用户明确要求保存长文本时，写"
        " fields.generation={instruction,source_message_count}，随后 node.run 由内部 runner 原子生成正文，"
        "不要把长正文塞进本工具参数；image/video/audio prompt 需要模型显式写入；"
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
        },
    )
    R(
        "node.get",
        node_universal.node_get,
        tags=["node", "read"],
        output_policy=DOCUMENT_OUTPUT_POLICY,
      description=(
          "读取 node_id/node_ids 的节点详情；文本正文在 content_page 中按字符偏移分页。"
      ),
      schema={
          "type": "object",
          "properties": {
              "project_id": {"type": "string"},
              "node_id": {"type": "string", "description": "单个节点 id；只查一个节点时使用"},
              "node_ids": {
                  "type": "array",
                  "items": {"type": "string"},
                  "maxItems": 20,
                  "description": "多个节点 id；每次最多 20 个，需要更多时分批读取",
              },
                "query": {
                    "type": "string",
                    "description": "模糊查询标题、prompt、状态、错误、input/output 等文本",
                },
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
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "query/regex 查询最多读取多少个详情；默认及上限均为 20。",
                },
              "content_offset": {
                  "type": "integer",
                  "description": "文本正文起始字符偏移，0-based；默认 0。",
                  "minimum": 0,
              },
              "content_limit": {
                  "type": "integer",
                  "description": "本次调用共享的正文字符预算；默认及最大 8000，0 只返回正文元数据。",
                  "minimum": 0,
                  "maximum": 8000,
              },
          },
        },
    )
    R(
        "node.update",
        node_universal.node_update,
        tags=["node", "write"],
      description=(
          "局部修改一个或少量节点。patch.title/status/prompt 写节点列；patch.input_json 写节点 fields 并与旧 input 局部合并。"
          "多个节点不同改动用 updates；多个节点同一 patch 可传 node_ids。"
          "复杂或高风险更新要分批。"
          "修 image 分辨率必须写精确像素，例如 1080x1920。"
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
        },
    )
    R(
        "node.list",
        node_universal.node_list,
        tags=["node", "read"],
        output_policy=COLLECTION_OUTPUT_POLICY,
      description=(
          "列出有界节点索引页，支持 query/regex 过滤。"
      ),
      schema={
          "type": "object",
          "properties": {
              "project_id": {"type": "string"},
              "type": {"type": "string", "enum": ["text", "image", "video", "audio"]},
              "status": {"type": "string"},
              "surface": {"type": "string", "enum": ["project_panel", "draft_canvas"]},
                "query": {
                    "type": "string",
                    "description": "模糊查询标题、prompt、状态、错误、input/output 等文本",
                },
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
              "offset": {
                  "type": "integer",
                  "minimum": 0,
                  "description": "索引页起始偏移，默认 0；继续读取时传上页 next_offset。",
              },
              "limit": {
                  "type": "integer",
                  "minimum": 1,
                  "maximum": 100,
                  "description": "单页条目数，默认 20，最大 100。",
              },
          },
        },
    )
    R(
        "vision.view_image",
        vision_tools.view_image,
        tags=["vision", "read"],
        output_policy=MULTIMODAL_OUTPUT_POLICY,
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
                "source": {
                    "type": "string",
                    "description": "项目存储内图片路径、当前项目 /api/media URL 或远程图片 URL",
                },
              "sources": {
                  "type": "array",
                  "items": {"type": "string"},
                  "description": "多个项目存储图片路径、当前项目 /api/media URL 或远程图片 URL",
              },
              "detail": {"type": "string", "enum": ["high"], "default": "high"},
                "max_images": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 32,
                    "description": "本次最多附加图片数，默认 8",
                },
          },
        },
    )
    R(
        "node.run",
        node_universal.node_run,
        tags=["node", "execute"],
      description=(
        "执行已有 text/image/video/audio 节点并保存产物。需要节点已具备可运行输入；"
        "普通 text 节点保存已有 fields.content；带 fields.generation 的 text 节点从已捕获用户消息"
        "受控续写并原子生成正文，成功结果已完成验证，无需再 node.get；"
        "带 workflow prompt_ref/prompt_spec 的 text 节点可在本工具内生成 fields.content；"
        "节点运行前先按当前 skill 和用户要求检查内容/prompt、fields 和依赖；"
        "不符合时先 node.update 修原节点，不要只改无关字段后重跑；"
        "复杂或高风险创作节点可用 agent.review 辅助检查内容、字段和依赖；"
        "action='force' 用于重跑，extra_fields 只对本次运行生效。"
        ),
    )
    # project.*
    R(
        "project.get_state",
        project_tools.project_get_state,
        tags=["project", "read"],
        description=(
            "读取项目运行状态和画布聚合计数。仅在当前回答或操作依赖项目现状时使用；"
            "节点索引与详情分别用 node.list/node.get。"
        ),
    )

    R("drama.parse_uploaded_script", drama_tools.parse_uploaded_script, tags=["drama", "ingest"])
    R(
        "project.reset",
        drama_tools.reset_project,
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
        },
    )
    R(
        "canvas.delete",
        canvas_tools.delete_canvas,
        tags=["canvas", "destructive"],
      description=(
        "删除指定画布节点或清空画布,并清理这些节点的本地生成产物。"
        "首次调用只创建结构化确认并结束当前轮，确认后后端按原参数执行。"
        "scope='selected' 时传 node_ids；scope='all' 时清空当前项目画布。"
        "它不清 project state、任务或标题；用户说重置项目才用 project.reset。"
        ),
      schema={
          "type": "object",
          "properties": {
              "project_id": {"type": "string"},
              "scope": {
                  "type": "string",
                  "enum": ["selected", "all"],
                  "default": "selected",
                  "description": "selected 删除 node_ids；all 清空当前画布。",
              },
              "node_ids": {
                  "type": "array",
                  "items": {"type": "string"},
                  "maxItems": 100,
                  "description": "scope=selected 时要删除的节点编号。",
              },
          },
      },
    )

    R(
        "media.cancel_image_generation",
        media_tools.cancel_image_generation,
      tags=["media", "control"],
      description=(
        "停止当前项目正在进行的图片生成或后续图片生成步骤。"
        "当用户说停止、取消、中止图片生成时调用。"
        ),
    )
    R(
        "media.get_presets",
        media_tools.get_presets,
        tags=["media", "read"],
        output_policy=COLLECTION_OUTPUT_POLICY,
    )

    # file.*
    R(
        "file.list_dir",
        file_tools.list_dir,
        tags=["file", "read"],
        output_policy=COLLECTION_OUTPUT_POLICY,
    )
    R(
        "file.read_text",
        file_tools.read_text,
        tags=["file", "read"],
        output_policy=DOCUMENT_OUTPUT_POLICY,
      description=(
          "读取用户上传文件或用户本轮明确给出的项目存储相对路径。"
          "rel_path 只接受上传结果或用户明确路径；正文在 content_page 中按字符偏移分页。"
          "Skill 正文通过 skills.list/read 获取；节点、trace 和 tool result 状态查询使用对应工具。"
      ),
      usage_hints=[
        "file.read_text(project_id=project_id, rel_path='uploads/script.txt', offset=0, limit=8000)",
        ],
    )
    R(
        "file.extract_text_from_upload",
        file_tools.extract_text_from_upload,
        tags=["file", "read"],
        output_policy=DOCUMENT_OUTPUT_POLICY,
        description="从 txt、md、docx 上传文件抽取文本；正文在 content_page 中按字符偏移分页。",
    )
    R(
        "file.workspace_list",
        file_tools.workspace_list,
        tags=["file", "read"],
        output_policy=COLLECTION_OUTPUT_POLICY,
      schema={
          "type": "object",
          "properties": {
                "path": {
                    "type": "string",
                    "description": "workspace 相对路径；空字符串表示项目根目录",
                },
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
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "页起始偏移；继续读取时传上页 next_offset",
                },
                "max_entries": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "单页条目数，默认 50，上限 100",
                },
          },
      },
      description="列出当前 workspace 内的文件和目录，支持 query/regex 过滤，不执行 shell 命令。",
        usage_hints=[
            "tool.execute(name='file.workspace_list', input={'path': 'apps/api', 'recursive': False})"
        ],
    )
    R(
        "file.workspace_search",
        file_tools.workspace_search,
        tags=["file", "read"],
        output_policy=COLLECTION_OUTPUT_POLICY,
      schema={
          "type": "object",
          "properties": {
                "query": {
                    "type": "string",
                    "description": "要搜索的文件名或文本内容；空字符串只按 glob 返回文件",
                },
                "path": {
                    "type": "string",
                    "description": "workspace 相对起点；空字符串表示项目根目录",
                },
                "glob": {
                    "type": "string",
                    "description": "文件路径 glob，例如 '*.py' 或 'apps/api/**/*.py'",
                },
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
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "结果页起始偏移；继续搜索时传上页 next_offset",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "单页匹配数，默认 50，上限 100",
                },
                "max_file_bytes": {
                    "type": "integer",
                    "description": "单文件内容搜索字节上限，默认 200000",
                },
          },
      },
      description="在当前 workspace 内按文件名或文本内容搜索，支持 query/regex，不执行 shell 命令。",
        usage_hints=[
            "tool.execute(name='file.workspace_search', input={'query': 'AgentOrchestrator', 'glob': '*.py'})"
        ],
    )
    R(
        "file.workspace_read",
        file_tools.workspace_read,
        tags=["file", "read"],
        output_policy=DOCUMENT_OUTPUT_POLICY,
      schema={
          "type": "object",
          "properties": {
              "path": {"type": "string", "description": "workspace 相对文件路径"},
              "mode": {"type": "string", "enum": ["text"], "description": "仅支持 text；二进制使用专用媒体读取工具"},
                "max_bytes": {
                    "type": "integer",
                    "description": "源文件物理安全上限，默认及最大 10485760 字节",
                },
              "offset": {"type": "integer", "minimum": 0, "description": "字符起始偏移，0-based"},
              "limit": {"type": "integer", "minimum": 0, "maximum": 8000, "description": "本页字符数，默认及最大 8000；0 只返回元数据"},
          },
          "required": ["path"],
      },
      description="读取当前 workspace 文本文件的一个有界字符页，不执行 shell 命令。",
        usage_hints=[
            "tool.execute(name='file.workspace_read', input={'path': 'README.md', 'offset': 0, 'limit': 8000})"
        ],
    )
    R(
        "file.workspace_write",
        file_tools.workspace_write,
        tags=["file", "write"],
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
        usage_hints=[
            "tool.execute(name='file.workspace_write', input={'path': 'tmp/notes.txt', 'content': 'hello\\n'})"
        ],
    )
    R(
        "file.workspace_patch",
        file_tools.workspace_patch,
        tags=["file", "write"],
      schema={
          "type": "object",
          "properties": {
              "path": {"type": "string", "description": "workspace 相对文本文件路径"},
              "old_text": {"type": "string", "description": "要精确匹配替换的旧文本"},
              "new_text": {"type": "string", "description": "替换后的新文本"},
                "occurrence": {
                    "type": "integer",
                    "description": "替换第几处，1-based；0 表示替换全部",
                },
          },
          "required": ["path", "old_text", "new_text"],
      },
      description="按精确文本替换修改当前 workspace 内的文本文件，不执行 shell 命令；拒绝修改 .git。",
        usage_hints=[
            "tool.execute(name='file.workspace_patch', input={'path': 'tmp/notes.txt', 'old_text': 'old', 'new_text': 'new'})"
        ],
    )
    R(
        "file.workspace_delete",
        file_tools.workspace_delete,
        tags=["file", "destructive"],
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
        usage_hints=["tool.execute(name='file.workspace_delete', input={'path': 'tmp/notes.txt'})"],
    )

    # memory.*
    R("memory.save_fact", memory_tools.memory_save_fact, tags=["memory"])
    R(
        "memory.recall",
        memory_tools.memory_recall,
        tags=["memory", "read"],
        output_policy=COLLECTION_OUTPUT_POLICY,
    )
    R(
        "memory.compact_context",
        memory_tools.memory_compact_context,
        tags=["memory"],
      description=(
          "把当前模型可见历史压缩为 Codex 风格上下文检查点并保存 transcript；聊天界面记录保持可见。"
          "原生 OpenAI Responses 使用 /responses/compact，其余模型使用同语义本地降级。"
        ),
    )
    R("memory.save_user_fact", memory_tools.memory_save_user_fact, tags=["memory", "user"])
    R(
        "memory.recall_user",
        memory_tools.memory_recall_user,
        tags=["memory", "user", "read"],
        output_policy=COLLECTION_OUTPUT_POLICY,
    )

    # config.* — 统一配置总览（LLM / 图片 / 视频 / API Keys）
    # config.* — runtime.jsonc 文件即真相源；唯一对外写入口
    R(
        "config.read",
        config_tools.config_read_for_agent,
        tags=["config", "read"],
        output_policy=COLLECTION_OUTPUT_POLICY,
        description="读 runtime 配置（结构化），默认 mask api_key",
    )
    R(
        "config.read_file",
        config_tools.config_read_file_for_agent,
        tags=["config", "read"],
        output_policy=DOCUMENT_OUTPUT_POLICY,
        description="读取 runtime.jsonc 的一个有界原始文本页；结构化配置用 config.read。",
    )
    R(
        "config.validate",
        config_tools.config_validate,
        tags=["config", "read"],
        description="干跑校验给定配置内容，不写入",
    )
    # feature.* — unified feature flags and kill switches
    R(
        "feature.list",
        feature_tools.feature_list,
        tags=["feature", "read"],
        output_policy=COLLECTION_OUTPUT_POLICY,
        description="列出统一 feature flag 和 kill switch 状态。",
    )
    R(
        "feature.is_enabled",
        feature_tools.feature_is_enabled,
        tags=["feature", "read"],
        description="查询某个 feature flag 当前是否启用，以及是否被 kill switch 强制关闭。",
    )

    # agent.* — meta + 四种协作模式
    R(
        "agent.run",
        agent_tools.agent_run,
        tags=["agent", "write"],
        output_policy=DELEGATED_OUTPUT_POLICY,
      description="委派给已注册的专职子 Agent；适合选择已有 workflow 模板、媒体节点生产和隔离图片编辑。",
      search_hint=(
          "subagent specialist delegate agent run workflow_spec workflow template selector node_producer image_editor node produce prompt fields run image video audio generate character reference edit crop brush fill annotate text arrow "
          "segment cutout background removal transparent png mask icon "
          "工作流 模板选择 现有模板 节点生产 节点补全 提示词编写 运行节点 图片生成 视频提示词 人物图 参考图 图片编辑 子agent 子 Agent 委派 专职 worker 裁剪 涂鸦 画笔 覆盖 填充 标注 文字 箭头 抠图 分割 透明背景 图标"
      ),
      usage_hints=[
          "Workflow 请求交给 workflow_spec 选择器；主 Agent 先自行读完匹配的 Skill，再传模板线索和已知输入。普通视频默认返回 general_short_drama_workflow 的 template_id。",
          "主 Agent 拿到 template_id/artifact_ref 和 input_fields 后，根据用户原话和历史状态判断是否提问；需要复查模板或 spec 时再读取 workflow.template.read 或 workflow.spec.read。",
          "tool.execute(name='agent.run', input={'agent':'workflow_spec','task':'为用户的视频请求选择可运行工作流模板','inputs':{'facts':{'plot':'江湖相逢'},'current_workflow':{}}})",
          "tool.execute(name='agent.run', input={'agent':'node_producer','task':'补全并运行节点12的人物参考图；按选定人物 prompt skill 写入并生成，完成后看图自检。','inputs':{'node_id':'12','allowed_node_types':['image'],'basis':{'kind':'skill_plan'},'primary_skill':{'name':'character-prompt','category':'prompt','scope':'builtin'},'acceptance_criteria':['主体清晰','参考一致']},'max_steps':12})",
          "tool.execute(name='agent.run', input={'agent':'image_editor','task':'修复节点12的软件图标边角和外框；成品要主体完整、安全边距稳定、透明背景干净，提交前验证最终候选。','inputs':{'node_id':'12'},'max_steps':24})",
          "抠图、透明背景、图标圆角和复杂边缘清理交给 image_editor；它可以在隔离上下文中调用 image.segment 和 image.edit。",
          "node_producer 通常使用默认步数或 10-12；简单局部编辑通常 max_steps=12-16，复杂透明背景、抠图、图标修边或多轮预览可用 20-30。",
          "image_editor 自己看图、preview、验证并 commit；主 Agent 根据返回的 verification 判断是否继续。",
      ],
      schema={
          "type": "object",
          "properties": {
                "agent": {
                    "type": "string",
                    "description": "专职子 Agent 名称；当前支持 workflow_spec、node_producer、image_editor。workflow_spec 只选择已有模板；省略或传 catalog 返回可用列表。",
                },
                "task": {
                    "type": "string",
                    "description": "交给子 Agent 的自然语言任务；写清目标成品、保留内容、验收标准和失败停止条件。",
                },
                "inputs": {
                    "type": "object",
                    "description": "少量上下文，如 workflow_skill_name、artifact_ref、facts、node_id/node_ids、allowed_node_types、basis、primary_skill、inline_spec、source_ref、candidate_ref、notes。",
                },
                "max_steps": {
                    "type": "integer",
                    "description": "子 Agent 最大步骤数；通常不传。node_producer 默认 12，image_editor 默认 20；复杂编辑可提高。",
                },
            },
        },
    )
    R(
        "agent.map_reduce",
        agent_tools.agent_map_reduce,
        tags=["agent", "mode"],
        output_policy=DELEGATED_OUTPUT_POLICY,
        description="Map-Reduce 模式:并行扇出 N 个独立子任务,可选 LLM 聚合摘要(三模型对比、候选图、独立配角)。",
    )
    R(
        "agent.pipeline",
        agent_tools.agent_pipeline,
        tags=["agent", "mode"],
        output_policy=DELEGATED_OUTPUT_POLICY,
        description="Pipeline 模式:顺序管道,前一阶段产出按 carry_keys 注入下一阶段(场景→分镜→视频提示词)。",
    )
    R(
        "agent.hierarchical",
        agent_tools.agent_hierarchical,
        tags=["agent", "mode"],
        output_policy=DELEGATED_OUTPUT_POLICY,
        description="Hierarchical 模式:每个 split 内部可继续走 map_reduce/pipeline(多集并行,每集再分发段任务)。",
    )
    R(
        "agent.review",
        agent_tools.agent_review,
        tags=["agent", "review", "read"],
        output_policy=DELEGATED_OUTPUT_POLICY,
        schema={
          "type": "object",
          "additionalProperties": False,
          "properties": {
            "review_goal": {"type": "string"},
            "user_request": {"type": "string"},
            "work_summary": {"type": "string"},
            "evidence": {"type": "object", "additionalProperties": True},
            "custom_checklist": {"type": "array", "items": {"type": "string"}},
            "review_skill": {
              "oneOf": [
                {"type": "string"},
                {"type": "object", "additionalProperties": True},
              ]
            },
            "max_steps": {"type": "integer"},
          },
        },
      description=(
          "隔离运行通用只读审查子 Agent，用真实项目状态、任务、节点、文件和主 Agent 传入的 Skill 规则审查指定目标。"
          "复杂视频节点批次或任务需要第二视角时传 review_goal、user_request、work_summary 和 evidence。"
          "媒体运行前可用它批量检查 prompt 是否符合 skill、字段是否可执行、依赖是否使用节点编号。"
          "自定义检查 Skill 从自动目录匹配；主 Agent 用 skills.list/read 读完后，把适用规则放进 custom_checklist 或 review_skill。"
          "返回 pass/revise_required/blocked 等结果；主 Agent 只修有 evidence 或 violated_requirement 的具体问题。"
        ),
    )
    # panel.* — project-level panel view (mode/axis switching)
    # media.* — provider configuration (image active; video stub)
    R(
        "media.list_providers",
        media_provider_tools.media_list_providers,
        tags=["media", "provider", "read"],
        output_policy=COLLECTION_OUTPUT_POLICY,
    )
    R(
        "media.test_provider",
        media_provider_tools.media_test_provider,
        tags=["media", "provider", "meta"],
    )

    # assets.* — user-designated local asset library
    R(
        "assets.get_library_path",
        asset_library_tools.assets_get_library_path,
        tags=["assets", "read"],
    )
    R(
        "assets.save_to_project",
        asset_library_tools.assets_save_to_project,
        tags=["assets", "write"],
        description="把节点、资产记录或本地文件显式保存到单一本地资产库。",
        usage_hints=[
            "tool.execute(name='assets.save_to_project', input={'episode': 1, 'kind': 'scene', 'source': 'node:12', 'name': '场景名'})",
        ],
    )
    R(
        "assets.save_to_shared",
        asset_library_tools.assets_save_to_shared,
        tags=["assets", "write"],
        description="把素材显式保存到单一本地资产库；kind/category 根据内容和用户用语判断，分类名跟随用户语言。",
        usage_hints=[
            "kind 只使用 character、scene、storyboard，对应人物、场景、分镜；再选择合适 category。",
            "tool.execute(name='assets.save_to_shared', input={'kind': 'character', 'category': '主要角色', 'source': 'node:12', 'name': '角色名'})",
        ],
    )
    R("assets.list_project", asset_library_tools.assets_list_project, tags=["assets", "read"], output_policy=COLLECTION_OUTPUT_POLICY)
    R("assets.list_shared", asset_library_tools.assets_list_shared, tags=["assets", "read"], output_policy=COLLECTION_OUTPUT_POLICY)
    R("assets.read_asset", asset_library_tools.assets_read_asset, tags=["assets", "read"], output_policy=DOCUMENT_OUTPUT_POLICY)
    R("assets.list_categories", asset_library_tools.assets_list_categories, tags=["assets", "read"], output_policy=COLLECTION_OUTPUT_POLICY)
    R(
        "assets.create_category",
        asset_library_tools.assets_create_category,
        tags=["assets", "write"],
        description="在单一本地资产库中创建 kind/category 文件夹。",
        usage_hints=[
            "category 使用用户消息语言的自然文件夹名。",
            "tool.execute(name='assets.create_category', input={'kind': 'character', 'category': '主要角色'})",
        ],
    )
    R(
        "assets.move_asset",
        asset_library_tools.assets_move_asset,
        tags=["assets", "write"],
        description="把资产库文件移动到另一个 kind/category 文件夹。",
        usage_hints=[
            "category 使用用户消息语言的自然文件夹名。",
            "tool.execute(name='assets.move_asset', input={'path': '/assets/人物/a.png', 'kind': 'character', 'category': '主要角色'})",
        ],
    )
    R(
        "assets.add_to_canvas",
        asset_library_tools.assets_add_to_canvas,
        tags=["assets", "write"],
        description="把生成资产或资产库文件加入画布为可预览节点。",
        usage_hints=[
            "tool.execute(name='assets.add_to_canvas', input={'source': 'asset:asset-id', 'title': '主角参考图'})",
            "tool.execute(name='assets.add_to_canvas', input={'source': '/assets/人物/a.png', 'node_type': 'image'})",
        ],
    )

    return target_registry


_register_builtins()
_apply_standard_tool_descriptions()
