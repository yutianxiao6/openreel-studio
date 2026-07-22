"""Model target configuration for UMA-backed video providers.

This catalog owns model identity, capabilities, defaults and UI metadata.  It
does not contain HTTP paths, request bodies, status fields or output parsing;
those live exclusively in ``uma.protocol/v2`` documents.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import settings


VIDEO_TARGET_CATALOG_VERSION = "openreel.uma_video_targets.v1"
_DEFAULT_CATALOG = Path("config") / "universal_model_adapter" / "video_targets" / "catalog.json"


def video_target_catalog_path() -> Path:
    override = os.getenv("OPENREEL_UMA_VIDEO_TARGETS_FILE", "").strip()
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
    if not isinstance(data, dict) or data.get("version") != VIDEO_TARGET_CATALOG_VERSION:
        raise ValueError(f"video target catalog must use version {VIDEO_TARGET_CATALOG_VERSION!r}")
    raw_targets = data.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("video target catalog must contain a non-empty targets list")
    seen: set[str] = set()
    targets: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_targets):
        if not isinstance(raw, dict):
            raise ValueError(f"video target #{index + 1} must be an object")
        item = deepcopy(raw)
        profile_id = str(item.get("id") or "").strip()
        protocol_id = str(item.get("protocol_id") or "").strip()
        model_match = str(item.get("match") or "").strip()
        capabilities = item.get("capabilities")
        modes = capabilities.get("modes") if isinstance(capabilities, dict) else None
        if not profile_id or profile_id in seen:
            raise ValueError(f"video target #{index + 1} has a missing or duplicate id")
        if not protocol_id or not model_match:
            raise ValueError(f"video target {profile_id!r} requires protocol_id and match")
        if not isinstance(capabilities, dict) or not isinstance(modes, dict) or not modes:
            raise ValueError(f"video target {profile_id!r} requires capabilities.modes")
        seen.add(profile_id)
        targets.append(item)
    return {"version": data["version"], "targets": targets}


def load_video_target_catalog() -> dict[str, Any]:
    path = video_target_catalog_path()
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError(f"cannot read video target catalog {path}: {exc}") from exc
    return deepcopy(_load_cached(str(path), stat.st_mtime_ns, stat.st_size))


def _targets() -> list[dict[str, Any]]:
    return load_video_target_catalog()["targets"]


def resolve_video_target(
    *,
    protocol_id: str,
    model_name: str,
    profile_id: str | None = None,
) -> dict[str, Any] | None:
    targets = _targets()
    if profile_id:
        matched = next((item for item in targets if item["id"] == profile_id), None)
        if matched is None:
            return None
        if protocol_id and matched["protocol_id"] != protocol_id:
            return None
        return matched
    candidates = [item for item in targets if item["protocol_id"] == protocol_id]
    exact = next((item for item in candidates if item["match"] == model_name), None)
    return exact or next((item for item in candidates if item["match"] == "*"), None)


def _positive_number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return value
    return None


def _duration_schema(rule: Any) -> dict[str, Any]:
    if not isinstance(rule, dict):
        return {"type": "number", "exclusiveMinimum": 0}
    minimum = _positive_number(rule.get("min"))
    maximum = _positive_number(rule.get("max"))
    allowed = [
        value
        for value in (rule.get("allowed_values") or [])
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    ranged: dict[str, Any] = {"type": "number"}
    if minimum is not None:
        ranged["minimum"] = minimum
    else:
        ranged["exclusiveMinimum"] = 0
    if maximum is not None:
        ranged["maximum"] = maximum
    if allowed and (minimum is not None or maximum is not None):
        return {"anyOf": [ranged, {"enum": allowed}]}
    if allowed:
        return {"enum": allowed}
    return ranged


def _parameter_schema(capabilities: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "duration_seconds": _duration_schema(capabilities.get("duration")),
    }
    ratios = [str(item) for item in capabilities.get("supported_ratios") or []]
    resolutions = [str(item).lower() for item in capabilities.get("supported_resolutions") or []]
    if ratios:
        properties["aspect_ratio"] = {"enum": ratios}
    if resolutions:
        properties["resolution"] = {"enum": resolutions}
    if capabilities.get("supports_native_audio") is True:
        properties["generate_audio"] = {"type": "boolean"}
    return {"type": "object", "properties": properties}


def _media_schema(mode: dict[str, Any]) -> dict[str, Any]:
    allowed_roles = [str(item) for item in mode.get("allowed_roles") or []]
    required_roles = [str(item) for item in mode.get("required_roles") or []]
    minimum = mode.get("min_total_media")
    if minimum is None:
        minimum = sum(int(mode.get(key) or 0) for key in ("min_images", "min_videos", "min_audios"))
    maximum = mode.get("max_total_media")
    if maximum is None and any(
        mode.get(key) is not None for key in ("max_images", "max_videos", "max_audios")
    ):
        maximum = sum(int(mode.get(key) or 0) for key in ("max_images", "max_videos", "max_audios"))
    schema: dict[str, Any] = {"type": "array"}
    if isinstance(minimum, (int, float)) and minimum >= 0:
        schema["minItems"] = int(minimum)
    if isinstance(maximum, (int, float)) and maximum >= 0:
        schema["maxItems"] = int(maximum)
    if allowed_roles:
        schema["items"] = {
            "type": "object",
            "properties": {"role": {"enum": allowed_roles}},
            "required": ["role"],
        }
    if required_roles:
        schema["allOf"] = [
            {
                "contains": {
                    "type": "object",
                    "properties": {"role": {"const": role}},
                    "required": ["role"],
                },
                "minContains": 1,
                "maxContains": 1,
            }
            for role in required_roles
        ]
    return schema


def _variant_config(mode: dict[str, Any]) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"media": _media_schema(mode)},
    }
    duration = mode.get("duration")
    if isinstance(duration, dict):
        schema["properties"]["parameters"] = {
            "type": "object",
            "properties": {"duration_seconds": _duration_schema(duration)},
        }
    return {
        "request_schema": schema,
        "metadata": {"capabilities": deepcopy(mode)},
    }


def compile_video_target_options(target: dict[str, Any]) -> dict[str, Any]:
    capabilities = deepcopy(target["capabilities"])
    modes = capabilities["modes"]
    default_mode = (
        "text_to_video"
        if "text_to_video" in modes
        else "first_frame"
        if "first_frame" in modes
        else next(iter(modes))
    )
    parameter_defaults: dict[str, Any] = {}
    default_ratio = capabilities.get("default_ratio")
    default_resolution = capabilities.get("default_resolution")
    if default_ratio:
        parameter_defaults["aspect_ratio"] = default_ratio
    if default_resolution:
        parameter_defaults["resolution"] = str(default_resolution).lower()
    if capabilities.get("supports_native_audio") is True and isinstance(
        capabilities.get("default_generate_audio"), bool
    ):
        parameter_defaults["generate_audio"] = capabilities["default_generate_audio"]
    request_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "input": {
                "type": "object",
                "properties": {"mode": {"enum": list(modes)}},
                "required": ["mode"],
            },
            "parameters": _parameter_schema(capabilities),
        },
    }
    for rule in capabilities.get("resolution_rules") or []:
        if not isinstance(rule, dict) or not rule.get("supported_resolutions"):
            continue
        threshold = _positive_number(rule.get("duration_gt"))
        if threshold is None:
            continue
        request_schema.setdefault("allOf", []).append(
            {
                "if": {
                    "properties": {
                        "parameters": {
                            "properties": {"duration_seconds": {"exclusiveMinimum": threshold}}
                        }
                    }
                },
                "then": {
                    "properties": {
                        "parameters": {
                            "properties": {
                                "resolution": {
                                    "enum": [
                                        str(item).lower() for item in rule["supported_resolutions"]
                                    ]
                                }
                            }
                        }
                    }
                },
            }
        )
    return {
        "protocol_id": target["protocol_id"],
        "operation": "video.generate",
        "target_defaults": {
            "input": {"mode": default_mode},
            "parameters": parameter_defaults,
        },
        "request_schema": request_schema,
        "variants": {name: _variant_config(mode) for name, mode in modes.items()},
        "accepted_media_roles": list(target.get("accepted_media_roles") or []),
        "pass_extra_parameters": True,
        "target_metadata": {
            "profile_id": target["id"],
            "label": target.get("label") or target["match"],
            "capabilities": capabilities,
        },
        **deepcopy(target.get("poll_policy") or {}),
    }


def list_video_model_targets() -> dict[str, Any]:
    targets = _targets()
    protocols: dict[str, dict[str, Any]] = {}
    public_targets: list[dict[str, Any]] = []
    for target in targets:
        public = {
            "id": target["id"],
            "protocol_id": target["protocol_id"],
            "model_match": target["match"],
            "label": target.get("label") or target["match"],
            "capabilities": deepcopy(target["capabilities"]),
            "additional_bases": deepcopy(target.get("additional_bases") or []),
        }
        public_targets.append(public)
        protocol = protocols.setdefault(
            target["protocol_id"],
            {
                "id": target["protocol_id"],
                "display_name": target["protocol_id"],
                "targets": [],
                "model_profiles": [],
                "additional_base_urls": [],
            },
        )
        protocol["targets"].append(public)
        protocol["model_profiles"].append(
            {
                "match": target["match"],
                "label": target.get("label") or target["match"],
                "target_profile_id": target["id"],
                **deepcopy(target["capabilities"]),
            }
        )
        known_slots = {str(item.get("slot") or "") for item in protocol["additional_base_urls"]}
        for item in target.get("additional_bases") or []:
            slot = str(item.get("slot") or "").strip()
            if not slot or slot in known_slots:
                continue
            protocol["additional_base_urls"].append(
                {
                    "param": str(item.get("runtime_param") or slot),
                    "slot": slot,
                    "label": item.get("label") or slot,
                    "hint": item.get("hint"),
                    "required": item.get("required") is True,
                }
            )
            known_slots.add(slot)
    return {
        "ok": True,
        "version": VIDEO_TARGET_CATALOG_VERSION,
        "protocols": list(protocols.values()),
        "targets": public_targets,
    }
