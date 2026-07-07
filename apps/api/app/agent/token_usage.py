"""Token and prompt-cache usage normalization for agent monitoring."""
from __future__ import annotations

import json
from typing import Any

from app.agent.context_compact import TOKEN_THRESHOLD, estimate_tokens


_TOTAL_KEYS = ("total_tokens", "total_token_count", "tokens")
_PROMPT_KEYS = ("prompt_tokens", "input_tokens", "input_token_count")
_COMPLETION_KEYS = ("completion_tokens", "output_tokens", "output_token_count")
_DETAIL_KEYS = ("prompt_tokens_details", "input_tokens_details")
_CACHED_KEYS = (
    "cached_tokens",
    "cache_read_tokens",
    "cache_read_input_tokens",
    "prompt_cache_hit_tokens",
    "cached_prompt_tokens",
)
_CACHE_READ_KEYS = (
    "cache_read_input_tokens",
    "cache_read_tokens",
    "prompt_cache_hit_tokens",
    "cached_prompt_tokens",
)
_CACHE_CREATE_KEYS = (
    "cache_creation_input_tokens",
    "cache_creation_tokens",
    "prompt_cache_miss_tokens",
    "prompt_cache_creation_tokens",
)

_LATEST_USAGE_KEYS = (
    "model",
    "usage_scope",
    "estimated_input_tokens",
    "active_input_tokens",
    "active_input_tokens_source",
    "context_limit_tokens",
    "context_limit_source",
    "context_remaining_tokens",
    "context_used_rate",
    "context_available_rate",
    "context_usage_scope",
    "cache_supported",
    "cache_supported_source",
    "max_input_tokens",
    "max_output_tokens",
    "tokenizer",
)

_LATEST_VIEW_KEYS = (
    "latest_call_tokens",
    "latest_call_context",
)

_CONTEXT_PEAK_KEYS = (
    "context_peak_active_input_tokens",
    "context_peak_active_input_tokens_source",
    "context_peak_limit_tokens",
    "context_peak_limit_source",
    "context_peak_remaining_tokens",
    "context_peak_used_rate",
    "context_peak_available_rate",
    "context_peak_model",
    "context_peak_usage_scope",
)

_TOTAL_VIEW_KEYS = (
    "cumulative_tokens",
    "context_peak",
)


def _as_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        data = value.model_dump()
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    data: dict[str, Any] = {}
    for key in (
        *_TOTAL_KEYS,
        *_PROMPT_KEYS,
        *_COMPLETION_KEYS,
        *_DETAIL_KEYS,
        *_CACHED_KEYS,
        *_CACHE_READ_KEYS,
        *_CACHE_CREATE_KEYS,
    ):
        if hasattr(value, key):
            data[key] = getattr(value, key)
    return data


