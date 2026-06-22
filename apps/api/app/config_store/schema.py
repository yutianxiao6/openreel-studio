"""Runtime 配置 Schema (Pydantic)。

文件 → parse → 这层校验 → 通过后才写入 DB。校验失败保留旧值。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ── 子模型 ────────────────────────────────────────────────────────────────


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
    is_default: bool = Field(False, description="兜底 provider；全局只能有 1 条 True")
    enabled: bool = True
    notes: Optional[str] = None
    params: dict = Field(default_factory=dict, description="其他模型私有元数据，原样保存在配置中")

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
    """单个图片/视频 provider，对应 media_providers 表一行。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(..., description="image | video")
    name: str = Field(..., min_length=1, max_length=64)
    base_url: str = Field(..., min_length=1)
    api_key: Optional[str] = None
    model_name: str = Field(..., min_length=1)
    api_format: str = Field("openai", description="openai | raw | raw_post | volcengine_ark | xai_video | grok_1_5")
    is_active: bool = False
    enabled: bool = True
    notes: Optional[str] = None
    params: dict = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def _valid_kind(cls, v: str) -> str:
        if v not in ("image", "video"):
            raise ValueError(f"kind must be 'image' or 'video', got {v!r}")
        return v

    @field_validator("api_format")
    @classmethod
    def _valid_api_format(cls, v: str) -> str:
        if v not in ("openai", "raw", "raw_post", "volcengine_ark", "xai_video", "grok_1_5"):
            raise ValueError(
                "api_format must be 'openai', 'raw', 'raw_post', 'volcengine_ark', 'xai_video', or 'grok_1_5', "
                f"got {v!r}"
            )
        return v


# ── 顶层模型 ──────────────────────────────────────────────────────────────


# 与 db.models.TASK_TYPES 保持一致；改动需同步那边
ALLOWED_TASK_TYPES = (
    "agent_loop",
    "planning",
    "character_generation",
    "outline_generation",
    "script_generation",
    "script_review",
    "storyboard_generation",
    "image_prompt_generation",
    "video_prompt_generation",
)
LEGACY_TASK_TYPE_ALIASES = {
    "intent_parse": "agent_loop",
}

# app_settings 已知键和默认值；启动 bootstrap 时若文件缺失会补全
DEFAULT_APP_SETTINGS: dict = {
    "agent.skip_confirmations": False,
    "agent.max_iterations": 200,
    "agent.auto_archive": True,
    "agent.blueprint_review_mode": "continuous_final_review",
    "agent.video_plan_confirmation_mode": "one_shot",
    "agent.vision_context_max_images": 8,
    "agent.vision_context_max_dimension": 1536,
    "ui.canvas_default_view": "canvas",
    "feature_flags": {},
    "kill_switches": {},
}


class RuntimeConfig(BaseModel):
    """整个 config/runtime.jsonc 的根结构。"""

    model_config = ConfigDict(extra="forbid")

    schema_version_: int = Field(1, alias="$schema_version")
    llm_providers: list[LlmProviderEntry] = Field(default_factory=list)
    media_providers: list[MediaProviderEntry] = Field(default_factory=list)
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

        # 2. is_default 至多 1 条
        defaults = [p.name for p in self.llm_providers if p.is_default]
        if len(defaults) > 1:
            raise ValueError(
                f"llm_providers: is_default=True 至多 1 条，当前 {len(defaults)} 条: {defaults}"
            )

        # 3. media_providers (kind, name) 联合唯一
        media_seen: set[tuple[str, str]] = set()
        for m in self.media_providers:
            key = (m.kind, m.name)
            if key in media_seen:
                raise ValueError(f"media_providers: 重复 (kind={m.kind!r}, name={m.name!r})")
            media_seen.add(key)

        # 4. 同 kind 内 is_active 至多 1 条
        for kind in ("image", "video"):
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

        # 5. model_assignments 引用必须存在
        provider_names = {p.name for p in self.llm_providers}
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
