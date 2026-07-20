"""Dynamic, read-only contracts for Codex-authored creative nodes.

The contract is derived from the current runtime provider configuration and
protocol catalogs.  It never writes project state and never invents image
dimensions when the project/user has not supplied them.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.mcp_tools import node_universal


CONTRACT_VERSION = "openreel.node-contract.v1"
NODE_TYPES = {"text", "image", "video", "audio"}

_FIELD_TYPES: dict[str, dict[str, Any]] = {
    "title": {"type": "string"},
    "content": {"type": "string"},
    "description": {"type": "string"},
    "prompt": {"type": "string"},
    "aspect_ratio": {"type": "string", "pattern": r"^\d+(?:\.\d+)?:\d+(?:\.\d+)?$"},
    "resolution": {"type": "string"},
    "quality": {"type": "string"},
    "duration_seconds": {"type": "number", "exclusiveMinimum": 0},
    "production_path": {"type": "string"},
    "purpose": {"type": "string"},
    "model": {"type": "string", "description": "Configured provider name or model name."},
    "provider": {"type": "string", "description": "Configured provider name."},
    "video_mode": {"type": "string"},
    "mode": {"type": "string"},
    "references": {"type": "array"},
    "depends_on": {"type": "array"},
    "reference_images": {"type": "array"},
    "reference_videos": {"type": "array"},
    "reference_audios": {"type": "array"},
    "media_references": {"type": "array"},
}

_PROJECT_DEFAULT_CONTAINERS = (
    "media_defaults",
    "output_settings",
    "creation_settings",
    "workflow_input_values",
)
_DYNAMIC_DEFAULT_FIELDS = (
    "aspect_ratio",
    "resolution",
    "quality",
    "duration_seconds",
    "model",
    "provider",
    "video_mode",
)


def _filled(value: Any) -> bool:
    return value not in (None, "", [], {})


def _strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _nonnegative_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _canonical_video_mode(value: Any) -> str:
    mode = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "t2v": "text_to_video",
        "txt2video": "text_to_video",
        "text2video": "text_to_video",
        "i2v": "first_frame",
        "image_to_video": "first_frame",
        "source_image": "first_frame",
        "single_image": "first_frame",
        "first_last": "first_last_frame",
        "first_and_last_frame": "first_last_frame",
        "first_last_frames": "first_last_frame",
        "reference_to_video": "multimodal_reference",
        "reference_video": "multimodal_reference",
        "omni_reference": "multimodal_reference",
        "omni_reference_video": "multimodal_reference",
    }
    return aliases.get(mode, mode)


def _project_defaults(state: dict[str, Any], node_type: str) -> tuple[dict[str, Any], dict[str, str]]:
    defaults: dict[str, Any] = {}
    sources: dict[str, str] = {}
    containers: list[tuple[str, Any]] = [
        (name, state.get(name)) for name in _PROJECT_DEFAULT_CONTAINERS
    ]
    pending = state.get("pending_video_blueprint_request")
    if isinstance(pending, dict):
        containers.append(("pending_video_blueprint_request.collected_facts", pending.get("collected_facts")))

    for source_name, raw in containers:
        if not isinstance(raw, dict):
            continue
        scoped = raw.get(node_type)
        candidates = [raw]
        if isinstance(scoped, dict):
            candidates.append(scoped)
        for candidate in candidates:
            for key in _DYNAMIC_DEFAULT_FIELDS:
                value = candidate.get(key)
                if key not in defaults and _filled(value):
                    defaults[key] = deepcopy(value)
                    sources[key] = f"project_state.{source_name}"
    return defaults, sources


def _provider_for(
    config: dict[str, Any],
    node_type: str,
    fields: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, list[dict[str, Any]]]:
    if node_type == "text":
        return None, None, []
    providers = [
        item for item in config.get("media_providers", [])
        if isinstance(item, dict)
        and item.get("kind") == node_type
        and item.get("enabled") is not False
    ]
    requested = str(fields.get("model") or fields.get("provider") or "").strip()
    provider = None
    if requested:
        provider = next(
            (
                item for item in providers
                if requested in {str(item.get("name") or ""), str(item.get("model_name") or "")}
            ),
            None,
        )
    if provider is None and not requested:
        provider = next((item for item in providers if item.get("is_active") is True), None)
        provider = provider or (providers[0] if providers else None)
    summaries = [
        {
            "name": str(item.get("name") or ""),
            "model_name": str(item.get("model_name") or ""),
            "api_format": str(item.get("api_format") or ""),
            "is_active": item.get("is_active") is True,
        }
        for item in providers
    ]
    return provider, requested or None, summaries


def _protocol_id(provider: dict[str, Any] | None, node_type: str) -> str:
    if not provider:
        return ""
    params = provider.get("params") if isinstance(provider.get("params"), dict) else {}
    return str(
        params.get(f"{node_type}_protocol_id")
        or params.get("protocol_id")
        or params.get("protocol")
        or ""
    ).strip()


def _protocol_for(catalog: dict[str, Any], protocol_id: str) -> dict[str, Any] | None:
    protocols = catalog.get("protocols") if isinstance(catalog, dict) else None
    if not isinstance(protocols, list):
        return None
    if protocol_id:
        return next((item for item in protocols if isinstance(item, dict) and item.get("id") == protocol_id), None)
    return protocols[0] if len(protocols) == 1 and isinstance(protocols[0], dict) else None


def _profile_for(protocol: dict[str, Any] | None, model_name: str) -> dict[str, Any] | None:
    profiles = protocol.get("model_profiles") if isinstance(protocol, dict) else None
    if not isinstance(profiles, list):
        return None
    return next(
        (
            item for item in profiles
            if isinstance(item, dict) and str(item.get("match") or item.get("model") or "").strip() == model_name
        ),
        None,
    )


def _video_modes(protocol: dict[str, Any] | None, profile: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    raw = protocol.get("modes") if isinstance(protocol, dict) else None
    modes = {
        _canonical_video_mode(key): deepcopy(value)
        for key, value in (raw.items() if isinstance(raw, dict) else [])
        if isinstance(value, dict)
    }
    profile_modes = profile.get("modes") if isinstance(profile, dict) else None
    if isinstance(profile_modes, list):
        allowed = {_canonical_video_mode(item) for item in profile_modes}
        modes = {key: value for key, value in modes.items() if key in allowed}
    elif isinstance(profile_modes, dict):
        modes = {
            _canonical_video_mode(key): {**modes.get(_canonical_video_mode(key), {}), **value}
            for key, value in profile_modes.items()
            if isinstance(value, dict)
        }
    supported = _strings(profile.get("supported_modes")) if isinstance(profile, dict) else []
    if supported:
        allowed = {_canonical_video_mode(item) for item in supported}
        modes = {key: value for key, value in modes.items() if key in allowed}
    return modes


def _reference_counts(fields: dict[str, Any]) -> dict[str, int]:
    counts = {
        "images": len(fields.get("reference_images") or []) if isinstance(fields.get("reference_images"), list) else 0,
        "videos": len(fields.get("reference_videos") or []) if isinstance(fields.get("reference_videos"), list) else 0,
        "audios": len(fields.get("reference_audios") or []) if isinstance(fields.get("reference_audios"), list) else 0,
    }
    for key in ("references", "media_references"):
        values = fields.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                counts["images"] += 1
                continue
            role = str(item.get("role") or item.get("kind") or item.get("type") or "").lower()
            if "audio" in role:
                counts["audios"] += 1
            elif "video" in role:
                counts["videos"] += 1
            else:
                counts["images"] += 1
    counts["total"] = counts["images"] + counts["videos"] + counts["audios"]
    return counts


def _infer_video_mode(explicit: Any, modes: dict[str, dict[str, Any]], counts: dict[str, int]) -> str:
    wanted = _canonical_video_mode(explicit)
    if wanted and wanted in modes:
        return wanted
    if counts["total"] > 0:
        if modes.get("multimodal_reference") is not None:
            return "multimodal_reference"
        if counts["images"] >= 2 and modes.get("first_last_frame") is not None:
            return "first_last_frame"
        if counts["images"] > 0 and modes.get("first_frame") is not None:
            return "first_frame"
    if modes.get("text_to_video") is not None:
        return "text_to_video"
    return next(iter(modes), "")


def _first_nonempty(*values: Any) -> Any:
    return next((value for value in values if _filled(value)), None)


def _video_capabilities(
    provider: dict[str, Any],
    protocol: dict[str, Any] | None,
    profile: dict[str, Any] | None,
    mode_config: dict[str, Any] | None,
    modes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    params = provider.get("params") if isinstance(provider.get("params"), dict) else {}
    mode_config = mode_config or {}
    profile = profile or {}
    protocol = protocol or {}
    supported_ratios = _strings(
        _first_nonempty(
            params.get("supported_ratios"),
            params.get("ratios"),
            params.get("supported_aspect_ratios"),
            mode_config.get("supported_ratios"),
            profile.get("supported_ratios"),
            protocol.get("supported_ratios"),
        )
    )
    supported_ratios = [value for value in supported_ratios if value != "adaptive"]
    supported_resolutions = [
        value.lower() for value in _strings(
            _first_nonempty(
                params.get("supported_resolutions"),
                params.get("resolutions"),
                mode_config.get("supported_resolutions"),
                profile.get("supported_resolutions"),
                protocol.get("supported_resolutions"),
            )
        )
    ]
    duration: dict[str, Any] = {}
    for item in (protocol.get("duration"), profile.get("duration"), mode_config.get("duration")):
        if isinstance(item, dict):
            duration.update({key: value for key, value in item.items() if _filled(value)})
    param_duration = {
        "min": _first_nonempty(params.get("duration_min"), params.get("min_duration")),
        "max": _first_nonempty(params.get("duration_max"), params.get("max_duration")),
        "step": _first_nonempty(params.get("duration_step"), params.get("step_duration")),
        "allowed_values": _first_nonempty(
            params.get("supported_durations"),
            params.get("duration_values"),
            params.get("allowed_durations"),
        ),
    }
    duration.update({key: value for key, value in param_duration.items() if _filled(value)})
    default_ratio = _first_nonempty(
        params.get("default_ratio"),
        params.get("aspect_ratio"),
        mode_config.get("default_ratio"),
        profile.get("default_ratio"),
        protocol.get("default_ratio"),
    )
    default_resolution = _first_nonempty(
        params.get("default_resolution"),
        mode_config.get("default_resolution"),
        profile.get("default_resolution"),
        protocol.get("default_resolution"),
    )
    return {
        "supported_modes": list(modes),
        "supported_aspect_ratios": supported_ratios,
        "supported_resolutions": supported_resolutions,
        "default_aspect_ratio": default_ratio,
        "default_resolution": str(default_resolution or "").lower() or None,
        "duration": duration,
        "reference_limits": {
            key: mode_config.get(key)
            for key in (
                "min_images", "max_images", "min_videos", "max_videos",
                "min_audios", "max_audios", "min_total_media", "max_total_media",
            )
            if mode_config.get(key) is not None
        },
    }


def _field_error(field: str, code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"field": field, "code": code, "message": message, **details}


def _validate_video(
    fields: dict[str, Any],
    capabilities: dict[str, Any],
    mode_config: dict[str, Any] | None,
    counts: dict[str, int],
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    ratios = capabilities.get("supported_aspect_ratios") or []
    ratio = str(fields.get("aspect_ratio") or "").strip()
    if ratio and ratios and ratio not in ratios:
        errors.append(_field_error(
            "aspect_ratio", "unsupported_value", f"当前模型不支持画幅 {ratio}",
            actual=ratio, supported=ratios,
        ))
    resolutions = capabilities.get("supported_resolutions") or []
    resolution = str(fields.get("resolution") or "").strip().lower()
    if resolution and resolutions and resolution not in resolutions:
        errors.append(_field_error(
            "resolution", "unsupported_value", f"当前模型不支持分辨率 {resolution}",
            actual=resolution, supported=resolutions,
        ))
    duration = _number(fields.get("duration_seconds"))
    rule = capabilities.get("duration") if isinstance(capabilities.get("duration"), dict) else {}
    allowed = [_number(item) for item in (rule.get("allowed_values") or [])]
    allowed = [item for item in allowed if item is not None]
    if duration is not None:
        minimum = _number(rule.get("min"))
        maximum = _number(rule.get("max"))
        if allowed and duration not in allowed:
            errors.append(_field_error(
                "duration_seconds", "unsupported_value", f"当前模型不支持 {duration:g} 秒",
                actual=duration, supported=allowed,
            ))
        elif minimum is not None and duration < minimum:
            errors.append(_field_error(
                "duration_seconds", "below_minimum", f"时长不能少于 {minimum:g} 秒",
                actual=duration, minimum=minimum,
            ))
        elif maximum is not None and duration > maximum:
            errors.append(_field_error(
                "duration_seconds", "above_maximum", f"时长不能超过 {maximum:g} 秒",
                actual=duration, maximum=maximum,
            ))
    mode_config = mode_config or {}
    for plural, singular in (("images", "image"), ("videos", "video"), ("audios", "audio")):
        count = counts[plural]
        minimum = _nonnegative_number(mode_config.get(f"min_{plural}"))
        maximum = _nonnegative_number(mode_config.get(f"max_{plural}"))
        if minimum is not None and count < minimum:
            errors.append(_field_error(
                "references", "too_few_references", f"当前模式至少需要 {minimum:g} 个{singular}参考",
                kind=singular, actual=count, minimum=minimum,
            ))
        if maximum is not None and count > maximum:
            errors.append(_field_error(
                "references", "too_many_references", f"当前模式最多支持 {maximum:g} 个{singular}参考",
                kind=singular, actual=count, maximum=maximum,
            ))
    total_min = _nonnegative_number(mode_config.get("min_total_media"))
    total_max = _nonnegative_number(mode_config.get("max_total_media"))
    if total_min is not None and counts["total"] < total_min:
        errors.append(_field_error(
            "references", "too_few_references", f"当前模式至少需要 {total_min:g} 个媒体参考",
            actual=counts["total"], minimum=total_min,
        ))
    if total_max is not None and counts["total"] > total_max:
        errors.append(_field_error(
            "references", "too_many_references", f"当前模式最多支持 {total_max:g} 个媒体参考",
            actual=counts["total"], maximum=total_max,
        ))
    return errors


def build_node_contract(
    *,
    node_type: str,
    fields: dict[str, Any] | None,
    config: dict[str, Any] | None,
    project_state: dict[str, Any] | None = None,
    protocol_catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic contract and preflight result without side effects."""
    if node_type not in NODE_TYPES:
        return {
            "ok": False,
            "ready": False,
            "contract_version": CONTRACT_VERSION,
            "error_kind": "invalid_node_type",
            "error": f"type must be one of {sorted(NODE_TYPES)}",
        }
    explicit_fields = deepcopy(fields) if isinstance(fields, dict) else {}
    defaults, sources = _project_defaults(project_state or {}, node_type)
    normalized = {**defaults, **explicit_fields}
    field_sources = {**sources, **{key: "request.fields" for key in explicit_fields}}

    schema = deepcopy(node_universal._NODE_FIELD_SCHEMA.get(node_type, {}))
    required = list(schema.get("required") or [])
    optional = list(schema.get("optional") or [])
    allowed = list(dict.fromkeys([*required, *optional, "model", "provider"]))
    field_schema = {
        "type": "object",
        "additionalProperties": True,
        "properties": {key: deepcopy(_FIELD_TYPES.get(key, {})) for key in allowed},
        "required": required,
    }
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    provider, requested_provider, available_providers = _provider_for(config or {}, node_type, normalized)
    provider_summary = None
    protocol = None
    profile = None
    capabilities: dict[str, Any] = {}
    effective_mode = ""
    reference_counts = _reference_counts(normalized)
    if node_type != "text":
        if provider is None:
            errors.append(_field_error(
                "model", "provider_not_found" if requested_provider else "provider_missing",
                f"未找到媒体 provider: {requested_provider}" if requested_provider else f"没有可用的 {node_type} provider",
                requested=requested_provider,
                available=[item["name"] for item in available_providers],
            ))
        else:
            params = provider.get("params") if isinstance(provider.get("params"), dict) else {}
            protocol_id = _protocol_id(provider, node_type)
            protocol = _protocol_for(protocol_catalog or {}, protocol_id)
            profile = _profile_for(protocol, str(provider.get("model_name") or ""))
            provider_summary = {
                "name": str(provider.get("name") or ""),
                "model_name": str(provider.get("model_name") or ""),
                "api_format": str(provider.get("api_format") or ""),
                "protocol_id": protocol_id or None,
                "selection": "explicit" if requested_provider else "active_or_first_enabled",
            }
            if not _filled(normalized.get("model")):
                normalized["model"] = provider_summary["name"]
                field_sources["model"] = "runtime_config.active_provider"
            if protocol_id and protocol is None:
                errors.append(_field_error(
                    "model", "protocol_not_found", f"provider 引用的协议 {protocol_id} 不存在",
                    protocol_id=protocol_id,
                ))
            if node_type == "video":
                modes = _video_modes(protocol, profile)
                explicit_mode = normalized.get("video_mode") or normalized.get("mode")
                canonical_explicit = _canonical_video_mode(explicit_mode)
                effective_mode = _infer_video_mode(explicit_mode, modes, reference_counts)
                if canonical_explicit and modes and canonical_explicit not in modes:
                    errors.append(_field_error(
                        "video_mode", "unsupported_value", f"当前模型不支持视频模式 {canonical_explicit}",
                        actual=canonical_explicit, supported=list(modes),
                    ))
                elif effective_mode:
                    normalized["video_mode"] = effective_mode
                    field_sources["video_mode"] = "request.fields" if explicit_mode else "protocol.inference"
                mode_config = modes.get(effective_mode)
                capabilities = _video_capabilities(provider, protocol, profile, mode_config, modes)
                for field, default_key in (
                    ("aspect_ratio", "default_aspect_ratio"),
                    ("resolution", "default_resolution"),
                ):
                    default_value = capabilities.get(default_key)
                    if not _filled(normalized.get(field)) and _filled(default_value):
                        normalized[field] = default_value
                        field_sources[field] = f"provider_protocol.{default_key}"
                errors.extend(_validate_video(normalized, capabilities, mode_config, reference_counts))
            elif node_type == "image":
                capabilities = {
                    "supported_sizes": list(protocol.get("supported_sizes") or []) if protocol else [],
                }
            elif node_type == "audio":
                capabilities = {
                    "result_type": protocol.get("result_type") if protocol else None,
                }

    for field in required:
        if not _filled(normalized.get(field)):
            errors.append(_field_error(
                field, "missing_required_field", f"缺少必填字段 fields.{field}",
            ))

    if node_type == "image" and all(_filled(normalized.get(key)) for key in ("aspect_ratio", "resolution")):
        resolution_error = node_universal._validate_image_resolution_fields(normalized)
        if resolution_error is not None:
            errors.append(_field_error(
                "resolution",
                str(resolution_error.get("error_kind") or "invalid_resolution"),
                str(resolution_error.get("error") or "图片分辨率无效"),
                hint=resolution_error.get("hint"),
                resolution=normalized.get("resolution"),
                aspect_ratio=normalized.get("aspect_ratio"),
            ))

    unknown = sorted(key for key in explicit_fields if key not in allowed)
    if unknown:
        warnings.append({
            "code": "extension_fields_unverified",
            "fields": unknown,
            "message": "这些扩展字段未包含在通用节点合同中，将由具体 provider 在运行时校验。",
        })

    deduped_errors: list[dict[str, Any]] = []
    seen_errors: set[tuple[str, str]] = set()
    for error in errors:
        marker = (str(error.get("field") or ""), str(error.get("code") or ""))
        if marker not in seen_errors:
            seen_errors.add(marker)
            deduped_errors.append(error)
    return {
        "ok": True,
        "ready": not deduped_errors,
        "contract_version": CONTRACT_VERSION,
        "node_type": node_type,
        "description": schema.get("description") or "",
        "field_schema": field_schema,
        "required_fields": required,
        "optional_fields": optional,
        "normalized_fields": normalized,
        "field_sources": field_sources,
        "provider": provider_summary,
        "available_providers": available_providers,
        "capabilities": capabilities,
        "effective_video_mode": effective_mode or None,
        "reference_counts": reference_counts,
        "errors": deduped_errors,
        "warnings": warnings,
        "repair": {
            "action": "update_request_fields",
            "retry_same_node": True,
            "hint": "按 errors 修正字段；如果节点已经存在，更新原节点后重跑，不创建替代节点。",
        } if deduped_errors else None,
    }