def _first_int(mapping: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return max(0, value)
        if isinstance(value, float):
            return max(0, int(value))
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _max_int(*values: int | None) -> int:
    return max((value for value in values if isinstance(value, int)), default=0)


def _as_non_bool_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_non_negative_int(value: Any) -> int | None:
    number = _as_non_bool_float(value)
    if number is None:
        return None
    return max(0, int(number))


def _as_rate(value: Any) -> float | None:
    number = _as_non_bool_float(value)
    if number is None:
        return None
    return max(0.0, min(1.0, float(number)))


def _estimate_payload_tokens(payload: Any) -> int:
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(payload)
    return estimate_tokens([{"role": "user", "content": text}])


def _cache_hit_rate(cached_tokens: int, prompt_tokens: int | None) -> float | None:
    if prompt_tokens and prompt_tokens > 0:
        return round(cached_tokens / prompt_tokens, 4)
    return None


def _without_none(mapping: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in mapping.items() if value is not None}


def normalize_model_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("context_window_tokens", "max_input_tokens", "max_output_tokens"):
        value = _as_non_negative_int(metadata.get(key))
        if value and value > 0:
            out[key] = value
    for key in ("supports_prompt_cache", "supports_vision"):
        value = metadata.get(key)
        if isinstance(value, bool):
            out[key] = value
    tokenizer = metadata.get("tokenizer")
    if isinstance(tokenizer, str) and tokenizer.strip():
        out["tokenizer"] = tokenizer.strip()
    tier = metadata.get("tier")
    if isinstance(tier, str) and tier.strip():
        out["tier"] = tier.strip()
    params = metadata.get("params")
    if isinstance(params, dict) and params:
        out["params"] = dict(params)
    return out


def _context_window_from_metadata(metadata: dict[str, Any]) -> tuple[int, str]:
    context_window = _as_non_negative_int(metadata.get("context_window_tokens"))
    if context_window and context_window > 0:
        return context_window, "runtime_config"
    max_input = _as_non_negative_int(metadata.get("max_input_tokens"))
    if max_input and max_input > 0:
        return max_input, "runtime_config:max_input_tokens"
    return TOKEN_THRESHOLD, "local_compaction_threshold"


def latest_call_tokens_from_usage(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Return the token spend view for one LLM call.

    This is intentionally separate from context-window pressure: repeated calls
    can spend prompt tokens many times while each call still has a large context
    window remaining.
    """
    if not isinstance(usage, dict):
        return {}
    prompt_tokens = _as_non_negative_int(usage.get("prompt_tokens"))
    completion_tokens = _as_non_negative_int(usage.get("completion_tokens"))
    total_tokens = _as_non_negative_int(usage.get("total_tokens"))
    if total_tokens is None:
        parts = [value for value in (prompt_tokens, completion_tokens) if value is not None]
        total_tokens = sum(parts) if parts else None
    cached_prompt_tokens = _as_non_negative_int(usage.get("cached_prompt_tokens"))
    cache_read_tokens = _as_non_negative_int(usage.get("cache_read_tokens"))
    cache_creation_tokens = _as_non_negative_int(usage.get("cache_creation_tokens"))
    cache_hit_rate = _as_rate(usage.get("cache_hit_rate"))
    if cache_hit_rate is None and cached_prompt_tokens is not None:
        cache_hit_rate = _cache_hit_rate(cached_prompt_tokens, prompt_tokens)

    result = _without_none({
        "scope": str(usage.get("usage_scope") or "latest_llm_call"),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_prompt_tokens": cached_prompt_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_hit_rate": cache_hit_rate,
    })
    cache_supported = usage.get("cache_supported")
    if isinstance(cache_supported, bool):
        result["cache_supported"] = cache_supported
    elif cached_prompt_tokens or cache_read_tokens or cache_creation_tokens:
        result["cache_supported"] = True
    if any(key in result for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_prompt_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "cache_hit_rate",
    )):
        return result
    nested = usage.get("latest_call_tokens")
    return dict(nested) if isinstance(nested, dict) else {}


def latest_call_context_from_usage(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Return the context-capacity view for one LLM call."""
    if not isinstance(usage, dict):
        return {}
    active_input_tokens = _as_non_negative_int(usage.get("active_input_tokens"))
    context_limit_tokens = _as_non_negative_int(usage.get("context_limit_tokens"))
    context_remaining_tokens = _as_non_negative_int(usage.get("context_remaining_tokens"))
    estimated_input_tokens = _as_non_negative_int(usage.get("estimated_input_tokens"))
    context_used_rate = _as_rate(usage.get("context_used_rate"))
    context_available_rate = _as_rate(usage.get("context_available_rate"))
    if context_remaining_tokens is None and context_limit_tokens is not None and active_input_tokens is not None:
        context_remaining_tokens = max(0, context_limit_tokens - active_input_tokens)
    if context_used_rate is None and context_limit_tokens and active_input_tokens is not None:
        context_used_rate = round(active_input_tokens / context_limit_tokens, 4)
    if context_available_rate is None:
        if context_limit_tokens and context_remaining_tokens is not None:
            context_available_rate = round(context_remaining_tokens / context_limit_tokens, 4)
        elif context_used_rate is not None:
            context_available_rate = max(0.0, 1.0 - context_used_rate)

    result = _without_none({
        "scope": str(usage.get("context_usage_scope") or "latest_llm_call"),
        "model": usage.get("model"),
        "estimated_input_tokens": estimated_input_tokens,
        "active_input_tokens": active_input_tokens,
        "active_input_tokens_source": usage.get("active_input_tokens_source"),
        "context_limit_tokens": context_limit_tokens,
        "context_limit_source": usage.get("context_limit_source"),
        "context_remaining_tokens": context_remaining_tokens,
        "context_used_rate": context_used_rate,
        "context_available_rate": context_available_rate,
    })
    if any(key in result for key in (
        "estimated_input_tokens",
        "active_input_tokens",
        "context_limit_tokens",
        "context_remaining_tokens",
        "context_used_rate",
        "context_available_rate",
    )):
        return result
    nested = usage.get("latest_call_context")
    return dict(nested) if isinstance(nested, dict) else {}


def cumulative_tokens_from_totals(total: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(total, dict):
        return {}
    prompt_tokens = _as_non_negative_int(total.get("prompt_tokens")) or 0
    cached_prompt_tokens = _as_non_negative_int(total.get("cached_prompt_tokens")) or 0
    cache_hit_rate = _as_rate(total.get("cache_hit_rate"))
    if cache_hit_rate is None:
        cache_hit_rate = _cache_hit_rate(cached_prompt_tokens, prompt_tokens)
    return {
        "scope": "cumulative_total",
        "llm_calls": _as_non_negative_int(total.get("llm_calls")) or 0,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": _as_non_negative_int(total.get("completion_tokens")) or 0,
        "total_tokens": _as_non_negative_int(total.get("total_tokens")) or 0,
        "cached_prompt_tokens": cached_prompt_tokens,
        "cache_read_tokens": _as_non_negative_int(total.get("cache_read_tokens")) or 0,
        "cache_creation_tokens": _as_non_negative_int(total.get("cache_creation_tokens")) or 0,
        "cache_hit_rate": cache_hit_rate,
    }


def context_peak_from_totals(total: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(total, dict):
        return {}
    result = _without_none({
        "scope": total.get("context_peak_usage_scope") or "session_context_peak",
        "model": total.get("context_peak_model"),
        "active_input_tokens": _as_non_negative_int(total.get("context_peak_active_input_tokens")),
        "active_input_tokens_source": total.get("context_peak_active_input_tokens_source"),
        "context_limit_tokens": _as_non_negative_int(total.get("context_peak_limit_tokens")),
        "context_limit_source": total.get("context_peak_limit_source"),
        "context_remaining_tokens": _as_non_negative_int(total.get("context_peak_remaining_tokens")),
        "context_used_rate": _as_rate(total.get("context_peak_used_rate")),
        "context_available_rate": _as_rate(total.get("context_peak_available_rate")),
    })
    if any(key in result for key in (
        "active_input_tokens",
        "context_limit_tokens",
        "context_remaining_tokens",
        "context_used_rate",
        "context_available_rate",
    )):
        return result
    nested = total.get("context_peak")
    return dict(nested) if isinstance(nested, dict) else {}


def _attach_usage_views(usage: dict[str, Any]) -> dict[str, Any]:
    next_usage = dict(usage)
    latest_call_tokens = latest_call_tokens_from_usage(next_usage)
    latest_call_context = latest_call_context_from_usage(next_usage)
    if latest_call_tokens:
        next_usage["latest_call_tokens"] = latest_call_tokens
    if latest_call_context:
        next_usage["latest_call_context"] = latest_call_context
    return next_usage


def _attach_total_views(total: dict[str, Any]) -> dict[str, Any]:
    next_total = dict(total)
    next_total["cumulative_tokens"] = cumulative_tokens_from_totals(next_total)
    nested_latest_call_tokens = next_total.get("latest_call_tokens")
    latest_call_tokens = (
        dict(nested_latest_call_tokens)
        if isinstance(nested_latest_call_tokens, dict)
        else {}
    )
    latest_call_context = latest_call_context_from_usage(next_total)
    context_peak = context_peak_from_totals(next_total)
    if latest_call_tokens:
        next_total["latest_call_tokens"] = latest_call_tokens
    if latest_call_context:
        next_total["latest_call_context"] = latest_call_context
    if context_peak:
        next_total["context_peak"] = context_peak
    return next_total


def infer_context_window_tokens(model: str | None) -> tuple[int, str]:
    """Compatibility fallback for callers that have no runtime model metadata."""
    return TOKEN_THRESHOLD, "local_compaction_threshold"


def _with_current_context_window(usage: dict[str, Any]) -> dict[str, Any]:
    model = usage.get("model")
    if not isinstance(model, str) or not model.strip():
        return usage
    limit_source = str(usage.get("context_limit_source") or "")
    if limit_source not in {"", "local_compaction_threshold"}:
        return usage
    inferred_limit, inferred_source = infer_context_window_tokens(model)
    stored_limit = _as_non_negative_int(usage.get("context_limit_tokens"))
    if stored_limit == inferred_limit:
        return usage

    active_input_tokens = _as_non_negative_int(usage.get("active_input_tokens"))
    if active_input_tokens is None:
        active_input_tokens = _as_non_negative_int(usage.get("prompt_tokens"))
    if active_input_tokens is None:
        return usage

    next_usage = dict(usage)
    remaining = max(0, inferred_limit - active_input_tokens)
    next_usage["context_limit_tokens"] = inferred_limit
    next_usage["context_limit_source"] = inferred_source
    next_usage["context_remaining_tokens"] = remaining
    next_usage["context_used_rate"] = round(active_input_tokens / inferred_limit, 4) if inferred_limit else None
    next_usage["context_available_rate"] = round(remaining / inferred_limit, 4) if inferred_limit else None
    return next_usage


def extract_usage_from_response(response: Any) -> dict[str, Any]:
    usage = _as_mapping(getattr(response, "usage", None))
    if not usage and isinstance(response, dict):
        usage = _as_mapping(response.get("usage"))

    details: dict[str, Any] = {}
    for key in _DETAIL_KEYS:
        details.update(_as_mapping(usage.get(key)))

    prompt_tokens = _first_int(usage, _PROMPT_KEYS)
    completion_tokens = _first_int(usage, _COMPLETION_KEYS)
    total_tokens = _first_int(usage, _TOTAL_KEYS)
    if total_tokens is None:
        known_parts = [value for value in (prompt_tokens, completion_tokens) if isinstance(value, int)]
        total_tokens = sum(known_parts) if known_parts else None

    cache_read_tokens = _max_int(
        _first_int(details, _CACHED_KEYS),
        _first_int(usage, _CACHE_READ_KEYS),
    )
    cache_creation_tokens = _first_int(usage, _CACHE_CREATE_KEYS) or 0
    cached_prompt_tokens = cache_read_tokens

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_prompt_tokens": cached_prompt_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_hit_rate": _cache_hit_rate(cached_prompt_tokens, prompt_tokens),
        "cache_supported": cached_prompt_tokens > 0 or cache_creation_tokens > 0,
        "raw_usage_keys": sorted(str(key) for key in usage.keys()),
    }


def build_usage_snapshot(
    response: Any,
    *,
    messages: list[dict[str, Any]],
    system: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
    model_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    usage = extract_usage_from_response(response)
    if model_metadata is None:
        model_metadata = getattr(response, "_openreel_model_metadata", None)
        if model_metadata is None and isinstance(response, dict):
            model_metadata = response.get("_openreel_model_metadata")
    metadata = normalize_model_metadata(model_metadata)
    response_model = (
        str(getattr(response, "_openreel_actual_model", "") or "")
        or str(getattr(response, "model", "") or "")
        or (response.get("model") if isinstance(response, dict) else "")
        or model
        or ""
    )
    estimate_messages = list(messages)
    if system:
        estimate_messages = [{"role": "system", "content": system}, *estimate_messages]
    estimated_input_tokens = estimate_tokens(estimate_messages)
    if tools:
        estimated_input_tokens += _estimate_payload_tokens(tools)
    prompt_tokens = usage.get("prompt_tokens")
    active_input_tokens = prompt_tokens if isinstance(prompt_tokens, int) and prompt_tokens > 0 else estimated_input_tokens
    context_limit, context_limit_source = _context_window_from_metadata(metadata)
    context_remaining = max(0, context_limit - active_input_tokens)
    context_used_rate = round(active_input_tokens / context_limit, 4) if context_limit else None
    context_available_rate = round(context_remaining / context_limit, 4) if context_limit else None
    configured_cache_support = metadata.get("supports_prompt_cache")
    if isinstance(configured_cache_support, bool):
        usage["cache_supported"] = configured_cache_support
        usage["cache_supported_source"] = "runtime_config"
    usage.update(
        {
            "model": response_model or None,
            "requested_model": getattr(response, "_openreel_requested_model", None),
            "fallback_used": getattr(response, "_openreel_fallback_used", None),
            "model_tier": metadata.get("tier"),
            "usage_scope": "latest_llm_call",
            "estimated_input_tokens": estimated_input_tokens,
            "active_input_tokens": active_input_tokens,
            "active_input_tokens_source": "provider_usage" if prompt_tokens else "local_estimate",
            "context_limit_tokens": context_limit,
            "context_limit_source": context_limit_source,
            "context_remaining_tokens": context_remaining,
            "context_used_rate": context_used_rate,
            "context_available_rate": context_available_rate,
            "context_usage_scope": "latest_llm_call",
            "max_input_tokens": metadata.get("max_input_tokens"),
            "max_output_tokens": metadata.get("max_output_tokens"),
            "tokenizer": metadata.get("tokenizer"),
        }
    )
    return _attach_usage_views(usage)


def empty_usage_totals() -> dict[str, Any]:
    return {
        "llm_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_prompt_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_hit_rate": None,
    }


def _apply_context_peak(total: dict[str, Any], usage: dict[str, Any]) -> None:
    used_rate = _as_rate(usage.get("context_used_rate"))
    available_rate = _as_rate(usage.get("context_available_rate"))
    if used_rate is None and available_rate is not None:
        used_rate = max(0.0, 1.0 - available_rate)
    if used_rate is None:
        return

    current_peak_used = _as_rate(total.get("context_peak_used_rate"))
    active_input_tokens = _as_non_negative_int(usage.get("active_input_tokens"))
    context_limit_tokens = _as_non_negative_int(usage.get("context_limit_tokens"))
    current_peak_limit = _as_non_negative_int(total.get("context_peak_limit_tokens"))
    if (
        current_peak_used is not None
        and used_rate <= current_peak_used
        and context_limit_tokens == current_peak_limit
    ):
        return

    context_remaining_tokens = _as_non_negative_int(usage.get("context_remaining_tokens"))
    if (
        context_remaining_tokens is None
        and context_limit_tokens is not None
        and active_input_tokens is not None
    ):
        context_remaining_tokens = max(0, context_limit_tokens - active_input_tokens)
    if available_rate is None:
        available_rate = max(0.0, 1.0 - used_rate)

    total["context_peak_used_rate"] = round(used_rate, 4)
    total["context_peak_available_rate"] = round(available_rate, 4)
    total["context_peak_usage_scope"] = "session_context_peak"
    if active_input_tokens is not None:
        total["context_peak_active_input_tokens"] = active_input_tokens
    if context_limit_tokens is not None:
        total["context_peak_limit_tokens"] = context_limit_tokens
    if context_remaining_tokens is not None:
        total["context_peak_remaining_tokens"] = context_remaining_tokens
    for source_key, target_key in (
        ("active_input_tokens_source", "context_peak_active_input_tokens_source"),
        ("context_limit_source", "context_peak_limit_source"),
        ("model", "context_peak_model"),
    ):
        value = usage.get(source_key)
        if value is not None:
            total[target_key] = value


def normalize_usage_totals(value: Any) -> dict[str, Any]:
    totals = empty_usage_totals()
    if not isinstance(value, dict):
        return totals
    for key in totals:
        if key == "cache_hit_rate":
            continue
        item = value.get(key)
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            totals[key] = max(0, int(item))
    totals["cache_hit_rate"] = _cache_hit_rate(
        int(totals["cached_prompt_tokens"]),
        int(totals["prompt_tokens"]),
    )
    for key in (*_LATEST_USAGE_KEYS, *_LATEST_VIEW_KEYS, *_CONTEXT_PEAK_KEYS, *_TOTAL_VIEW_KEYS):
        item = value.get(key)
        if item is not None:
            totals[key] = item
    return _attach_total_views(totals)


def accumulate_usage(
    total: dict[str, Any],
    usage: dict[str, Any],
    *,
    track_context_peak: bool = True,
) -> dict[str, Any]:
    usage = _attach_usage_views(_with_current_context_window(usage))
    next_total = normalize_usage_totals(total)
    next_total["llm_calls"] = int(next_total["llm_calls"]) + 1
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_prompt_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            next_total[key] = int(next_total[key]) + max(0, int(value))
    next_total["cache_hit_rate"] = _cache_hit_rate(
        int(next_total["cached_prompt_tokens"]),
        int(next_total["prompt_tokens"]),
    )
    for key in _LATEST_USAGE_KEYS:
        value = usage.get(key)
        if value is not None:
            next_total[key] = value
    for key in _LATEST_VIEW_KEYS:
        value = usage.get(key)
        if value:
            next_total[key] = value
    if track_context_peak:
        _apply_context_peak(next_total, usage)
    return _attach_total_views(next_total)


def reset_context_peak_usage(total: dict[str, Any]) -> dict[str, Any]:
    """Keep cumulative token totals but restart context-window pressure tracking."""
    next_total = normalize_usage_totals(total)
    for key in _CONTEXT_PEAK_KEYS:
        next_total.pop(key, None)
    next_total.pop("context_peak", None)
    return _attach_total_views(next_total)


def normalize_usage_snapshot(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {}
    return _attach_usage_views(_with_current_context_window(dict(usage)))


def build_usage_monitor_payload(
    usage: dict[str, Any],
    run_totals: dict[str, Any],
    session_totals: dict[str, Any],
) -> dict[str, Any]:
    """Build the shared trace/SSE payload for usage monitoring."""
    normalized_usage = normalize_usage_snapshot(usage)
    normalized_run_totals = normalize_usage_totals(run_totals)
    normalized_session_totals = normalize_usage_totals(session_totals)
    return {
        "usage": normalized_usage,
        "run_totals": normalized_run_totals,
        "session_totals": normalized_session_totals,
        "latest_call_tokens": latest_call_tokens_from_usage(normalized_usage) or None,
        "latest_call_context": latest_call_context_from_usage(normalized_usage) or None,
        "run_cumulative_tokens": cumulative_tokens_from_totals(normalized_run_totals),
        "session_cumulative_tokens": cumulative_tokens_from_totals(normalized_session_totals),
        "run_context_peak": context_peak_from_totals(normalized_run_totals) or None,
        "session_context_peak": context_peak_from_totals(normalized_session_totals) or None,
    }
