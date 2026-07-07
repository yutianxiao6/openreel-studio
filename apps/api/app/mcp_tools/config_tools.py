"""Runtime config helpers — runtime.jsonc 文件即真相源。

Agent-facing registry only keeps read/validate helpers. Writes go through
REST/control-plane paths that call these Python helpers directly.
"""
from __future__ import annotations

from typing import Any

from app.config_store import get_store


async def config_read(*, mask_secrets: bool = True) -> dict[str, Any]:
    """读取当前 runtime 配置（结构化）。

    默认 mask api_key（Agent 视角）。UI 拉密文要显式传 mask_secrets=False。
    返回: {$schema_version, llm_providers, media_providers, model_tier_defaults, model_assignments, app_settings}
    """
    store = get_store()
    return await store.read(mask_secrets=mask_secrets)


async def config_read_file(*, mask_secrets: bool = True) -> dict[str, Any]:
    """返回原始 JSONC 文本 + 解析后结构 + 校验状态。

    UI 的"原始文件"Tab 用这个一次拉到所有需要的视图。
    """
    store = get_store()
    raw = await store.get_raw_text()
    parsed = await store.read(mask_secrets=mask_secrets)
    ok, errors = await store.validate_text(raw)
    return {
        "raw_text": raw,
        "parsed": parsed,
        "valid": ok,
        "errors": errors,
        "file_path": str(store.file_path),
    }


async def config_validate(content: str) -> dict[str, Any]:
    """干跑校验，不写入。用于"应用前预览错误"场景。"""
    store = get_store()
    ok, errors = await store.validate_text(content)
    return {"ok": ok, "errors": errors}


async def config_write_file(content: str) -> dict[str, Any]:
    """整段覆盖写入文件（UI 原始编辑器 / 命令行手改场景）。

    流程: parse → schema 校验 → 临时文件 → 原子 replace → 同步 DB → 更新缓存。
    校验失败时文件和 DB 都不动。
    """
    store = get_store()
    ok, errors = await store.write_raw_text(content)
    return {
        "ok": ok,
        "errors": errors,
        "config": (await store.read(mask_secrets=True)) if ok else None,
    }


async def config_patch(patch: dict) -> dict[str, Any]:
    """局部更新当前配置（推荐 Agent / 表单按钮用）。

    语义: deep merge — dict 递归合并，list/标量整体替换，None 表示删除该键。

    REST patch body 示例:
        # 加一个 LLM provider（注意 list 是整体替换，要带上现有所有项）
        {"patch": {"llm_providers": [...完整新数组...]}}

        # 改某个 task 的 provider 引用
        {"patch": {"model_assignments": {"script_generation": "gpt-4o-aihubmix"}}}

        # 改 Agent 偏好
        {"patch": {"app_settings": {"agent.max_iterations": 120}}}

    校验失败返回 {"ok": false, "errors": [...]}; 文件和 DB 不动。
    """
    store = get_store()
    ok, errors = await store.patch(patch)
    return {
        "ok": ok,
        "errors": errors,
        "config": (await store.read(mask_secrets=True)) if ok else None,
    }


async def config_reload() -> dict[str, Any]:
    """强制从文件重读（用户在 IDE 改完手动触发的场景）。"""
    store = get_store()
    ok, errors = await store.reload()
    return {"ok": ok, "errors": errors}


# ── 兼容旧接口 ────────────────────────────────────────────────────────────


async def config_list_all() -> dict[str, Any]:
    """向后兼容：返回 LLM / 图片 / 视频 / API Keys 总览。

    保持上一版本调用方（前端 /config 命令、设置弹窗 v1）兼容。新代码用 config.read。
    """
    from app.db.models import AppSetting, LlmProvider, MediaProvider, ModelConfig
    from app.db.session import session_scope
    from sqlmodel import select
    import json as _json

    store = get_store()
    cfg = await store.get_runtime()

    llm_list: list[dict] = []
    image_list: list[dict] = []
    video_list: list[dict] = []
    settings_dict: dict = {}

    async with session_scope() as session:
        for p in (await session.exec(select(LlmProvider))).all():
            try:
                params = _json.loads(p.params_json or "{}")
            except Exception:
                params = {}
            llm_list.append({
                "id": p.id, "name": p.name, "provider": p.provider,
                "model_name": p.model_name, "base_url": p.base_url,
                "context_window_tokens": p.context_window_tokens,
                "max_input_tokens": p.max_input_tokens,
                "max_output_tokens": p.max_output_tokens,
                "supports_prompt_cache": p.supports_prompt_cache,
                "supports_vision": p.supports_vision,
                "tokenizer": p.tokenizer,
                "tier": getattr(p, "tier", "balanced") or "balanced",
                "params": params,
                "enabled": p.enabled,
                "notes": p.notes,
            })
        for m in (await session.exec(select(MediaProvider))).all():
            entry = {
                "id": m.id, "name": m.name, "model_name": m.model_name,
                "base_url": m.base_url, "api_format": m.api_format,
                "is_active": m.is_active, "enabled": m.enabled, "notes": m.notes,
            }
            (image_list if m.kind == "image" else video_list).append(entry)
        for s in (await session.exec(select(AppSetting))).all():
            try:
                settings_dict[s.key] = _json.loads(s.value_json)
            except Exception:
                settings_dict[s.key] = s.value_json

        # task → provider name 映射
        task_map: dict[str, str | None] = {}
        for c in (await session.exec(select(ModelConfig))).all():
            task_map[c.task_type] = c.llm_provider_name

    active_image = next((p["name"] for p in image_list if p["is_active"]), None)
    active_video = next((p["name"] for p in video_list if p["is_active"]), None)

    return {
        "llm_providers": llm_list,
        "image": image_list,
        "video": video_list,
        "model_tier_defaults": dict(cfg.model_tier_defaults),
        "model_assignments": task_map,
        "app_settings": settings_dict,
        "summary": {
            "llm_providers": len(llm_list),
            "image_providers": len(image_list),
            "video_providers": len(video_list),
            "active_image": active_image,
            "active_video": active_video,
            "schema_version": cfg.schema_version_,
        },
    }
