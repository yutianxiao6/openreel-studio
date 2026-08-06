"""
SQLModel database models for OpenReel Studio.
Covers all tables from the design document (section 11).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel

from app.llm_limits import DEFAULT_LLM_MAX_OUTPUT_TOKENS


def gen_uuid() -> str:
    return str(uuid.uuid4())


def now() -> datetime:
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------


class ProjectBase(SQLModel):
    title: str
    description: Optional[str] = None
    genre: Optional[str] = None
    format: Optional[str] = None
    episode_count: int = 1
    duration_per_episode: int = 90
    budget_level: str = "low"
    status: str = "draft"  # draft | active | archived
    state_json: Optional[str] = None  # full project state as JSON string


class Project(ProjectBase, table=True):
    __tablename__ = "projects"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


# ---------------------------------------------------------------------------
# episodes
# ---------------------------------------------------------------------------


class EpisodeBase(SQLModel):
    project_id: str = Field(foreign_key="projects.id", index=True)
    episode_number: int
    title: Optional[str] = None
    hook: Optional[str] = None
    summary: Optional[str] = None
    script: Optional[str] = None
    cliffhanger: Optional[str] = None
    score_json: Optional[str] = None  # JSON with rating breakdown
    status: str = "pending"  # pending | generating | done | failed


class Episode(EpisodeBase, table=True):
    __tablename__ = "episodes"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


# ---------------------------------------------------------------------------
# workflow_nodes
# ---------------------------------------------------------------------------


class WorkflowNodeBase(SQLModel):
    project_id: str = Field(foreign_key="projects.id", index=True)
    display_id: Optional[int] = Field(default=None, index=True)
    type: str
    title: str
    status: str = "idle"
    position_x: float = 0.0
    position_y: float = 0.0
    input_json: Optional[str] = None
    output_json: Optional[str] = None
    model_config_json: Optional[str] = None
    prompt: Optional[str] = None
    error_message: Optional[str] = None
    version: int = 1
    supersedes_id: Optional[str] = Field(default=None, foreign_key="workflow_nodes.id", index=True)


class WorkflowNode(WorkflowNodeBase, table=True):
    __tablename__ = "workflow_nodes"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


# ---------------------------------------------------------------------------
# frame-accurate video editor sequences
# ---------------------------------------------------------------------------


class VideoEditSequence(SQLModel, table=True):
    __tablename__ = "video_edit_sequences"

    node_id: str = Field(foreign_key="workflow_nodes.id", primary_key=True)
    project_id: str = Field(foreign_key="projects.id", index=True)
    spec_json: str
    revision: int = 1
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class VideoEditSequenceRevision(SQLModel, table=True):
    __tablename__ = "video_edit_sequence_revisions"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    node_id: str = Field(foreign_key="workflow_nodes.id", index=True)
    project_id: str = Field(foreign_key="projects.id", index=True)
    revision: int = Field(index=True)
    spec_json: str
    created_at: datetime = Field(default_factory=now)


class VideoSequenceRenderJob(SQLModel, table=True):
    __tablename__ = "video_sequence_render_jobs"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    project_id: str = Field(foreign_key="projects.id", index=True)
    source_node_id: str = Field(foreign_key="workflow_nodes.id", index=True)
    sequence_revision: int = Field(ge=1)
    title: str
    spec_json: str
    status: str = Field(default="queued", index=True)
    progress: int = 0
    phase: str = "等待渲染"
    cancel_requested: bool = False
    output_node_id: Optional[str] = Field(default=None, foreign_key="workflow_nodes.id")
    result_json: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    completed_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# workflow_edges
# ---------------------------------------------------------------------------


class WorkflowEdgeBase(SQLModel):
    project_id: str = Field(foreign_key="projects.id", index=True)
    source_node_id: str = Field(foreign_key="workflow_nodes.id")
    target_node_id: str = Field(foreign_key="workflow_nodes.id")
    label: Optional[str] = None


class WorkflowEdge(WorkflowEdgeBase, table=True):
    __tablename__ = "workflow_edges"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=now)


# ---------------------------------------------------------------------------
# assets
# ---------------------------------------------------------------------------


class AssetBase(SQLModel):
    project_id: str = Field(foreign_key="projects.id", index=True)
    node_id: Optional[str] = Field(default=None, foreign_key="workflow_nodes.id")
    type: str
    name: str
    path: Optional[str] = None
    url: Optional[str] = None
    mime_type: Optional[str] = None
    metadata_json: Optional[str] = None
    prompt: Optional[str] = None
    model_name: Optional[str] = None


class Asset(AssetBase, table=True):
    __tablename__ = "assets"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=now)


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------


class MessageBase(SQLModel):
    project_id: str = Field(foreign_key="projects.id", index=True)
    role: str  # user | assistant | developer | system | tool
    content: str
    metadata_json: Optional[str] = None
    # Internal typed Responses rollout. Excluded from public model_dump/API
    # projections so reasoning and tool protocol items stay model-private.
    model_context_json: Optional[str] = Field(default=None, exclude=True)
    archived: bool = False


class Message(MessageBase, table=True):
    __tablename__ = "messages"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=now)


# ---------------------------------------------------------------------------
# agent trace events
# ---------------------------------------------------------------------------


class AgentTraceEvent(SQLModel, table=True):
    __tablename__ = "agent_trace_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: str = Field(index=True)
    run_id: str = Field(index=True)
    ts: str = Field(index=True)
    event: str = Field(index=True)
    iteration: Optional[int] = Field(default=None, index=True)
    tool_name: Optional[str] = Field(default=None, index=True)
    transition_reason: Optional[str] = None
    duration_ms: Optional[int] = None
    error_kind: Optional[str] = Field(default=None, index=True)
    payload_json: str
    created_at: datetime = Field(default_factory=now)


# ---------------------------------------------------------------------------
# model_configs
# ---------------------------------------------------------------------------


class ModelConfigBase(SQLModel):
    task_type: str = Field(index=True)
    provider: str
    model_name: str
    llm_provider_name: Optional[str] = Field(
        None, index=True, description="引用 llm_providers.name；ConfigStore 同步时设置"
    )
    temperature: float = 0.7
    max_tokens: int = DEFAULT_LLM_MAX_OUTPUT_TOKENS
    top_p: float = 1.0
    fallback_model: Optional[str] = None
    enabled: bool = True
    extra_json: Optional[str] = None


class ModelConfig(ModelConfigBase, table=True):
    __tablename__ = "model_configs"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


# ---------------------------------------------------------------------------
# media_providers — user-configured image/video/audio model endpoints
# Multiple per kind; exactly one (or zero) is_active per kind.
# ---------------------------------------------------------------------------


class MediaProviderBase(SQLModel):
    kind: str = Field(index=True)  # image | video | audio
    name: str = Field(index=True)  # user-supplied label, e.g. "fal-flux-pro"
    base_url: str
    api_key: Optional[str] = None
    model_name: str  # model id sent in payload
    api_format: str = "universal_adapter"
    params_json: Optional[str] = None  # default extra params JSON (size, steps, etc.)
    is_active: bool = False
    enabled: bool = True
    notes: Optional[str] = None


class MediaProvider(MediaProviderBase, table=True):
    __tablename__ = "media_providers"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


# ---------------------------------------------------------------------------
# llm_providers — DB mirror of config/runtime.jsonc llm_providers
# 写入只能由 ConfigStore.load() 触发；不要直接 INSERT/UPDATE/DELETE
# ---------------------------------------------------------------------------


class LlmProviderBase(SQLModel):
    name: str = Field(index=True)
    provider: str
    model_name: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    context_window_tokens: Optional[int] = None
    max_input_tokens: Optional[int] = None
    max_output_tokens: Optional[int] = None
    supports_prompt_cache: Optional[bool] = True
    supports_vision: Optional[bool] = None
    tokenizer: Optional[str] = None
    tier: str = "balanced"
    params_json: Optional[str] = None
    is_default: bool = False
    enabled: bool = True
    notes: Optional[str] = None


class LlmProvider(LlmProviderBase, table=True):
    __tablename__ = "llm_providers"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


# ---------------------------------------------------------------------------
# app_settings — KV store, mirror of config/runtime.jsonc app_settings
# ---------------------------------------------------------------------------


class AppSetting(SQLModel, table=True):
    __tablename__ = "app_settings"

    key: str = Field(primary_key=True)
    value_json: str
    description: Optional[str] = None
    category: str = "general"
    updated_at: datetime = Field(default_factory=now)


# ---------------------------------------------------------------------------
# user_memory — cross-project, long-lived facts about the user (preferences,
# voice/style, recurring naming conventions, model choices)
# ---------------------------------------------------------------------------


class UserMemoryBase(SQLModel):
    kind: str = Field(index=True)  # preference | style | naming | model | fact
    content: str
    source_project_id: Optional[str] = Field(default=None, index=True)
    hits: int = 0


class UserMemory(UserMemoryBase, table=True):
    __tablename__ = "user_memory"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=now)
    last_used_at: Optional[datetime] = None
