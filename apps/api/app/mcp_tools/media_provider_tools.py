"""Media Provider MCP Tools — manage image/video/audio provider configurations.

Users configure one or more providers (base_url + api_key + model_name) per
kind (image / video / audio). Exactly one provider per kind can be 'active' at a time;
generate tools use the active provider by default or accept an explicit model
name to pick a specific one.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlmodel import select

from app.db.models import MediaProvider
from app.db.session import session_scope
from app.services.media_provider import test_provider as _test_provider


_MEDIA_API_FORMATS = {"openai", "raw", "raw_post", "image_http_v1", "video_http_v1", "audio_http_v1", "volcengine_ark", "xai_video", "grok_1_5", "t8_grok_video_3", "lingke_media_generate", "suno_compatible", "openai_tts"}
_MEDIA_KINDS = {"image", "video", "audio"}


def _provider_to_dict(p: MediaProvider) -> dict[str, Any]:
    return {
        "id": p.id,
        "kind": p.kind,
        "name": p.name,
        "base_url": p.base_url,
        "model_name": p.model_name,
        "api_format": p.api_format,
        "is_active": p.is_active,
        "enabled": p.enabled,
        "notes": p.notes,
        "params": json.loads(p.params_json or "{}"),
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


async def media_list_providers(kind: str | None = None) -> dict[str, Any]:
    """List all configured media providers, optionally filtered by kind (image/video/audio)."""
    async with session_scope() as session:
        q = select(MediaProvider).where(MediaProvider.enabled == True)
        if kind:
            q = q.where(MediaProvider.kind == kind)
        result = await session.exec(q.order_by(MediaProvider.kind, MediaProvider.name))
        providers = result.all()
    return {
        "providers": [_provider_to_dict(p) for p in providers],
        "count": len(providers),
    }


async def media_add_provider(
    kind: str,
    name: str,
    base_url: str,
    api_key: str,
    model_name: str,
    api_format: str = "openai",
    set_active: bool = False,
    notes: str | None = None,
    params: dict | None = None,
) -> dict[str, Any]:
    """Add a new image, video, or audio provider.

    kind: 'image', 'video', or 'audio'
    name: short identifier you'll reference in generate calls (e.g. 'flux-pro', 'sdxl')
    base_url: versioned or namespaced API base, e.g. 'https://api.openai.com/v1'; do not include resource paths such as /images or /videos
    api_key: provider API key
    model_name: model identifier sent to the API
    api_format: use image_http_v1, video_http_v1, or audio_http_v1 with the matching protocol id in params.
    set_active: if True, mark this as the active provider for this kind
    params: extra default parameters (size, quality, steps, etc.)
    """
    if kind not in _MEDIA_KINDS:
        return {"ok": False, "error": "kind must be 'image', 'video', or 'audio'"}
    if api_format not in _MEDIA_API_FORMATS:
        return {"ok": False, "error": f"api_format must be one of {sorted(_MEDIA_API_FORMATS)}"}

    async with session_scope() as session:
        # Check name collision
        existing = await session.exec(
            select(MediaProvider).where(MediaProvider.kind == kind).where(MediaProvider.name == name)
        )
        if existing.first():
            return {"ok": False, "error": f"Provider '{name}' already exists for kind '{kind}'"}

        if set_active:
            # Deactivate existing active providers of this kind
            actives = await session.exec(
                select(MediaProvider)
                .where(MediaProvider.kind == kind)
                .where(MediaProvider.is_active == True)
            )
            for p in actives.all():
                p.is_active = False
                session.add(p)

        provider = MediaProvider(
            kind=kind,
            name=name,
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            model_name=model_name,
            api_format=api_format,
            is_active=set_active,
            enabled=True,
            notes=notes,
            params_json=json.dumps(params or {}, ensure_ascii=False),
        )
        session.add(provider)
        await session.commit()
        await session.refresh(provider)

    return {"ok": True, "provider": _provider_to_dict(provider)}


async def media_update_provider(
    provider_id: str,
    base_url: str | None = None,
    api_key: str | None = None,
    model_name: str | None = None,
    api_format: str | None = None,
    notes: str | None = None,
    params: dict | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Update an existing provider's settings. Pass only the fields to change."""
    async with session_scope() as session:
        provider = await session.get(MediaProvider, provider_id)
        if not provider:
            return {"ok": False, "error": f"Provider {provider_id} not found"}

        if base_url is not None:
            provider.base_url = base_url.rstrip("/")
        if api_key is not None:
            provider.api_key = api_key
        if model_name is not None:
            provider.model_name = model_name
        if api_format is not None:
            if api_format not in _MEDIA_API_FORMATS:
                return {"ok": False, "error": f"api_format must be one of {sorted(_MEDIA_API_FORMATS)}"}
            provider.api_format = api_format
        if notes is not None:
            provider.notes = notes
        if params is not None:
            provider.params_json = json.dumps(params, ensure_ascii=False)
        if enabled is not None:
            provider.enabled = enabled
        provider.updated_at = datetime.utcnow()

        session.add(provider)
        await session.commit()
        await session.refresh(provider)

    return {"ok": True, "provider": _provider_to_dict(provider)}


async def media_remove_provider(provider_id: str) -> dict[str, Any]:
    """Remove a provider configuration."""
    async with session_scope() as session:
        provider = await session.get(MediaProvider, provider_id)
        if not provider:
            return {"ok": False, "error": f"Provider {provider_id} not found"}
        name = provider.name
        kind = provider.kind
        await session.delete(provider)
        await session.commit()
    return {"ok": True, "removed": name, "kind": kind}


async def media_set_active(provider_id: str) -> dict[str, Any]:
    """Set a provider as the active one for its kind. Deactivates the previous active."""
    async with session_scope() as session:
        provider = await session.get(MediaProvider, provider_id)
        if not provider:
            return {"ok": False, "error": f"Provider {provider_id} not found"}
        if not provider.enabled:
            return {"ok": False, "error": "Provider is disabled; enable it first"}

        # Deactivate all others of the same kind
        actives = await session.exec(
            select(MediaProvider)
            .where(MediaProvider.kind == provider.kind)
            .where(MediaProvider.is_active == True)
        )
        for p in actives.all():
            p.is_active = False
            session.add(p)

        provider.is_active = True
        session.add(provider)
        await session.commit()
        await session.refresh(provider)

    return {"ok": True, "active": _provider_to_dict(provider)}


async def media_test_provider(provider_id: str) -> dict[str, Any]:
    """Test a provider by sending a minimal real request. Returns ok/error + sample_url."""
    return await _test_provider(provider_id)


async def media_get_active(kind: str) -> dict[str, Any]:
    """Get the currently active provider for a kind (image/video/audio)."""
    async with session_scope() as session:
        result = await session.exec(
            select(MediaProvider)
            .where(MediaProvider.kind == kind)
            .where(MediaProvider.is_active == True)
            .where(MediaProvider.enabled == True)
        )
        provider = result.first()
    if not provider:
        return {"ok": False, "active": None, "error": f"No active {kind} provider configured"}
    return {"ok": True, "active": _provider_to_dict(provider)}
