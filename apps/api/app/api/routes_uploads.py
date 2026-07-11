"""Multipart file upload route.

POST /api/uploads/{project_id}  (multipart/form-data with `file` field)
  → 200 {rel_path, filename, size, mime_type, kind}
  → 400 if filename escapes sandbox
  → 413 if size exceeds limit

Files land under `data/storage/<project_id>/uploads/<uuid>-<filename>`.
The agent then sees `rel_path` and can use `file.extract_text_from_upload`,
`drama.parse_uploaded_script`, or `upload:<rel_path>` inside `fields.references`.
"""
from __future__ import annotations

import json
import mimetypes
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.db.models import Project
from app.db.session import session_scope
from app.mcp_tools.file_tools import _project_dir, _safe_path, write_image_base64_cache
from app.services.media_url_signing import verify_media_url_signature

router = APIRouter()


MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB

_SCRIPT_SUFFIXES = {".txt", ".md", ".docx", ".rtf"}
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".m4v"}
_DOC_SUFFIXES = {".pdf", ".doc", ".csv", ".json", ".yaml", ".yml"}


def _classify(filename: str, mime_type: str | None) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in _IMAGE_SUFFIXES or (mime_type or "").startswith("image/"):
        return "image"
    if suffix in _VIDEO_SUFFIXES or (mime_type or "").startswith("video/"):
        return "video"
    if suffix in _SCRIPT_SUFFIXES:
        return "script"
    if suffix in _DOC_SUFFIXES:
        return "document"
    return "other"


def _validate_signature_query(request: Request) -> None:
    expires = request.query_params.get("expires")
    signature = request.query_params.get("signature")
    if expires is None and signature is None:
        return
    if not verify_media_url_signature(request.url.path, expires, signature):
        raise HTTPException(status_code=403, detail="Invalid or expired media URL signature")


@router.post("/{project_id}")
async def upload_file(project_id: str, file: UploadFile = File(...)) -> dict:
    raw_name = Path(file.filename or "upload.bin").name  # strip any path
    if not raw_name or raw_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid filename")

    attachment_id = uuid.uuid4().hex[:8]
    unique_name = f"{attachment_id}-{raw_name}"
    rel_path = f"uploads/{unique_name}"

    try:
        target = _safe_path(project_id, rel_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    target.parent.mkdir(parents=True, exist_ok=True)

    size = 0
    chunk_size = 1 << 20  # 1 MiB
    with target.open("wb") as fh:
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                fh.close()
                target.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"File exceeds {MAX_UPLOAD_BYTES} bytes",
                )
            fh.write(chunk)

    mime_type = file.content_type or mimetypes.guess_type(raw_name)[0]
    kind = _classify(raw_name, mime_type)

    payload = {
        "attachment_id": attachment_id,
        "rel_path": rel_path,
        "filename": raw_name,
        "size": size,
        "mime_type": mime_type,
        "kind": kind,
        "url": f"/api/uploads/{project_id}/file/{rel_path}",
    }
    if kind == "image":
        payload.update(
            write_image_base64_cache(
                project_id,
                rel_path,
                source_path=target,
                mime_type=mime_type,
            )
        )
    return payload


@router.get("/{project_id}/file/{rel_path:path}")
async def get_uploaded_file(project_id: str, rel_path: str, request: Request):
    _validate_signature_query(request)
    try:
        target = _safe_path(project_id, rel_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    uploads_root = (_project_dir(project_id) / "uploads").resolve()
    resolved = target.resolve()
    if uploads_root not in resolved.parents and resolved != uploads_root:
        raise HTTPException(status_code=400, detail="Path outside uploads")
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    mime_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    return FileResponse(
        str(resolved),
        media_type=mime_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/{project_id}/reference/{ref_id}")
async def get_reference_asset_file(project_id: str, ref_id: str, request: Request):
    _validate_signature_query(request)
    async with session_scope() as session:
        project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        state = json.loads(project.state_json or "{}")
    except json.JSONDecodeError:
        state = {}
    store = state.get("reference_assets") if isinstance(state.get("reference_assets"), dict) else {}
    assets = store.get("assets") if isinstance(store.get("assets"), list) else []
    ref = next(
        (item for item in assets if isinstance(item, dict) and item.get("ref_id") == ref_id),
        None,
    )
    if not ref:
        raise HTTPException(status_code=404, detail="Reference asset not found")

    target: Path | None = None
    rel_path = str(ref.get("rel_path") or "").strip()
    if rel_path and not rel_path.startswith(("asset:", "node:")):
        try:
            target = _safe_path(project_id, rel_path)
        except ValueError:
            target = None
    source_path = str(ref.get("source_path") or "").strip()
    if source_path:
        candidate = Path(source_path).expanduser().resolve()
        if candidate.exists() and candidate.is_file():
            target = candidate
    if target is None or not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Reference file not found")
    mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(
        str(target),
        media_type=mime_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )
