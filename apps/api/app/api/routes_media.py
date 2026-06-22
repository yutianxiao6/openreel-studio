"""Static file route for locally stored generated media.

Generated images live under storage/<project_id>/generated_images/<filename>.
Generated videos live under storage/<project_id>/generated_videos/<filename>.
Provider returns a remote URL (often short-lived) and downloads a copy to disk.
The frontend should hit /api/media/<project_id>/<path> for stable access.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import settings

router = APIRouter()


def _storage_root() -> Path:
    return Path(getattr(settings, "STORAGE_PATH", "./storage")).resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _inline_content_disposition(filename: str) -> str:
    safe_name = filename.replace("\\", "_").replace('"', "_")
    return f'inline; filename="{safe_name}"'


def _resolve_media_target(project_id: str, path: str) -> Path:
    if not project_id or "/" in project_id or ".." in project_id:
        raise HTTPException(status_code=400, detail="Invalid project_id")

    project_root = _storage_root() / project_id
    if path.startswith(("generated_images/", "generated_videos/")):
        target = (project_root / path).resolve()
    else:
        target = (project_root / "generated_images" / path).resolve()
    allowed_roots = [
        project_root / "generated_images",
        project_root / "generated_videos",
    ]
    if not any(_is_within(target, root) for root in allowed_roots):
        raise HTTPException(status_code=400, detail="Path outside storage")
    if not target.exists() or not target.is_file():
        # 给 404 也带短缓存,否则前端历史消息里的失效图会让浏览器每次 re-render
        # 都重发一遍请求,后端日志被刷屏。30s 足够覆盖大多数 re-render burst,
        # 又不至于把刚生成的图也卡住。
        raise HTTPException(
            status_code=404,
            detail="File not found",
            headers={"Cache-Control": "public, max-age=30"},
        )
    return target


def _media_response(target: Path) -> FileResponse:
    mime, _ = mimetypes.guess_type(str(target))
    return FileResponse(
        path=str(target),
        media_type=mime or "application/octet-stream",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Content-Disposition": _inline_content_disposition(target.name),
            "Accept-Ranges": "bytes",
        },
    )


@router.get("/{project_id}/{path:path}")
async def get_media(project_id: str, path: str):
    return _media_response(_resolve_media_target(project_id, path))


@router.head("/{project_id}/{path:path}")
async def head_media(project_id: str, path: str):
    return _media_response(_resolve_media_target(project_id, path))
