"""
SQLModel database models for OpenReel Studio.
Covers all tables from the design document (section 11).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


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


class ProjectCreate(ProjectBase):
    pass


class ProjectRead(ProjectBase):
    id: str
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# characters
# ---------------------------------------------------------------------------

class CharacterBase(SQLModel):
    project_id: str = Field(foreign_key="projects.id", index=True)
    name: str
    role_type: Optional[str] = None          # female_lead | male_lead | antagonist | support
    age: Optional[int] = None
    identity: Optional[str] = None
    personality: Optional[str] = None
    appearance: Optional[str] = None
    motivation: Optional[str] = None
    relationship_json: Optional[str] = None  # JSON
    visual_prompt: Optional[str] = None
    locked: bool = False


class Character(CharacterBase, table=True):
    __tablename__ = "characters"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class CharacterCreate(CharacterBase):
    pass


class CharacterRead(CharacterBase):
    id: str
    created_at: datetime
    updated_at: datetime


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
    score_json: Optional[str] = None   # JSON with rating breakdown
    status: str = "pending"            # pending | generating | done | failed


class Episode(EpisodeBase, table=True):
    __tablename__ = "episodes"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class EpisodeCreate(EpisodeBase):
    pass


class EpisodeRead(EpisodeBase):
    id: str
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# scenes
# ---------------------------------------------------------------------------

class SceneBase(SQLModel):
    project_id: str = Field(foreign_key="projects.id", index=True)
    episode_id: Optional[str] = Field(default=None, foreign_key="episodes.id", index=True)
    name: Optional[str] = None
    location: Optional[str] = None
    time_of_day: Optional[str] = None
    characters_json: Optional[str] = None   # JSON list of character ids
    props_json: Optional[str] = None        # JSON list of props
    summary: Optional[str] = None


class Scene(SceneBase, table=True):
    __tablename__ = "scenes"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class SceneCreate(SceneBase):
    pass


class SceneRead(SceneBase):
    id: str
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# shots
# ---------------------------------------------------------------------------

class ShotBase(SQLModel):
    project_id: str = Field(foreign_key="projects.id", index=True)
    episode_id: Optional[str] = Field(default=None, foreign_key="episodes.id", index=True)
    scene_id: Optional[str] = Field(default=None, foreign_key="scenes.id", index=True)
    shot_number: int
    shot_type: Optional[str] = None      # close_up | medium | wide | etc.
    camera: Optional[str] = None
    duration: Optional[int] = None       # seconds
    content: Optional[str] = None
    dialogue: Optional[str] = None
    image_prompt: Optional[str] = None
    video_prompt: Optional[str] = None
    asset_id: Optional[str] = None


class Shot(ShotBase, table=True):
    __tablename__ = "shots"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class ShotCreate(ShotBase):
    pass


class ShotRead(ShotBase):
    id: str
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# workflow_nodes
# ---------------------------------------------------------------------------

NODE_TYPES = [
    # ── 12 类收敛后的画布节点（2026-05-21）──
    "script_collection",          # 全剧剧本根（脑图 root）
    "episode_script",             # 单集剧本
    "episode_segment_plan",       # 该集切段方案
    "episode_cast_scene_plan",    # 该集出场人物+场景+段落分配
    "character",                  # 人物（融合 prompt + image）
    "scene",                      # 场景（融合 prompt + image）
    "segment_storyboard",         # 段落分镜（mode = grid | shot_list）
    "shot_first_frame",           # 镜头首帧（融合 prompt + image）
    "shot_last_frame",            # 镜头尾帧（融合 prompt + image）
    "segment_story_template",     # 段落故事模板（融合 prompt + 一张大图）
    "segment_video_prompt",       # 段落视频提示词（引用图清单 + prompt）
    "segment_video_clip",         # 段落视频片段
    # ── 旧 type 保留（DB 数据兼容，前端 nodeStyles 重定向到 12 类风格）──
    "project_setting",
    "outline",
    "character_image_prompt",
    "character_reference_image",
    "character_relationship",
    "episode_review",
    "segment",
    "scene_image",
    "scene_image_prompt",
    "panorama",
    "panorama_view",
    "shot_list",
    "storyboard_grid",
    "shot",
    "shot_image_prompt",
    "shot_reference_image",
    "shot_video_prompt",
    "shot_video_clip",
    "episode_export",
    "project_export",
    "character_generation",
    "outline_generation",
    "script_generation",
    "script_review",
    "storyboard_generation",
    "image_prompt_generation",
    "image_generation",
    "video_prompt_generation",
    "video_generation",
    "export",
]

CHARACTER_TIERS = ["main", "recurring", "guest"]

NODE_STATUSES = ["idle", "queued", "running", "completed", "failed", "waiting_confirm"]


class WorkflowNodeBase(SQLModel):
    project_id: str = Field(foreign_key="projects.id", index=True)
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


class WorkflowNodeCreate(WorkflowNodeBase):
    pass


class WorkflowNodeRead(WorkflowNodeBase):
    id: str
    created_at: datetime
    updated_at: datetime


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


class WorkflowEdgeCreate(WorkflowEdgeBase):
    pass


class WorkflowEdgeRead(WorkflowEdgeBase):
    id: str
    created_at: datetime


# ---------------------------------------------------------------------------
# assets
# ---------------------------------------------------------------------------

ASSET_TYPES = [
    "character_image",
    "scene_image",
    "storyboard_image",
    "video",
    "audio",
    "document",
    "reference",
]


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


class AssetCreate(AssetBase):
    pass


class AssetRead(AssetBase):
    id: str
    created_at: datetime


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

class MessageBase(SQLModel):
    project_id: str = Field(foreign_key="projects.id", index=True)
    role: str   # user | assistant | system | tool
    content: str
    metadata_json: Optional[str] = None
    archived: bool = False


class Message(MessageBase, table=True):
    __tablename__ = "messages"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=now)


class MessageCreate(MessageBase):
    pass


class MessageRead(MessageBase):
    id: str
    created_at: datetime


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

TASK_TYPES = [
    "agent_loop",
    "planning",
    "character_generation",
    "outline_generation",
    "script_generation",
    "script_review",
    "storyboard_generation",
    "image_prompt_generation",
    "video_prompt_generation",
]


class ModelConfigBase(SQLModel):
    task_type: str = Field(index=True)
    provider: str
    model_name: str
    llm_provider_name: Optional[str] = Field(None, index=True,
        description="引用 llm_providers.name；ConfigStore 同步时设置")
    temperature: float = 0.7
    max_tokens: int = 4000
    top_p: float = 1.0
    fallback_model: Optional[str] = None
    enabled: bool = True
    extra_json: Optional[str] = None


class ModelConfig(ModelConfigBase, table=True):
    __tablename__ = "model_configs"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class ModelConfigCreate(ModelConfigBase):
    pass


class ModelConfigRead(ModelConfigBase):
    id: str
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# media_providers — user-configured image/video/audio model endpoints
# Multiple per kind; exactly one (or zero) is_active per kind.
# ---------------------------------------------------------------------------

MEDIA_KINDS = ["image", "video", "audio"]
MEDIA_API_FORMATS = ["openai", "raw", "raw_post", "volcengine_ark", "xai_video", "grok_1_5", "suno_compatible", "openai_tts"]


class MediaProviderBase(SQLModel):
    kind: str = Field(index=True)            # image | video | audio
    name: str = Field(index=True)            # user-supplied label, e.g. "fal-flux-pro"
    base_url: str
    api_key: Optional[str] = None
    model_name: str                          # model id sent in payload
    api_format: str = "openai"               # openai | raw | raw_post | volcengine_ark | xai_video | grok_1_5 | suno_compatible | openai_tts
    params_json: Optional[str] = None        # default extra params JSON (size, steps, etc.)
    is_active: bool = False
    enabled: bool = True
    notes: Optional[str] = None


class MediaProvider(MediaProviderBase, table=True):
    __tablename__ = "media_providers"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class MediaProviderCreate(MediaProviderBase):
    pass


class MediaProviderRead(MediaProviderBase):
    id: str
    created_at: datetime
    updated_at: datetime


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
    supports_prompt_cache: Optional[bool] = None
    supports_vision: Optional[bool] = None
    tokenizer: Optional[str] = None
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
# versions
# ---------------------------------------------------------------------------

class VersionBase(SQLModel):
    project_id: str = Field(foreign_key="projects.id", index=True)
    target_type: str    # project | character | episode | scene | shot | node
    target_id: str
    version_number: int
    snapshot_json: str   # {"before": {...}, "after": {...}}
    message: Optional[str] = None


class Version(VersionBase, table=True):
    __tablename__ = "versions"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=now)


class VersionCreate(VersionBase):
    pass


class VersionRead(VersionBase):
    id: str
    created_at: datetime


# ---------------------------------------------------------------------------
# user_memory — cross-project, long-lived facts about the user (preferences,
# voice/style, recurring naming conventions, model choices)
# ---------------------------------------------------------------------------

class UserMemoryBase(SQLModel):
    kind: str = Field(index=True)   # preference | style | naming | model | fact
    content: str
    source_project_id: Optional[str] = Field(default=None, index=True)
    hits: int = 0


class UserMemory(UserMemoryBase, table=True):
    __tablename__ = "user_memory"

    id: str = Field(default_factory=gen_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=now)
    last_used_at: Optional[datetime] = None


class UserMemoryCreate(UserMemoryBase):
    pass


class UserMemoryRead(UserMemoryBase):
    id: str
    created_at: datetime
    last_used_at: Optional[datetime] = None
