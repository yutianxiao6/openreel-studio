"""Model target configuration for UMA-backed image providers.

The target catalog owns model identity, capabilities, defaults and UI metadata.
HTTP request and response contracts live only in ``uma.protocol/v2`` documents.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import settings


IMAGE_TARGET_CATALOG_VERSION = "openreel.uma_image_targets.v1"
_DEFAULT_CATALOG = Path("config") / "universal_model_adapter" / "image_targets" / "catalog.json"


def image_target_catalog_path() -> Path:
    override = os.getenv("OPENREEL_UMA_IMAGE_TARGETS_FILE", "").strip()
    if override:
        path = Path(override).expanduser()
        return (
            path.resolve()
            if path.is_absolute()
            else (Path(settings.PROJECT_ROOT).expanduser().resolve() / path)
        )
    return Path(settings.PROJECT_ROOT).expanduser().resolve() / _DEFAULT_CATALOG


@lru_cache(maxsize=4)
def _load_cached(path_text: str, mtime_ns: int, size: int) -> dict[str, Any]:
    del mtime_ns, size
    path = Path(path_text)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != IMAGE_TARGET_CATALOG_VERSION:
        raise ValueError(f"image target catalog must use version {IMAGE_TARGET_CATALOG_VERSION!r}")
    raw_targets = data.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("image target catalog must contain a non-empty targets list")
    seen: set[str] = set()
    targets: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_targets):
        if not isinstance(raw, dict):
            raise ValueError(f"image target #{index + 1} must be an object")
        item = deepcopy(raw)
        profile_id = str(item.get("id") or "").strip()
        protocol_id = str(item.get("protocol_id") or "").strip()
        model_match = str(item.get("match") or "").strip()
        operation = str(item.get("operation") or "").strip()
        capabilities = item.get("capabilities")
        if not profile_id or profile_id in seen:
            raise ValueError(f"image target #{index + 1} has a missing or duplicate id")
        if not protocol_id or not model_match:
            raise ValueError(f"image target {profile_id!r} requires protocol_id and match")
        if operation != "image.generate":
            raise ValueError(f"image target {profile_id!r} requires operation='image.generate'")
        if not isinstance(capabilities, dict):
            raise ValueError(f"image target {profile_id!r} requires capabilities")
        roles = item.get("accepted_media_roles") or []
        if not isinstance(roles, list) or any(not str(role).strip() for role in roles):
            raise ValueError(f"image target {profile_id!r} has invalid accepted_media_roles")
        seen.add(profile_id)
        targets.append(item)
    return {"version": data["version"], "targets": targets}


def load_image_target_catalog() -> dict[str, Any]:
    path = image_target_catalog_path()
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError(f"cannot read image target catalog {path}: {exc}") from exc
    return deepcopy(_load_cached(str(path), stat.st_mtime_ns, stat.st_size))


def _targets() -> list[dict[str, Any]]:
    return load_image_target_catalog()["targets"]


def resolve_image_target(
    *,
    protocol_id: str,
    model_name: str,
    profile_id: str | None = None,
) -> dict[str, Any] | None:
    targets = _targets()
    if profile_id:
        matched = next((item for item in targets if item["id"] == profile_id), None)
        if matched is None or (protocol_id and matched["protocol_id"] != protocol_id):
            return None
        return matched
    candidates = [item for item in targets if item["protocol_id"] == protocol_id]
    exact = next((item for item in candidates if item["match"] == model_name), None)
    return exact or next((item for item in candidates if item["match"] == "*"), None)


def compile_image_target_options(target: dict[str, Any]) -> dict[str, Any]:
    capabilities = deepcopy(target["capabilities"])
    maximum_outputs = int(capabilities.get("max_outputs") or 4)
    maximum_references = int(capabilities.get("max_reference_images") or 0)
    request_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "input": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "minLength": 1},
                    "mode": {
                        "enum": ["text_to_image", "reference_single", "reference_multiple"]
                    },
                },
                "required": ["prompt", "mode"],
            },
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "minimum": 1, "maximum": maximum_outputs}
                },
            },
            "media": {"type": "array", "maxItems": maximum_references},
        },
        "required": ["input", "parameters"],
    }
    return {
        "protocol_id": target["protocol_id"],
        "operation": target["operation"],
        "target_defaults": {"parameters": deepcopy(target.get("defaults") or {})},
        "request_schema": request_schema,
        "input_map": deepcopy(target.get("input_map") or {}),
        "parameter_map": deepcopy(target.get("parameter_map") or {}),
        "static_input": deepcopy(target.get("static_input") or {}),
        "static_parameters": deepcopy(target.get("static_parameters") or {}),
        "pass_extra_parameters": True,
        "accepted_media_roles": tuple(target.get("accepted_media_roles") or ()),
        "target_metadata": {
            "profile_id": target["id"],
            "label": target.get("label") or target["match"],
            "capabilities": capabilities,
        },
    }


def list_image_model_targets() -> dict[str, Any]:
    targets = _targets()
    protocols: dict[str, dict[str, Any]] = {}
    public_targets: list[dict[str, Any]] = []
    for target in targets:
        capabilities = deepcopy(target["capabilities"])
        public = {
            "id": target["id"],
            "protocol_id": target["protocol_id"],
            "model_match": target["match"],
            "label": target.get("label") or target["match"],
            "operation": target["operation"],
            "capabilities": capabilities,
        }
        public_targets.append(public)
        protocol = protocols.setdefault(
            target["protocol_id"],
            {
                "id": target["protocol_id"],
                "display_name": target.get("protocol_label") or target["protocol_id"],
                "targets": [],
                "model_profiles": [],
            },
        )
        protocol["targets"].append(public)
        protocol["model_profiles"].append(
            {
                "match": target["match"],
                "label": target.get("label") or target["match"],
                "target_profile_id": target["id"],
                "operation": target["operation"],
                **capabilities,
            }
        )
    return {
        "ok": True,
        "version": IMAGE_TARGET_CATALOG_VERSION,
        "protocols": list(protocols.values()),
        "targets": public_targets,
    }


def image_model_presets() -> dict[str, dict[str, Any]]:
    presets: dict[str, dict[str, Any]] = {}
    for target in _targets():
        defaults = deepcopy(target.get("defaults") or {})
        presets[str(target["match"])] = defaults
    return presets
