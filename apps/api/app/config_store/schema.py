"""Runtime 配置 Schema (Pydantic)。

文件 → parse → 这层校验 → 通过后才写入 DB。校验失败保留旧值。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ── 子模型 ────────────────────────────────────────────────────────────────


def _project_root() -> Path:
    runtime_root = os.getenv("PROJECT_ROOT", "").strip()
    if runtime_root:
        return Path(runtime_root).expanduser().resolve()
    return Path(__file__).resolve().parents[4]


def _protocol_catalog_path(env_name: str, relative_path: str) -> Path:
    override = os.getenv(env_name, "").strip()
    if override:
        path = Path(override).expanduser()
        return path if path.is_absolute() else _project_root() / path
    return _project_root() / relative_path


def _protocols_from_catalog(env_name: str, relative_path: str) -> dict[str, dict[str, Any]]:
    path = _protocol_catalog_path(env_name, relative_path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    protocols = data.get("protocols") if isinstance(data, dict) else None
    if not isinstance(protocols, dict):
        return {}
    return {
        str(protocol_id): protocol
        for protocol_id, protocol in protocols.items()
        if str(protocol_id).strip() and isinstance(protocol, dict)
    }


def _protocol_ids_from_catalog(env_name: str, relative_path: str) -> set[str]:
    return set(_protocols_from_catalog(env_name, relative_path))


def _image_protocol_ids_from_catalog() -> set[str]:
    return _protocol_ids_from_catalog(
        "OPENREEL_IMAGE_PROTOCOLS_FILE",
        "config/image_provider_protocols/catalog.json",
    )


def _audio_protocol_ids_from_catalog() -> set[str]:
    return _protocol_ids_from_catalog(
        "OPENREEL_AUDIO_PROTOCOLS_FILE",
        "config/audio_provider_protocols/catalog.json",
    )


class LlmProviderEntry(BaseModel):
    """单个 LLM provider，对应 llm_providers 表一行。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=64,
                      description="唯一标识，model_assignments 用此名引用")
    provider: str = Field(..., min_length=1,
                          description="LiteLLM provider prefix (deepseek/openai/anthropic 等)")
    model_name: str = Field(..., min_length=1,
                            description="LiteLLM model id (deepseek-chat/gpt-4o 等)")
    base_url: Optional[str] = Field(None,
                                    description="None=用 provider 默认 endpoint；填则走中转站")
    api_key: Optional[str] = Field(None, description="API Key；某些 provider 可空")
    context_window_tokens: Optional[int] = Field(
        None,
        ge=1,
        description="模型完整上下文窗口 tokens；用于上下文剩余/使用率和压缩监控",
    )
    max_input_tokens: Optional[int] = Field(
        None,
        ge=1,
        description="可用输入 token 上限；服务商若保留输出空间，可小于 context_window_tokens",
    )
    max_output_tokens: Optional[int] = Field(
        None,
        ge=1,
        description="默认输出 token 上限；未指定 task max_tokens 时使用",
    )
    supports_prompt_cache: Optional[bool] = Field(
        None,
        description="模型/provider 是否支持 prompt cache 统计或计费",
    )
    supports_vision: Optional[bool] = Field(
        None,
        description="聊天接口是否支持 image_url/视觉输入",
    )
    tokenizer: Optional[str] = Field(
        None,
        max_length=64,
        description="token 估算器标记，例如 o200k_base/cl100k_base/provider",
    )
    tier: str = Field(
        "balanced",
        description="模型策略档位: strong | balanced | small",
    )
    enabled: bool = True
    notes: Optional[str] = None
    params: dict = Field(default_factory=dict, description="其他模型私有元数据，原样保存在配置中")

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_global_default(cls, value: Any) -> Any:
        if isinstance(value, dict) and "is_default" in value:
            next_value = dict(value)
            next_value.pop("is_default", None)
            return next_value
        return value

    @field_validator("tier")
    @classmethod
    def _valid_tier(cls, v: str) -> str:
        if v not in ALLOWED_MODEL_TIERS:
            raise ValueError(f"tier must be one of {ALLOWED_MODEL_TIERS}, got {v!r}")
        return v

    @model_validator(mode="after")
    def _validate_token_limits(self) -> "LlmProviderEntry":
        if (
            self.context_window_tokens is not None
            and self.max_input_tokens is not None
            and self.max_input_tokens > self.context_window_tokens
        ):
            raise ValueError("max_input_tokens 不能大于 context_window_tokens")
        if (
            self.context_window_tokens is not None
            and self.max_output_tokens is not None
            and self.max_output_tokens > self.context_window_tokens
        ):
            raise ValueError("max_output_tokens 不能大于 context_window_tokens")
        return self


