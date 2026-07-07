"""Workflow plugin loading and execution.

Plugins are workflow node extensions, not Agent tools. A plugin package has a
manifest for UI/schema metadata and optional runtime code for execution.
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import mimetypes
import re
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.db.models import WorkflowNode
from app.db.session import session_scope
from app.services.node_public_ids import resolve_internal_node_id


PLUGIN_RUNNER = "workflow_plugin"
PLUGIN_NODE_KIND = "plugin"

_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,100}$")
_NODE_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,120}$")
_PLUGIN_CACHE: dict[str, Any] | None = None


class WorkflowPluginError(ValueError):
    """Raised when a workflow plugin cannot be loaded or executed."""


@dataclass
class WorkflowPlugin:
    id: str
    name: str
    version: str
    category: str
    description: str
    path: Path
    manifest: dict[str, Any]
    nodes: list[dict[str, Any]]
    errors: list[str] = field(default_factory=list)


def plugin_root() -> Path:
    root = Path(settings.PROJECT_ROOT).resolve() / "plugins"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _as_list(value: Any) -> list[Any]:
    if value in (None, "", {}):
        return []
    return value if isinstance(value, list) else [value]


def _safe_string(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _normalize_node_runtime(plugin: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    runtime = node.get("runtime") if isinstance(node.get("runtime"), dict) else {}
    plugin_runtime = plugin.get("runtime") if isinstance(plugin.get("runtime"), dict) else {}
    merged = {**plugin_runtime, **runtime}
    kind = str(merged.get("kind") or "").strip().lower()
    if kind not in {"python", "http"}:
        kind = "python" if merged.get("entrypoint") else "http" if merged.get("endpoint") else ""
    if kind:
        merged["kind"] = kind
    return merged


def _normalize_plugin_node(plugin: dict[str, Any], node: dict[str, Any], *, plugin_path: Path) -> dict[str, Any]:
    plugin_id = _safe_string(plugin.get("id"))
    version = _safe_string(plugin.get("version"), "0.0.0")
    raw_type = _safe_string(node.get("type") or node.get("id"))
    if not raw_type or not _NODE_TYPE_RE.fullmatch(raw_type):
        raise WorkflowPluginError(f"Invalid plugin node type in {plugin_id}: {raw_type!r}")
    runtime = _normalize_node_runtime(plugin, node)
    return {
        "id": f"{plugin_id}/{raw_type}@{version}",
        "type": raw_type,
        "kind": PLUGIN_NODE_KIND,
        "title": _safe_string(node.get("title") or node.get("name"), raw_type),
        "name": _safe_string(node.get("name") or node.get("title"), raw_type),
        "description": _safe_string(node.get("description") or plugin.get("description")),
        "category": _safe_string(node.get("category") or plugin.get("category"), "plugin"),
        "plugin_id": plugin_id,
        "plugin_name": _safe_string(plugin.get("name"), plugin_id),
        "plugin_version": version,
        "inputs": _as_list(node.get("inputs")),
        "outputs": _as_list(node.get("outputs")),
        "settings": _as_list(node.get("settings")),
        "ui": node.get("ui") if isinstance(node.get("ui"), dict) else {},
        "runtime": {
            "kind": runtime.get("kind"),
            "entrypoint": runtime.get("entrypoint"),
            "endpoint": runtime.get("endpoint"),
        },
        "permissions": plugin.get("permissions") if isinstance(plugin.get("permissions"), dict) else {},
        "manifest_path": str(plugin_path / "plugin.json"),
    }


def _load_one_plugin(path: Path) -> WorkflowPlugin:
    manifest_path = path / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise WorkflowPluginError(f"{manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise WorkflowPluginError(f"{manifest_path}: manifest must be an object")
    plugin_id = _safe_string(manifest.get("id"))
    if not plugin_id or not _PLUGIN_ID_RE.fullmatch(plugin_id):
        raise WorkflowPluginError(f"{manifest_path}: invalid plugin id {plugin_id!r}")
    nodes_raw = manifest.get("nodes")
    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise WorkflowPluginError(f"{manifest_path}: nodes must be a non-empty array")
    nodes: list[dict[str, Any]] = []
    for node in nodes_raw:
        if not isinstance(node, dict):
            raise WorkflowPluginError(f"{manifest_path}: every node must be an object")
        nodes.append(_normalize_plugin_node(manifest, node, plugin_path=path))
    return WorkflowPlugin(
        id=plugin_id,
        name=_safe_string(manifest.get("name"), plugin_id),
        version=_safe_string(manifest.get("version"), "0.0.0"),
        category=_safe_string(manifest.get("category"), "plugin"),
        description=_safe_string(manifest.get("description")),
        path=path,
        manifest=manifest,
        nodes=nodes,
    )


def load_plugins(*, force: bool = False) -> dict[str, Any]:
    global _PLUGIN_CACHE
    if _PLUGIN_CACHE is not None and not force:
        return _PLUGIN_CACHE
    root = plugin_root()
    plugins: list[WorkflowPlugin] = []
    errors: list[dict[str, str]] = []
    for item in sorted(root.iterdir(), key=lambda p: p.name):
        if not item.is_dir() or not (item / "plugin.json").exists():
            continue
        try:
            plugins.append(_load_one_plugin(item))
        except WorkflowPluginError as exc:
            errors.append({"path": str(item), "error": str(exc)})
    nodes: list[dict[str, Any]] = []
    for plugin in plugins:
        nodes.extend(plugin.nodes)
    _PLUGIN_CACHE = {
        "plugins": plugins,
        "nodes": nodes,
        "errors": errors,
    }
    return _PLUGIN_CACHE


def reload_plugins() -> dict[str, Any]:
    return load_plugins(force=True)


def list_plugins() -> list[dict[str, Any]]:
    loaded = load_plugins()
    return [
        {
            "id": plugin.id,
            "name": plugin.name,
            "version": plugin.version,
            "category": plugin.category,
            "description": plugin.description,
            "path": str(plugin.path),
            "node_count": len(plugin.nodes),
            "nodes": plugin.nodes,
        }
        for plugin in loaded["plugins"]
    ]


def plugin_errors() -> list[dict[str, str]]:
    return list(load_plugins().get("errors") or [])


def plugin_node_types() -> list[dict[str, Any]]:
    return list(load_plugins().get("nodes") or [])


def available_extension_ids() -> set[str]:
    return {plugin.id for plugin in load_plugins().get("plugins") or []}


def find_plugin_node(step: dict[str, Any], workflow: dict[str, Any] | None = None) -> tuple[WorkflowPlugin, dict[str, Any]]:
    loaded = load_plugins()
    workflow = workflow if isinstance(workflow, dict) else {}
    extension = step.get("extension") or step.get("plugin") or workflow.get("extension") or workflow.get("plugin")
    plugin_id = ""
    if isinstance(extension, dict):
        plugin_id = _safe_string(extension.get("id") or extension.get("name"))
    else:
        plugin_id = _safe_string(extension)
    node_type = _safe_string(
        step.get("plugin_node_type")
        or step.get("operation")
        or step.get("type")
        or workflow.get("plugin_node_type")
        or workflow.get("operation")
    )
    for plugin in loaded.get("plugins") or []:
        if plugin_id and plugin.id != plugin_id:
            continue
        for node in plugin.nodes:
            if node_type and node["type"] not in {node_type, node_type.split("/")[-1].split("@")[0]} and node["id"] != node_type:
                continue
            return plugin, node
    raise WorkflowPluginError(f"Plugin node not found: plugin={plugin_id or '*'} node={node_type or '*'}")


def _project_storage_root(project_id: str) -> Path:
    return (settings.storage_path_resolved / project_id).resolve()


def _project_upload_root(project_id: str) -> Path:
    return (Path(settings.STORAGE_DIR).resolve() / project_id / "uploads").resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _media_path_from_url(project_id: str, value: str) -> Path | None:
    text = value.strip()
    media_prefix = f"/api/media/{project_id}/"
    upload_prefix = f"/api/uploads/{project_id}/file/"
    if text.startswith(media_prefix):
        rel = text[len(media_prefix):].lstrip("/")
        if rel.startswith(("generated_images/", "generated_videos/", "generated_audio/")):
            return (_project_storage_root(project_id) / rel).resolve()
        return (_project_storage_root(project_id) / "generated_images" / rel).resolve()
    if text.startswith(upload_prefix):
        rel = text[len(upload_prefix):].lstrip("/")
        if rel.startswith("uploads/"):
            rel = rel[len("uploads/"):]
        return (_project_upload_root(project_id) / rel).resolve()
    return None


def _looks_like_node_reference(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    return text.startswith(("node:", "#", "@node:", "@#"))


def _media_reference_from_value(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, str):
        text = value.strip()
        if (
            text.startswith(("http://", "https://", "/api/media/", "/api/uploads/", "generated_videos/", "upload:"))
            or re.search(r"\.(mp4|webm|mov|m4v)(\?|#|$)", text, re.IGNORECASE)
        ):
            return text
        return ""
    if isinstance(value, dict):
        for key in (
            "url",
            "local_url",
            "remote_url",
            "path",
            "local_path",
            "rel_path",
            "output_path",
            "video",
            "source_video",
        ):
            ref = _media_reference_from_value(value.get(key))
            if ref:
                return ref
        for item in value.values():
            ref = _media_reference_from_value(item)
            if ref:
                return ref
        return ""
    if isinstance(value, list):
        for item in value:
            ref = _media_reference_from_value(item)
            if ref:
                return ref
    return ""


def _json_value(raw: str | None) -> Any:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


async def _resolve_canvas_node_media_reference(project_id: str, value: str) -> str:
    if not _looks_like_node_reference(value):
        return ""
    async with session_scope() as session:
        node_id = await resolve_internal_node_id(session, project_id, value)
        if not node_id:
            return ""
        node = await session.get(WorkflowNode, node_id)
        if not node or node.project_id != project_id:
            return ""
        output = _json_value(node.output_json)
        input_data = _json_value(node.input_json)
        return _media_reference_from_value(output) or _media_reference_from_value(input_data) or ""


class WorkflowPluginContext:
    def __init__(self, *, project_id: str, plugin: WorkflowPlugin, run_id: str):
        self.project_id = project_id
        self.plugin = plugin
        self.run_id = run_id
        self.logs: list[dict[str, str]] = []
        self.progress_events: list[dict[str, Any]] = []
        self.workspace = (
            _project_storage_root(project_id)
            / "generated_images"
            / "plugin_outputs"
            / plugin.id.replace(".", "_")
            / run_id
        ).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    def log(self, message: str, level: str = "info") -> None:
        self.logs.append({"level": level, "message": str(message)})

    def progress(self, value: float, message: str = "") -> None:
        self.progress_events.append({"value": max(0.0, min(1.0, float(value))), "message": str(message)})

    async def resolve_asset(self, value: Any) -> str:
        raw = value
        if isinstance(value, dict):
            for key in ("local_path", "path", "url", "local_url", "ref", "value"):
                if value.get(key):
                    raw = value[key]
                    break
        text = str(raw or "").strip()
        if not text:
            raise WorkflowPluginError("asset reference is empty")
        if _looks_like_node_reference(text):
            node_ref = await _resolve_canvas_node_media_reference(self.project_id, text)
            if not node_ref:
                raise WorkflowPluginError(f"canvas node has no media output: {text}")
            text = node_ref
        if text.startswith("http://") or text.startswith("https://"):
            return text
        if text.startswith("upload:"):
            text = text[len("upload:"):]
        path = _media_path_from_url(self.project_id, text)
        if path is None:
            if text.startswith(("generated_images/", "generated_videos/", "generated_audio/")):
                path = (_project_storage_root(self.project_id) / text).resolve()
            elif text.startswith("uploads/"):
                path = (_project_upload_root(self.project_id) / text[len("uploads/"):].lstrip("/")).resolve()
            else:
                candidate = Path(text).expanduser()
                path = candidate.resolve() if candidate.is_absolute() else (_project_storage_root(self.project_id) / text).resolve()
        allowed_roots = [
            _project_storage_root(self.project_id),
            _project_upload_root(self.project_id).parent,
        ]
        if not any(_is_within(path, root) for root in allowed_roots):
            raise WorkflowPluginError(f"asset path outside project storage: {text}")
        if not path.exists() or not path.is_file():
            raise WorkflowPluginError(f"asset file not found: {text}")
        return str(path)

    async def save_file(self, source: Any, *, kind: str = "file", suffix: str = "") -> dict[str, Any]:
        suffix = suffix or Path(str(source)).suffix if isinstance(source, (str, Path)) else suffix
        suffix = suffix if suffix.startswith(".") else f".{suffix}" if suffix else ".bin"
        target = self.workspace / f"{kind}-{uuid.uuid4().hex[:10]}{suffix}"
        if isinstance(source, bytes):
            target.write_bytes(source)
        elif isinstance(source, (str, Path)):
            src = Path(source).expanduser().resolve()
            if not src.exists() or not src.is_file():
                raise WorkflowPluginError(f"source file not found: {source}")
            shutil.copyfile(src, target)
        else:
            raise WorkflowPluginError("save_file source must be bytes or a file path")
        rel = target.relative_to(_project_storage_root(self.project_id)).as_posix()
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        local_url = f"/api/media/{self.project_id}/{rel}"
        return {
            "type": kind,
            "path": rel,
            "local_path": str(target),
            "url": local_url,
            "local_url": local_url,
            "mime_type": media_type,
            "size": target.stat().st_size,
        }

    async def save_image(self, image: Any, *, kind: str = "image") -> dict[str, Any]:
        target = self.workspace / f"{kind}-{uuid.uuid4().hex[:10]}.png"
        if isinstance(image, (str, Path)):
            src = Path(image).expanduser().resolve()
            if not src.exists() or not src.is_file():
                raise WorkflowPluginError(f"image file not found: {image}")
            shutil.copyfile(src, target)
        else:
            try:
                from PIL import Image
                import numpy as np
            except Exception as exc:  # pragma: no cover - dependency is installed in normal env
                raise WorkflowPluginError("Pillow/numpy is required for image outputs") from exc
            if isinstance(image, bytes):
                target.write_bytes(image)
            elif isinstance(image, Image.Image):
                image.save(target, format="PNG")
            elif isinstance(image, np.ndarray):
                if image.ndim == 3 and image.shape[2] == 3:
                    image = image[:, :, ::-1]
                Image.fromarray(image).save(target, format="PNG")
            else:
                raise WorkflowPluginError("save_image expects path, bytes, PIL image, or numpy array")
        rel = target.relative_to(_project_storage_root(self.project_id)).as_posix()
        local_url = f"/api/media/{self.project_id}/{rel}"
        payload: dict[str, Any] = {
            "type": "image",
            "path": rel,
            "local_path": str(target),
            "url": local_url,
            "local_url": local_url,
            "mime_type": "image/png",
        }
        try:
            from PIL import Image
            with Image.open(target) as img:
                payload["width"] = img.width
                payload["height"] = img.height
        except Exception:
            pass
        return payload

    async def save_video(self, source: Any, *, kind: str = "video") -> dict[str, Any]:
        video_workspace = (
            _project_storage_root(self.project_id)
            / "generated_videos"
            / "plugin_outputs"
            / self.plugin.id.replace(".", "_")
            / self.run_id
        ).resolve()
        video_workspace.mkdir(parents=True, exist_ok=True)
        suffix = Path(str(source)).suffix or ".mp4"
        target = video_workspace / f"{kind}-{uuid.uuid4().hex[:10]}{suffix}"
        if isinstance(source, bytes):
            target.write_bytes(source)
        elif isinstance(source, (str, Path)):
            src = Path(source).expanduser().resolve()
            if not src.exists() or not src.is_file():
                raise WorkflowPluginError(f"video file not found: {source}")
            shutil.copyfile(src, target)
        else:
            raise WorkflowPluginError("save_video source must be bytes or a file path")
        rel = target.relative_to(_project_storage_root(self.project_id)).as_posix()
        local_url = f"/api/media/{self.project_id}/{rel}"
        return {
            "type": "video",
            "path": rel,
            "local_path": str(target),
            "url": local_url,
            "local_url": local_url,
            "mime_type": mimetypes.guess_type(target.name)[0] or "video/mp4",
            "size": target.stat().st_size,
        }

    async def save_text(self, content: str, *, kind: str = "text") -> dict[str, Any]:
        target = self.workspace / f"{kind}-{uuid.uuid4().hex[:10]}.txt"
        target.write_text(str(content), encoding="utf-8")
        rel = target.relative_to(_project_storage_root(self.project_id)).as_posix()
        local_url = f"/api/media/{self.project_id}/{rel}"
        return {
            "type": "text",
            "path": rel,
            "local_path": str(target),
            "url": local_url,
            "local_url": local_url,
            "mime_type": "text/plain",
        }


def _load_python_entrypoint(plugin: WorkflowPlugin, entrypoint: str) -> Any:
    module_name, sep, function_name = entrypoint.partition(":")
    if not sep or not module_name or not function_name:
        raise WorkflowPluginError(f"Invalid python entrypoint for plugin {plugin.id}: {entrypoint!r}")
    module_path = (plugin.path / f"{module_name.replace('.', '/')}.py").resolve()
    if not _is_within(module_path, plugin.path) or not module_path.exists():
        raise WorkflowPluginError(f"Plugin entrypoint file not found: {module_path}")
    unique_name = f"openreel_plugin_{plugin.id.replace('.', '_')}_{module_name.replace('.', '_')}_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(unique_name, module_path)
    if spec is None or spec.loader is None:
        raise WorkflowPluginError(f"Cannot load plugin module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    old_path = list(sys.path)
    try:
        sys.path.insert(0, str(plugin.path))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = old_path
    func = getattr(module, function_name, None)
    if not callable(func):
        raise WorkflowPluginError(f"Plugin entrypoint function not found: {entrypoint}")
    return func


async def _call_python_plugin(
    *,
    plugin: WorkflowPlugin,
    node_type: dict[str, Any],
    context: WorkflowPluginContext,
    inputs: dict[str, Any],
    settings_payload: dict[str, Any],
) -> dict[str, Any]:
    entrypoint = str((node_type.get("runtime") or {}).get("entrypoint") or "").strip()
    if not entrypoint:
        raise WorkflowPluginError(f"Plugin node {node_type['id']} has no python entrypoint")
    func = _load_python_entrypoint(plugin, entrypoint)
    result = func(context, inputs, settings_payload)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, dict):
        raise WorkflowPluginError("Plugin runtime must return an object")
    return result


async def _call_http_plugin(
    *,
    plugin: WorkflowPlugin,
    node_type: dict[str, Any],
    context: WorkflowPluginContext,
    inputs: dict[str, Any],
    settings_payload: dict[str, Any],
    project_id: str,
    workflow_id: str,
    step_id: str,
) -> dict[str, Any]:
    endpoint = str((node_type.get("runtime") or {}).get("endpoint") or "").strip()
    if not endpoint:
        raise WorkflowPluginError(f"Plugin node {node_type['id']} has no http endpoint")
    payload = {
        "project_id": project_id,
        "workflow_id": workflow_id,
        "run_id": context.run_id,
        "node_id": step_id,
        "inputs": inputs,
        "settings": settings_payload,
        "assets": {},
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(endpoint, json=payload)
    if resp.status_code >= 400:
        raise WorkflowPluginError(f"Plugin HTTP runtime failed: {resp.status_code} {resp.text[:300]}")
    data = resp.json()
    if not isinstance(data, dict):
        raise WorkflowPluginError("Plugin HTTP runtime must return an object")
    return data


def _step_settings(step: dict[str, Any], workflow: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for value in (
        step.get("settings"),
        step.get("plugin_settings"),
        workflow.get("settings"),
        workflow.get("plugin_settings"),
        fields.get("settings"),
    ):
        if isinstance(value, dict):
            result.update(value)
    return result


async def run_plugin_step(
    *,
    project_id: str,
    template: dict[str, Any],
    step: dict[str, Any],
    record: dict[str, Any],
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fields = dict(record.get("input") if isinstance(record.get("input"), dict) else {})
    workflow = dict(fields.get("workflow") if isinstance(fields.get("workflow"), dict) else {})
    plugin, node_type = find_plugin_node(step, workflow)
    run_id = uuid.uuid4().hex[:12]
    ctx = WorkflowPluginContext(project_id=project_id, plugin=plugin, run_id=run_id)
    runtime_inputs = {
        "workflow_inputs": inputs or {},
        "fields": fields,
        "record": record,
        "step": step,
    }
    explicit_inputs = fields.get("plugin_inputs") or workflow.get("plugin_inputs") or step.get("plugin_inputs")
    if isinstance(explicit_inputs, dict):
        runtime_inputs.update(explicit_inputs)
    settings_payload = _step_settings(step, workflow, fields)
    kind = str((node_type.get("runtime") or {}).get("kind") or "").strip().lower()
    if kind == "http":
        result = await _call_http_plugin(
            plugin=plugin,
            node_type=node_type,
            context=ctx,
            inputs=runtime_inputs,
            settings_payload=settings_payload,
            project_id=project_id,
            workflow_id=str(template.get("id") or ""),
            step_id=str(step.get("id") or ""),
        )
    else:
        result = await _call_python_plugin(
            plugin=plugin,
            node_type=node_type,
            context=ctx,
            inputs=runtime_inputs,
            settings_payload=settings_payload,
        )
    output = dict(result)
    output.setdefault("status", "succeeded")
    output.setdefault("outputs", {})
    if ctx.logs:
        output["logs"] = [*(output.get("logs") or []), *ctx.logs] if isinstance(output.get("logs"), list) else ctx.logs
    if ctx.progress_events:
        output["progress"] = ctx.progress_events
    output["plugin"] = {
        "id": plugin.id,
        "name": plugin.name,
        "version": plugin.version,
        "node_type": node_type["type"],
        "run_id": run_id,
    }
    ok = str(output.get("status") or "").lower() not in {"failed", "error"} and output.get("ok") is not False
    return {
        "ok": ok,
        "runtime_step": True,
        "run_result": output,
        "error": output.get("error") if not ok else None,
        "error_kind": output.get("error_kind") if not ok else None,
    }


def builtin_node_types() -> list[dict[str, Any]]:
    return [
        {
            "id": "openreel.input",
            "type": "input",
            "kind": "input",
            "title": "流程输入",
            "category": "core",
            "description": "收集本次运行的主题、集数、段数、风格等输入。",
            "inputs": [],
            "outputs": [{"id": "values", "label": "输入值", "type": "object"}],
            "settings": [],
        },
        {
            "id": "openreel.llm_text",
            "type": "llm_text",
            "kind": "llm_text",
            "title": "生成文本",
            "category": "core",
            "description": "根据输入或上游内容生成、改写正文。",
            "inputs": [{"id": "context", "label": "上下文", "type": "object"}],
            "outputs": [{"id": "content", "label": "文本", "type": "text"}],
            "settings": [{"id": "model_tier", "label": "模型档位", "type": "select", "options": ["强", "平衡", "小模型"]}],
        },
        {
            "id": "openreel.llm_json",
            "type": "llm_json",
            "kind": "llm_json",
            "title": "分段拆分",
            "category": "core",
            "description": "按规则把正文拆成分段、镜头或其他结构化步骤。",
            "inputs": [{"id": "context", "label": "上下文", "type": "object"}],
            "outputs": [{"id": "json", "label": "结构化结果", "type": "object"}],
            "settings": [{"id": "model_tier", "label": "模型档位", "type": "select", "options": ["强", "平衡", "小模型"]}],
        },
        {
            "id": "openreel.collection",
            "type": "collection",
            "kind": "collection",
            "title": "提取集合",
            "category": "core",
            "description": "从输入或上游正文中提取人物、场景、段落等集合。",
            "inputs": [{"id": "context", "label": "上下文", "type": "object"}],
            "outputs": [{"id": "items", "label": "列表项", "type": "array"}],
            "settings": [{"id": "model_tier", "label": "模型档位", "type": "select", "options": ["强", "平衡", "小模型"]}],
        },
        {
            "id": "openreel.image",
            "type": "image",
            "kind": "image",
            "title": "图片节点",
            "category": "core",
            "description": "从上游读取图片提示词，并按本节点属性生成或承接画布图片。",
            "inputs": [{"id": "prompt", "label": "图片提示词", "type": "text"}],
            "outputs": [{"id": "image", "label": "图片", "type": "image"}],
            "settings": [
                {"id": "aspect_ratio", "label": "画幅", "type": "select", "options": ["9:16", "16:9", "1:1"]},
                {"id": "quality", "label": "质量", "type": "select", "options": ["标准", "高清"]},
            ],
        },
        {
            "id": "openreel.video",
            "type": "video",
            "kind": "video",
            "title": "视频节点",
            "category": "core",
            "description": "从上游读取视频提示词，并按本节点属性生成或承接画布视频。",
            "inputs": [{"id": "prompt", "label": "视频提示词", "type": "text"}],
            "outputs": [{"id": "video", "label": "视频", "type": "video"}],
            "settings": [{"id": "duration_seconds", "label": "时长", "type": "number"}],
        },
        {
            "id": "openreel.audio",
            "type": "audio",
            "kind": "audio",
            "title": "音频节点",
            "category": "core",
            "description": "从上游读取音频文本，并按本节点属性生成或承接画布音频。",
            "inputs": [{"id": "prompt", "label": "音频文本", "type": "text"}],
            "outputs": [{"id": "audio", "label": "音频", "type": "audio"}],
            "settings": [{"id": "duration_seconds", "label": "时长", "type": "number"}],
        },
        {
            "id": "openreel.review",
            "type": "review",
            "kind": "review",
            "title": "检查",
            "category": "core",
            "description": "检查上游结果，输出问题和修改建议。",
            "inputs": [{"id": "target", "label": "检查对象", "type": "object"}],
            "outputs": [{"id": "review", "label": "检查结果", "type": "text"}],
            "settings": [{"id": "model_tier", "label": "模型档位", "type": "select", "options": ["强", "平衡", "小模型"]}],
        },
    ]


def workflow_node_types() -> dict[str, Any]:
    plugins = list_plugins()
    nodes = [*builtin_node_types(), *plugin_node_types()]
    return {
        "ok": True,
        "node_types": nodes,
        "plugins": plugins,
        "errors": plugin_errors(),
        "total": len(nodes),
    }
