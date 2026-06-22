"""ConfigStore — runtime.jsonc 文件 ↔ DB 物化缓存的中间层。

职责：
  1. 启动时 bootstrap：runtime.jsonc 不存在则从 .env 种一份默认文件
  2. load()：读文件 → JSON5 parse → schema 校验 → upsert DB → 更新内存缓存
  3. write_raw_text() / patch()：唯一的写入口，全部走文件
  4. start_watcher()：watchfiles 监听文件变更，外部编辑器手改也自动 reload
  5. 写竞争用 asyncio.Lock 串行化；文件写用临时文件 + os.replace 原子替换

任何来路（Agent / UI / 编辑器）改配置都过这里。DB 表只在 load() 里被改。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import json5
from pydantic import ValidationError
from sqlalchemy import text
from sqlmodel import select

from app.config_store.schema import (
    ALLOWED_TASK_TYPES,
    DEFAULT_APP_SETTINGS,
    LlmProviderEntry,
    MediaProviderEntry,
    RuntimeConfig,
)
from app.db.models import AppSetting, LlmProvider, MediaProvider, ModelConfig
from app.db.session import session_scope

logger = logging.getLogger(__name__)


def _resolve_secret(value: str | None) -> str | None:
    """Resolve ${ENV_VAR} references without persisting the secret in runtime.jsonc."""
    if not value or not value.startswith("${") or not value.endswith("}"):
        return value
    return os.getenv(value[2:-1]) or None


def _deep_merge(base: dict, patch: dict) -> dict:
    """patch 覆盖 base，dict 递归合并；list/标量整体替换；None 表示删除键。"""
    result = dict(base)
    for k, v in patch.items():
        if v is None:
            result.pop(k, None)
        elif isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _format_validation_errors(exc: ValidationError) -> list[str]:
    out: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) if err.get("loc") else "(root)"
        out.append(f"{loc}: {err['msg']}")
    return out


def _mask_key(key: Optional[str]) -> Optional[str]:
    if not key:
        return key
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]


def _mask_runtime(data: dict) -> dict:
    """深拷贝并对 api_key 字段打码；用于给 Agent 看的视图。"""
    out = json.loads(json.dumps(data, ensure_ascii=False))
    for p in out.get("llm_providers", []):
        if "api_key" in p:
            p["api_key"] = _mask_key(p.get("api_key"))
    for p in out.get("media_providers", []):
        if "api_key" in p:
            p["api_key"] = _mask_key(p.get("api_key"))
    return out


class ConfigStore:
    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self._cached: Optional[RuntimeConfig] = None
        self._raw_text: str = ""
        self._lock = asyncio.Lock()
        self._watch_task: Optional[asyncio.Task] = None
        self._suppress_next_event = False

    # ── 公开接口 ────────────────────────────────────────────

    async def get_runtime(self) -> RuntimeConfig:
        if self._cached is None:
            ok, errs = await self.load()
            if not ok:
                raise RuntimeError(f"config load failed: {errs}")
        assert self._cached is not None
        return self._cached

    async def get_raw_text(self) -> str:
        if self._cached is None:
            await self.load()
        return self._raw_text

    async def read(self, *, mask_secrets: bool = False) -> dict[str, Any]:
        cfg = await self.get_runtime()
        data = cfg.model_dump(by_alias=True)
        if mask_secrets:
            data = _mask_runtime(data)
        return data

    async def validate_text(self, content: str) -> tuple[bool, list[str]]:
        try:
            parsed = json5.loads(content)
        except Exception as exc:
            return False, [f"JSON5 parse error: {exc}"]
        try:
            RuntimeConfig.model_validate(parsed)
        except ValidationError as exc:
            return False, _format_validation_errors(exc)
        except Exception as exc:
            return False, [f"validation error: {exc}"]
        return True, []

    async def write_raw_text(self, content: str) -> tuple[bool, list[str]]:
        async with self._lock:
            ok, errs = await self.validate_text(content)
            if not ok:
                return False, errs
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file_path.with_suffix(self.file_path.suffix + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            self._suppress_next_event = True
            os.replace(tmp, self.file_path)
            return await self._load_locked()

    async def patch(self, patch_data: dict) -> tuple[bool, list[str]]:
        """局部更新当前配置：dict 走 deep merge，list 整体替换。"""
        current = await self.read()
        merged = _deep_merge(current, patch_data)
        return await self.write_raw_text(_format_jsonc(merged))

    async def reload(self) -> tuple[bool, list[str]]:
        async with self._lock:
            return await self._load_locked()

    async def load(self) -> tuple[bool, list[str]]:
        async with self._lock:
            return await self._load_locked()

    # ── 启动 bootstrap ──────────────────────────────────────

    async def bootstrap(self, env_keys: dict[str, str]) -> tuple[bool, list[str]]:
        """首次启动：runtime.jsonc 缺失则从 env keys 种一份默认。"""
        if self.file_path.exists():
            return await self.reload()
        seeded = _seed_default_config(env_keys)
        async with self._lock:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            self.file_path.write_text(_format_jsonc(seeded, header=True), encoding="utf-8")
            return await self._load_locked()

    # ── 文件 watcher ────────────────────────────────────────

    async def start_watcher(self) -> None:
        if self._watch_task is not None:
            return
        try:
            from watchfiles import awatch
        except ImportError:
            logger.warning("watchfiles 未安装，配置文件外部修改不会自动加载")
            return
        self._watch_task = asyncio.create_task(self._watch_loop(awatch))

    async def _watch_loop(self, awatch) -> None:
        try:
            async for _changes in awatch(str(self.file_path.parent)):
                if self._suppress_next_event:
                    self._suppress_next_event = False
                    continue
                changed = any(Path(p).resolve() == self.file_path.resolve()
                              for _, p in _changes)
                if not changed:
                    continue
                ok, errs = await self.reload()
                if ok:
                    logger.info("配置文件已 reload (外部修改)")
                else:
                    logger.warning("配置文件外部修改但校验失败，保留旧配置: %s", errs)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("config watcher crashed")

    async def stop_watcher(self) -> None:
        if self._watch_task is not None:
            self._watch_task.cancel()
            self._watch_task = None

    # ── 内部 ────────────────────────────────────────────────

    async def _load_locked(self) -> tuple[bool, list[str]]:
        if not self.file_path.exists():
            return False, [f"配置文件不存在: {self.file_path}"]
        try:
            raw_text = self.file_path.read_text(encoding="utf-8")
        except Exception as exc:
            return False, [f"读文件失败: {exc}"]
        try:
            parsed = json5.loads(raw_text)
        except Exception as exc:
            return False, [f"JSON5 parse error: {exc}"]
        try:
            cfg = RuntimeConfig.model_validate(parsed)
        except ValidationError as exc:
            return False, _format_validation_errors(exc)
        await _sync_to_db(cfg)
        self._cached = cfg
        self._raw_text = raw_text
        return True, []


# ── DB 同步 ────────────────────────────────────────────────────────────────


async def _sync_to_db(cfg: RuntimeConfig) -> None:
    """以文件为权威把 4 张表 upsert。文件没的删。"""
    async with session_scope() as session:
        await _ensure_runtime_config_columns(session)
        # llm_providers
        existing = (await session.exec(select(LlmProvider))).all()
        by_name = {p.name: p for p in existing}
        keep: set[str] = set()
        for entry in cfg.llm_providers:
            keep.add(entry.name)
            row = by_name.get(entry.name)
            params_json = json.dumps(entry.params, ensure_ascii=False) if entry.params else None
            if row is None:
                row = LlmProvider(
                    name=entry.name, provider=entry.provider, model_name=entry.model_name,
                    base_url=entry.base_url, api_key=_resolve_secret(entry.api_key),
                    context_window_tokens=entry.context_window_tokens,
                    max_input_tokens=entry.max_input_tokens,
                    max_output_tokens=entry.max_output_tokens,
                    supports_prompt_cache=entry.supports_prompt_cache,
                    supports_vision=entry.supports_vision,
                    tokenizer=entry.tokenizer,
                    params_json=params_json,
                    is_default=entry.is_default, enabled=entry.enabled, notes=entry.notes,
                )
                session.add(row)
            else:
                row.provider = entry.provider
                row.model_name = entry.model_name
                row.base_url = entry.base_url
                row.api_key = _resolve_secret(entry.api_key)
                row.context_window_tokens = entry.context_window_tokens
                row.max_input_tokens = entry.max_input_tokens
                row.max_output_tokens = entry.max_output_tokens
                row.supports_prompt_cache = entry.supports_prompt_cache
                row.supports_vision = entry.supports_vision
                row.tokenizer = entry.tokenizer
                row.params_json = params_json
                row.is_default = entry.is_default
                row.enabled = entry.enabled
                row.notes = entry.notes
                session.add(row)
        for name, row in by_name.items():
            if name not in keep:
                await session.delete(row)
        await session.flush()

        # media_providers
        existing_m = (await session.exec(select(MediaProvider))).all()
        by_key = {(m.kind, m.name): m for m in existing_m}
        keep_m: set[tuple[str, str]] = set()
        for entry in cfg.media_providers:
            key = (entry.kind, entry.name)
            keep_m.add(key)
            row = by_key.get(key)
            params_json = json.dumps(entry.params, ensure_ascii=False) if entry.params else None
            if row is None:
                row = MediaProvider(
                    kind=entry.kind, name=entry.name, base_url=entry.base_url,
                    api_key=_resolve_secret(entry.api_key), model_name=entry.model_name,
                    api_format=entry.api_format, params_json=params_json,
                    is_active=entry.is_active, enabled=entry.enabled, notes=entry.notes,
                )
                session.add(row)
            else:
                row.base_url = entry.base_url
                row.api_key = _resolve_secret(entry.api_key)
                row.model_name = entry.model_name
                row.api_format = entry.api_format
                row.params_json = params_json
                row.is_active = entry.is_active
                row.enabled = entry.enabled
                row.notes = entry.notes
                session.add(row)
        for key, row in by_key.items():
            if key not in keep_m:
                await session.delete(row)
        await session.flush()

        # model_configs：完全替换为文件里 model_assignments 的内容
        existing_cfg = (await session.exec(select(ModelConfig))).all()
        by_task = {c.task_type: c for c in existing_cfg}
        provider_lookup = {p.name: p for p in cfg.llm_providers}
        keep_t: set[str] = set()
        for task, prov_name in cfg.model_assignments.items():
            if prov_name is None:
                continue
            keep_t.add(task)
            entry = provider_lookup.get(prov_name)
            if entry is None:
                continue
            row = by_task.get(task)
            if row is None:
                row = ModelConfig(
                    task_type=task, provider=entry.provider, model_name=entry.model_name,
                    llm_provider_name=entry.name,
                    max_tokens=entry.max_output_tokens or 4000,
                    enabled=True,
                )
                session.add(row)
            else:
                row.provider = entry.provider
                row.model_name = entry.model_name
                row.llm_provider_name = entry.name
                row.max_tokens = entry.max_output_tokens or 4000
                row.enabled = True
                session.add(row)
        for task, row in by_task.items():
            if task not in keep_t:
                await session.delete(row)
        await session.flush()

        # app_settings
        existing_s = (await session.exec(select(AppSetting))).all()
        by_skey = {s.key: s for s in existing_s}
        keep_s: set[str] = set()
        for k, v in cfg.app_settings.items():
            keep_s.add(k)
            row = by_skey.get(k)
            value_json = json.dumps(v, ensure_ascii=False)
            if row is None:
                row = AppSetting(key=k, value_json=value_json,
                                 category=k.split(".", 1)[0] if "." in k else "general")
                session.add(row)
            else:
                row.value_json = value_json
                session.add(row)
        for k, row in by_skey.items():
            if k not in keep_s:
                await session.delete(row)
        await session.flush()

        await session.commit()


async def _ensure_runtime_config_columns(session) -> None:
    """Config loading can run before app startup migrations in tests and scripts."""
    rows = (await session.exec(text("PRAGMA table_info(llm_providers)"))).all()
    existing = {row[1] for row in rows}
    for column, ddl_type in (
        ("context_window_tokens", "INTEGER"),
        ("max_input_tokens", "INTEGER"),
        ("max_output_tokens", "INTEGER"),
        ("supports_prompt_cache", "BOOLEAN"),
        ("supports_vision", "BOOLEAN"),
        ("tokenizer", "VARCHAR"),
        ("params_json", "VARCHAR"),
    ):
        if column not in existing:
            await session.exec(
                text(f"ALTER TABLE llm_providers ADD COLUMN {column} {ddl_type}")
            )


# ── 文件写出 ───────────────────────────────────────────────────────────────


_HEADER = """// OpenReel Studio 运行时配置（真相源）
// 改完保存后会自动加载到运行时。文件不合法时保留旧配置不切换。
// 模型也通过 config.* 工具改这个文件，所有写入路径统一。
"""


def _format_jsonc(data: dict, *, header: bool = False) -> str:
    """把 dict 序列化成 JSONC（标准 JSON + 顶部注释 header）。"""
    body = json.dumps(data, ensure_ascii=False, indent=2)
    return (_HEADER + body + "\n") if header else (body + "\n")


def _seed_default_config(env_keys: dict[str, str]) -> dict:
    """从 env 里的 *_API_KEY 种一份默认 runtime config。"""
    provider_map = [
        ("DEEPSEEK_API_KEY", "deepseek-default", "deepseek", "deepseek-chat", None),
        ("OPENAI_API_KEY", "openai-default", "openai", "gpt-4o-mini", None),
        ("ANTHROPIC_API_KEY", "anthropic-default", "anthropic",
         "claude-haiku-4-5-20251001", None),
        ("DASHSCOPE_API_KEY", "dashscope-default", "dashscope", "qwen-turbo", None),
        ("GEMINI_API_KEY", "gemini-default", "gemini", "gemini-2.5-flash", None),
    ]
    llm: list[dict] = []
    first = True
    for env_key, name, provider, model, base in provider_map:
        val = env_keys.get(env_key)
        if not val:
            continue
        llm.append({
            "name": name, "provider": provider, "model_name": model,
            "base_url": base, "api_key": f"${{{env_key}}}", "is_default": first, "enabled": True,
            "context_window_tokens": None,
            "max_input_tokens": None,
            "max_output_tokens": None,
            "supports_prompt_cache": None,
            "supports_vision": None,
            "tokenizer": None,
            "params": {},
        })
        first = False

    return {
        "$schema_version": 1,
        "llm_providers": llm,
        "media_providers": [],
        "model_assignments": {t: None for t in ALLOWED_TASK_TYPES},
        "app_settings": dict(DEFAULT_APP_SETTINGS),
    }


# ── 单例 ───────────────────────────────────────────────────────────────────


_store: Optional[ConfigStore] = None


def get_store() -> ConfigStore:
    global _store
    if _store is None:
        from app.config import settings
        repo_root = Path(settings.PROJECT_ROOT) if hasattr(settings, "PROJECT_ROOT") \
            else Path(__file__).resolve().parents[3].parent
        path = repo_root / "config" / "runtime.jsonc"
        _store = ConfigStore(path)
    return _store