class MediaProviderEntry(BaseModel):
    """单个图片/视频/音频 provider，对应 media_providers 表一行。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(..., description="image | video | audio")
    name: str = Field(..., min_length=1, max_length=64)
    base_url: str = Field(..., min_length=1)
    api_key: Optional[str] = None
    model_name: str = Field(..., min_length=1)
    api_format: str = Field("openai", description="provider transport contract")
    is_active: bool = False
    enabled: bool = True
    notes: Optional[str] = None
    params: dict = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def _valid_kind(cls, v: str) -> str:
        if v not in ("image", "video", "audio"):
            raise ValueError(f"kind must be 'image', 'video', or 'audio', got {v!r}")
        return v

    @field_validator("api_format")
    @classmethod
    def _valid_api_format(cls, v: str) -> str:
        if v not in ("universal_adapter", "openai", "raw", "raw_post", "image_http_v1", "audio_http_v1", "suno_compatible", "openai_tts"):
            raise ValueError(
                "api_format must be 'universal_adapter' or a supported image/audio format, "
                f"got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _validate_video_adapter(self) -> "MediaProviderEntry":
        if self.kind == "video" and self.api_format != "universal_adapter":
            raise ValueError("video provider 只支持 api_format='universal_adapter'")
        return self

    @model_validator(mode="after")
    def _validate_universal_adapter_reference(self) -> "MediaProviderEntry":
        if self.api_format != "universal_adapter":
            return self
        params = self.params if isinstance(self.params, dict) else {}
        uma = params.get("uma")
        if not isinstance(uma, dict):
            raise ValueError("universal_adapter provider 必须设置 params.uma")
        if any(key in uma for key in ("protocol", "protocol_document", "operations")):
            raise ValueError("params.uma 只引用协议 ID，不能内嵌协议内容")
        protocol_id = str(uma.get("protocol_id") or "").strip()
        if not protocol_id:
            raise ValueError("universal_adapter provider 必须设置 params.uma.protocol_id")
        if self.kind == "video" and not str(uma.get("target_profile_id") or "").strip():
            raise ValueError(
                "video universal_adapter provider 必须设置 params.uma.target_profile_id"
            )
        operation = str(uma.get("operation") or f"{self.kind}.{'speech' if self.kind == 'audio' else 'generate'}").strip()
        if not operation.startswith(f"{self.kind}."):
            raise ValueError(
                f"params.uma.operation={operation!r} 必须属于 {self.kind!r} 模态"
            )
        return self

    @model_validator(mode="after")
    def _validate_image_protocol_reference(self) -> "MediaProviderEntry":
        if self.kind != "image" or self.api_format != "image_http_v1":
            return self
        params = self.params if isinstance(self.params, dict) else {}
        if "image_protocol" in params or isinstance(params.get("protocol"), dict):
            raise ValueError(
                "image_http_v1 provider 只保存 params.image_protocol_id；协议 JSON 必须写在 config/image_provider_protocols/catalog.json"
            )
        protocol_id = str(params.get("image_protocol_id") or "").strip()
        if not protocol_id:
            raise ValueError("image_http_v1 provider 必须设置 params.image_protocol_id")
        catalog_ids = _image_protocol_ids_from_catalog()
        if not catalog_ids:
            raise ValueError("image_http_v1 protocol catalog 缺失或没有可用协议")
        if protocol_id not in catalog_ids:
            raise ValueError(
                f"params.image_protocol_id={protocol_id!r} 不在 config/image_provider_protocols/catalog.json 的 protocols 中"
            )
        return self

    @model_validator(mode="after")
    def _validate_audio_protocol_reference(self) -> "MediaProviderEntry":
        if self.kind != "audio" or self.api_format != "audio_http_v1":
            return self
        params = self.params if isinstance(self.params, dict) else {}
        if "audio_protocol" in params or isinstance(params.get("protocol"), dict):
            raise ValueError(
                "audio_http_v1 provider 只保存 params.audio_protocol_id；协议 JSON 必须写在 config/audio_provider_protocols/catalog.json"
            )
        protocol_id = str(params.get("audio_protocol_id") or "").strip()
        if not protocol_id:
            raise ValueError("audio_http_v1 provider 必须设置 params.audio_protocol_id")
        catalog_ids = _audio_protocol_ids_from_catalog()
        if not catalog_ids:
            raise ValueError("audio_http_v1 protocol catalog 缺失或没有可用协议")
        if protocol_id not in catalog_ids:
            raise ValueError(
                f"params.audio_protocol_id={protocol_id!r} 不在 config/audio_provider_protocols/catalog.json 的 protocols 中"
            )
        return self


# ── 顶层模型 ──────────────────────────────────────────────────────────────


ALLOWED_MODEL_TIERS = ("strong", "balanced", "small")

# 与 db.models.TASK_TYPES 保持一致；改动需同步那边
ALLOWED_TASK_TYPES = (
    "agent_loop",
    "agent_review",
    "agent_compact",
    "agent_aux",
    "planning",
    "character_generation",
    "outline_generation",
    "script_generation",
    "script_review",
    "storyboard_generation",
    "image_understanding",
    "image_prompt_generation",
    "video_prompt_generation",
    "subagent_node_producer",
    "subagent_image_editor",
)
DEFAULT_MODEL_TASK_TIERS: dict[str, str] = {
    "agent_loop": "strong",
    "agent_review": "small",
    "agent_compact": "balanced",
    "agent_aux": "small",
    "planning": "balanced",
    "character_generation": "balanced",
    "outline_generation": "balanced",
    "script_generation": "strong",
    "script_review": "small",
    "storyboard_generation": "balanced",
    "image_understanding": "balanced",
    "image_prompt_generation": "balanced",
    "video_prompt_generation": "strong",
    "subagent_node_producer": "balanced",
    "subagent_image_editor": "balanced",
}
LEGACY_TASK_TYPE_ALIASES = {
    "intent_parse": "agent_loop",
    "subagent_image_generator": "subagent_node_producer",
}

# app_settings 已知键和默认值；启动 bootstrap 时若文件缺失会补全
DEFAULT_APP_SETTINGS: dict = {
    "agent.max_iterations": 200,
    "agent.auto_archive": True,
    "agent.vision_context_max_images": 8,
    "agent.vision_context_max_dimension": 2048,
    "feature_flags": {},
    "kill_switches": {},
}


class RuntimeConfig(BaseModel):
    """整个 config/runtime.jsonc 的根结构。"""

    model_config = ConfigDict(extra="forbid")

    schema_version_: int = Field(1, alias="$schema_version")
    llm_providers: list[LlmProviderEntry] = Field(default_factory=list)
    media_providers: list[MediaProviderEntry] = Field(default_factory=list)
    model_tier_defaults: dict[str, Optional[str]] = Field(default_factory=dict)
    model_assignments: dict[str, Optional[str]] = Field(default_factory=dict)
    app_settings: dict = Field(default_factory=dict)

    # ── 跨字段约束 ─────────────────────────────────────────

    @model_validator(mode="after")
    def _validate_consistency(self) -> "RuntimeConfig":
        # 1. llm_providers.name 唯一
        seen: set[str] = set()
        for p in self.llm_providers:
            if p.name in seen:
                raise ValueError(f"llm_providers: 重复 name {p.name!r}")
            seen.add(p.name)

        # 2. media_providers (kind, name) 联合唯一
        media_seen: set[tuple[str, str]] = set()
        for m in self.media_providers:
            key = (m.kind, m.name)
            if key in media_seen:
                raise ValueError(f"media_providers: 重复 (kind={m.kind!r}, name={m.name!r})")
            media_seen.add(key)

        # 3. 同 kind 内 is_active 至多 1 条
        for kind in ("image", "video", "audio"):
            actives = [m.name for m in self.media_providers if m.kind == kind and m.is_active]
            if len(actives) > 1:
                raise ValueError(
                    f"media_providers[{kind}]: is_active 至多 1 条，当前 {len(actives)} 条"
                )

        normalized_assignments: dict[str, Optional[str]] = {}
        for task, name in self.model_assignments.items():
            normalized_task = LEGACY_TASK_TYPE_ALIASES.get(task, task)
            if normalized_task in normalized_assignments and normalized_assignments[normalized_task] is not None:
                continue
            normalized_assignments[normalized_task] = name
        self.model_assignments = normalized_assignments

        normalized_tier_defaults: dict[str, Optional[str]] = {
            tier: self.model_tier_defaults.get(tier)
            for tier in ALLOWED_MODEL_TIERS
        }
        for tier in self.model_tier_defaults:
            if tier not in ALLOWED_MODEL_TIERS:
                raise ValueError(
                    f"model_tier_defaults: 未知 tier {tier!r}（可选: {ALLOWED_MODEL_TIERS}）"
                )
        self.model_tier_defaults = normalized_tier_defaults

        # 5. model_tier_defaults / model_assignments 引用必须存在
        provider_names = {p.name for p in self.llm_providers}
        for tier, name in self.model_tier_defaults.items():
            if name is not None and name not in provider_names:
                raise ValueError(
                    f"model_tier_defaults[{tier}]: 引用不存在的 provider {name!r}"
                )
        for task, name in self.model_assignments.items():
            if task not in ALLOWED_TASK_TYPES:
                raise ValueError(
                    f"model_assignments: 未知 task {task!r}（可选: {ALLOWED_TASK_TYPES}）"
                )
            if name is not None and name not in provider_names:
                raise ValueError(
                    f"model_assignments[{task}]: 引用不存在的 provider {name!r}"
                )

        return self
