"""Asset read endpoints."""
from __future__ import annotations

import json
import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db.models import Asset, Project
from app.db.session import get_session
from app.services.asset_library_paths import asset_library_roots

router = APIRouter()


def _inline_content_disposition(filename: str) -> str:
    safe_name = filename.replace("\\", "_").replace('"', "_")
    return f'inline; filename="{safe_name}"'


@router.get("/{project_id}")
async def list_assets(project_id: str, db: AsyncSession = Depends(get_session)):
    result = await db.exec(
        select(Asset).where(Asset.project_id == project_id).order_by(Asset.created_at)
    )
    assets = list(result.all())
    return {"project_id": project_id, "assets": [a.model_dump() for a in assets]}


@router.get("/{project_id}/preview")
async def preview_asset_library_file(
    project_id: str,
    path: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_session),
):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        state = json.loads(project.state_json or "{}")
    except json.JSONDecodeError:
        state = {}
    library = state.get("asset_library") if isinstance(state.get("asset_library"), dict) else {}
    allowed_roots = asset_library_roots(library)

    target = Path(path).expanduser().resolve()
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Asset file not found")
    allowed = False
    for root in allowed_roots:
        try:
            target.relative_to(root)
            allowed = True
            break
        except ValueError:
            continue
    if not allowed:
        raise HTTPException(status_code=403, detail="Path outside configured asset library")

    mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(
        str(target),
        media_type=mime_type,
        headers={
            "Cache-Control": "public, max-age=3600",
            "Content-Disposition": _inline_content_disposition(target.name),
            "Accept-Ranges": "bytes",
        },
    )
