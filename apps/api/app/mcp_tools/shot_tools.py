"""Scene / Shot / Asset CRUD tools."""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from sqlmodel import select

from app.db.models import Asset, Scene, Shot
from app.db.session import session_scope


# ── Scenes ──────────────────────────────────────────────────────────────

async def create_scene(
    project_id: str,
    episode_id: str | None = None,
    name: str | None = None,
    location: str | None = None,
    time_of_day: str | None = None,
    characters: list[str] | None = None,
    summary: str | None = None,
) -> dict:
    async with session_scope() as session:
        scene = Scene(
            project_id=project_id,
            episode_id=episode_id,
            name=name,
            location=location,
            time_of_day=time_of_day,
            characters_json=json.dumps(characters or [], ensure_ascii=False),
            summary=summary,
        )
        session.add(scene)
        await session.commit()
        await session.refresh(scene)
        return {"id": scene.id, "name": scene.name}


async def list_scenes(project_id: str, episode_id: str | None = None) -> list[dict]:
    async with session_scope() as session:
        stmt = select(Scene).where(Scene.project_id == project_id)
        if episode_id:
            stmt = stmt.where(Scene.episode_id == episode_id)
        rows = (await session.exec(stmt)).all()
        return [
            {
                "id": s.id,
                "name": s.name,
                "location": s.location,
                "time_of_day": s.time_of_day,
                "summary": s.summary,
                "episode_id": s.episode_id,
            }
            for s in rows
        ]


# ── Shots ───────────────────────────────────────────────────────────────

async def create_shot(
    project_id: str,
    shot_number: int,
    episode_id: str | None = None,
    scene_id: str | None = None,
    shot_type: str | None = None,
    camera: str | None = None,
    duration: int | None = None,
    content: str | None = None,
    dialogue: str | None = None,
    image_prompt: str | None = None,
    video_prompt: str | None = None,
) -> dict:
    async with session_scope() as session:
        shot = Shot(
            project_id=project_id,
            episode_id=episode_id,
            scene_id=scene_id,
            shot_number=shot_number,
            shot_type=shot_type,
            camera=camera,
            duration=duration,
            content=content,
            dialogue=dialogue,
            image_prompt=image_prompt,
            video_prompt=video_prompt,
        )
        session.add(shot)
        await session.commit()
        await session.refresh(shot)
        return {"id": shot.id, "shot_number": shot.shot_number}


async def list_shots(project_id: str, episode_id: str | None = None) -> list[dict]:
    async with session_scope() as session:
        stmt = select(Shot).where(Shot.project_id == project_id)
        if episode_id:
            stmt = stmt.where(Shot.episode_id == episode_id)
        stmt = stmt.order_by(Shot.shot_number)
        rows = (await session.exec(stmt)).all()
        return [
            {
                "id": s.id,
                "shot_number": s.shot_number,
                "shot_type": s.shot_type,
                "camera": s.camera,
                "duration": s.duration,
                "content": s.content,
                "dialogue": s.dialogue,
                "image_prompt": s.image_prompt,
                "video_prompt": s.video_prompt,
                "asset_id": s.asset_id,
            }
            for s in rows
        ]


async def update_shot(shot_id: str, patch: dict) -> dict:
    async with session_scope() as session:
        shot = await session.get(Shot, shot_id)
        if not shot:
            return {"error": "Shot not found"}
        for key, value in patch.items():
            if hasattr(shot, key):
                setattr(shot, key, value)
        shot.updated_at = datetime.utcnow()
        session.add(shot)
        await session.commit()
        return {"id": shot.id}


# ── Assets ──────────────────────────────────────────────────────────────

async def register_asset(
    project_id: str,
    asset_type: str,
    name: str,
    path: str | None = None,
    url: str | None = None,
    mime_type: str | None = None,
    metadata: dict | None = None,
    prompt: str | None = None,
    model_name: str | None = None,
    node_id: str | None = None,
) -> dict:
    async with session_scope() as session:
        asset = Asset(
            id=str(uuid.uuid4()),
            project_id=project_id,
            node_id=node_id,
            type=asset_type,
            name=name,
            path=path,
            url=url,
            mime_type=mime_type,
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
            prompt=prompt,
            model_name=model_name,
        )
        session.add(asset)
        await session.commit()
        await session.refresh(asset)
        return {"id": asset.id, "type": asset.type, "path": asset.path, "url": asset.url}


async def list_assets(project_id: str, asset_type: str | None = None) -> list[dict]:
    async with session_scope() as session:
        stmt = select(Asset).where(Asset.project_id == project_id)
        if asset_type:
            stmt = stmt.where(Asset.type == asset_type)
        rows = (await session.exec(stmt)).all()
        return [
            {
                "id": a.id,
                "type": a.type,
                "name": a.name,
                "path": a.path,
                "url": a.url,
                "prompt": a.prompt,
                "node_id": a.node_id,
            }
            for a in rows
        ]


async def attach_asset_to_shot(shot_id: str, asset_id: str) -> dict:
    async with session_scope() as session:
        shot = await session.get(Shot, shot_id)
        if not shot:
            return {"error": "Shot not found"}
        shot.asset_id = asset_id
        session.add(shot)
        await session.commit()
        return {"shot_id": shot_id, "asset_id": asset_id}
