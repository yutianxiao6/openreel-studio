"""Model target configuration for UMA-backed audio providers.

The target catalog owns model identity, operation, capabilities, defaults and
UI metadata. HTTP paths, request bodies, polling and output extraction live in
``uma.protocol/v2`` documents under the shared UMA protocol catalog.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import settings


AUDIO_TARGET_CATALOG_VERSION = "openreel.uma_audio_targets.v1"
_DEFAULT_CATALOG = Path("config") / "universal_model_adapter" / "audio_targets" / "catalog.json"


def audio_target_catalog_path() -> Path:
    override = os.getenv("OPENREEL_UMA_AUDIO_TARGETS_FILE", "").strip()
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
    if not isinstance(data, dict) or data.get("version") != AUDIO_TARGET_CATALOG_VERSION:
        raise ValueError(f"audio target catalog must use version {AUDIO_TARGET_CATALOG_VERSION!r}")
    raw_targets = data.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("audio target catalog must contain a non-empty targets list")
    seen: set[str] = set()
    targets: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_targets):
        if not isinstance(raw, dict):
            raise ValueError(f"audio target #{index + 1} must be an object")
        item = deepcopy(raw)
        profile_id = str(item.get("id") or "").strip()
        protocol_id = str(item.get("protocol_id") or "").strip()
        model_match = str(item.get("match") or "").strip()
        operation = str(item.get("operation") or "").strip()
        capabilities = item.get("capabilities")
        if not profile_id or profile_id in seen:
            raise ValueError(f"audio target #{index + 1} has a missing or duplicate id")
        if not protocol_id or not model_match:
            raise ValueError(f"audio target {profile_id!r} requires protocol_id and match")
        if not operation.startswith("audio."):
            raise ValueError(f"audio target {profile_id!r} requires an audio.* operation")
        if not isinstance(capabilities, dict) or not str(capabilities.get("mode") or "").strip():
            raise ValueError(f"audio target {profile_id!r} requires capabilities.mode")
        seen.add(profile_id)
        targets.append(item)
    return {"version": data["version"], "targets": targets}


def load_audio_target_catalog() -> dict[str, Any]:
    path = audio_target_catalog_path()
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError(f"cannot read audio target catalog {path}: {exc}") from exc
    return deepcopy(_load_cached(str(path), stat.st_mtime_ns, stat.st_size))


def _targets() -> list[dict[str, Any]]:
    return load_audio_target_catalog()["targets"]


def resolve_audio_target(
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


def compile_audio_target_options(target: dict[str, Any]) -> dict[str, Any]:
    capabilities = deepcopy(target["capabilities"])
    mode = str(capabilities["mode"])
    operation = str(target["operation"])
    input_field = "text" if operation == "audio.speech" else "prompt"
    request_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "input": {
                "type": "object",
                "properties": {input_field: {"type": "string", "minLength": 1}},
                "required": [input_field],
            },
            "parameters": {"type": "object"},
        },
        "required": ["input"],
    }
    return {
        "protocol_id": target["protocol_id"],
        "operation": operation,
        "target_defaults": {
            "parameters": deepcopy(target.get("defaults") or {}),
        },
        "request_schema": request_schema,
        "input_map": deepcopy(target.get("input_map") or {}),
        "parameter_map": deepcopy(target.get("parameter_map") or {}),
        "static_input": deepcopy(target.get("static_input") or {}),
        "static_parameters": deepcopy(target.get("static_parameters") or {}),
        "pass_extra_parameters": True,
        "target_metadata": {
            "profile_id": target["id"],
            "label": target.get("label") or target["match"],
            "capabilities": capabilities,
            "audio_mode": mode,
        },
        **deepcopy(target.get("poll_policy") or {}),
    }


def list_audio_model_targets() -> dict[str, Any]:
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
        "version": AUDIO_TARGET_CATALOG_VERSION,
        "protocols": list(protocols.values()),
        "targets": public_targets,
    }
