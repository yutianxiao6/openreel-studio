from types import SimpleNamespace

from app.agent.token_usage import (
    accumulate_usage,
    build_usage_monitor_payload,
    build_usage_snapshot,
    extract_usage_from_response,
    normalize_usage_totals,
    reset_context_peak_usage,
)


def test_extract_usage_from_openai_style_response() -> None:
    response = SimpleNamespace(
        model="gpt-4o",
        usage={
            "prompt_tokens": 1000,
            "completion_tokens": 120,
            "total_tokens": 1120,
            "prompt_tokens_details": {"cached_tokens": 250},
        },
    )

    usage = extract_usage_from_response(response)

    assert usage["prompt_tokens"] == 1000
    assert usage["completion_tokens"] == 120
    assert usage["total_tokens"] == 1120
    assert usage["cached_prompt_tokens"] == 250
    assert usage["cache_hit_rate"] == 0.25
    assert usage["cache_supported"] is True


def test_build_usage_snapshot_estimates_remaining_context() -> None:
    response = SimpleNamespace(
        model="deepseek/deepseek-chat",
        usage={"prompt_tokens": 1024, "completion_tokens": 256},
    )

    snapshot = build_usage_snapshot(
        response,
        messages=[{"role": "user", "content": "hello"}],
    )

    assert snapshot["total_tokens"] == 1280
    assert snapshot["context_limit_tokens"] == 50000
    assert snapshot["context_limit_source"] == "local_compaction_threshold"
    assert snapshot["context_remaining_tokens"] == 50000 - 1024
    assert snapshot["context_available_rate"] == round((50000 - 1024) / 50000, 4)
    assert snapshot["context_usage_scope"] == "latest_llm_call"
    assert snapshot["active_input_tokens_source"] == "provider_usage"
    assert snapshot["usage_scope"] == "latest_llm_call"
    assert snapshot["latest_call_tokens"]["total_tokens"] == 1280
    assert snapshot["latest_call_context"]["context_remaining_tokens"] == 50000 - 1024
    assert snapshot["latest_call_context"]["scope"] == "latest_llm_call"


def test_build_usage_snapshot_uses_runtime_configured_context_window() -> None:
    response = SimpleNamespace(
        model="deepseek-v4-pro",
        usage={"prompt_tokens": 1024, "completion_tokens": 256},
    )

    snapshot = build_usage_snapshot(
        response,
        messages=[{"role": "user", "content": "hello"}],
        model_metadata={
            "context_window_tokens": 1_000_000,
            "max_input_tokens": 900_000,
            "max_output_tokens": 8192,
            "supports_prompt_cache": True,
            "tokenizer": "provider",
        },
    )

    assert snapshot["context_limit_tokens"] == 1_000_000
    assert snapshot["context_limit_source"] == "runtime_config"
    assert snapshot["context_remaining_tokens"] == 1_000_000 - 1024
    assert snapshot["context_available_rate"] == round((1_000_000 - 1024) / 1_000_000, 4)
    assert snapshot["max_input_tokens"] == 900_000
    assert snapshot["max_output_tokens"] == 8192
    assert snapshot["tokenizer"] == "provider"
    assert snapshot["cache_supported"] is True
    assert snapshot["cache_supported_source"] == "runtime_config"


def test_build_usage_snapshot_estimate_includes_system_and_tools() -> None:
    response = SimpleNamespace(
        model="local-model",
        usage={"completion_tokens": 10},
    )

    without_extras = build_usage_snapshot(
        response,
        messages=[{"role": "user", "content": "hello"}],
    )
    with_extras = build_usage_snapshot(
        response,
        messages=[{"role": "user", "content": "hello"}],
        system="system prompt " * 50,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "node__list",
                    "parameters": {"type": "object", "properties": {"project_id": {"type": "string"}}},
                },
            }
        ],
    )

    assert with_extras["estimated_input_tokens"] > without_extras["estimated_input_tokens"]
    assert with_extras["active_input_tokens"] == with_extras["estimated_input_tokens"]
    assert with_extras["active_input_tokens_source"] == "local_estimate"


def test_accumulate_usage_updates_cache_hit_rate() -> None:
    total = normalize_usage_totals(None)
    total = accumulate_usage(total, {"prompt_tokens": 100, "total_tokens": 110, "cached_prompt_tokens": 40})
    total = accumulate_usage(total, {"prompt_tokens": 300, "total_tokens": 330, "cached_prompt_tokens": 60})

    assert total["llm_calls"] == 2
    assert total["prompt_tokens"] == 400
    assert total["total_tokens"] == 440
    assert total["cached_prompt_tokens"] == 100
    assert total["cache_hit_rate"] == 0.25
    assert total["cumulative_tokens"]["total_tokens"] == 440
    assert total["cumulative_tokens"]["cache_hit_rate"] == 0.25
    assert total["latest_call_tokens"]["total_tokens"] == 330
    assert total["latest_call_tokens"]["prompt_tokens"] == 300


def test_accumulate_usage_keeps_lowest_observed_context_remaining_rate() -> None:
    total = normalize_usage_totals(None)
    total = accumulate_usage(
        total,
        {
            "prompt_tokens": 1000,
            "total_tokens": 1100,
            "active_input_tokens": 50_000,
            "context_limit_tokens": 100_000,
            "context_remaining_tokens": 50_000,
            "context_used_rate": 0.5,
            "context_available_rate": 0.5,
            "context_limit_source": "model_heuristic",
            "active_input_tokens_source": "provider_usage",
            "model": "test-model",
        },
    )
    total = accumulate_usage(
        total,
        {
            "prompt_tokens": 100,
            "total_tokens": 120,
            "active_input_tokens": 10_000,
            "context_limit_tokens": 100_000,
            "context_remaining_tokens": 90_000,
            "context_used_rate": 0.1,
            "context_available_rate": 0.9,
            "context_limit_source": "model_heuristic",
        },
    )

    assert total["llm_calls"] == 2
    assert total["context_available_rate"] == 0.9
    assert total["context_peak_available_rate"] == 0.5
    assert total["context_peak_used_rate"] == 0.5
    assert total["context_peak_remaining_tokens"] == 50_000
    assert total["context_peak_active_input_tokens"] == 50_000
    assert total["context_peak_usage_scope"] == "session_context_peak"
    assert total["latest_call_context"]["context_remaining_tokens"] == 90_000
    assert total["context_peak"]["context_remaining_tokens"] == 50_000
    assert total["context_peak"]["context_available_rate"] == 0.5


def test_accumulate_usage_preserves_runtime_configured_context_window() -> None:
    total = normalize_usage_totals(None)
    total = accumulate_usage(
        total,
        {
            "model": "deepseek-v4-pro",
            "prompt_tokens": 10_000,
            "total_tokens": 10_500,
            "active_input_tokens": 10_000,
            "context_limit_tokens": 1_000_000,
            "context_limit_source": "runtime_config",
            "context_remaining_tokens": 990_000,
            "context_used_rate": 0.01,
            "context_available_rate": 0.99,
        },
    )

    assert total["context_limit_tokens"] == 1_000_000
    assert total["context_remaining_tokens"] == 990_000
    assert total["context_available_rate"] == 0.99
    assert total["context_peak_limit_tokens"] == 1_000_000
    assert total["context_peak_remaining_tokens"] == 990_000
    assert total["context_peak_available_rate"] == 0.99
    assert total["latest_call_context"]["context_limit_tokens"] == 1_000_000
    assert total["context_peak"]["context_limit_tokens"] == 1_000_000


def test_accumulate_usage_can_skip_context_peak_for_compaction_llm_call() -> None:
    total = normalize_usage_totals(None)
    total = accumulate_usage(
        total,
        {
            "prompt_tokens": 1000,
            "total_tokens": 1100,
            "active_input_tokens": 20_000,
            "context_limit_tokens": 100_000,
            "context_remaining_tokens": 80_000,
            "context_used_rate": 0.2,
            "context_available_rate": 0.8,
        },
    )
    total = accumulate_usage(
        total,
        {
            "prompt_tokens": 80_000,
            "total_tokens": 81_000,
            "active_input_tokens": 80_000,
            "context_limit_tokens": 100_000,
            "context_remaining_tokens": 20_000,
            "context_used_rate": 0.8,
            "context_available_rate": 0.2,
        },
        track_context_peak=False,
    )

    assert total["llm_calls"] == 2
    assert total["total_tokens"] == 82_100
    assert total["context_available_rate"] == 0.2
    assert total["context_peak_available_rate"] == 0.8
    assert total["context_peak_used_rate"] == 0.2
    assert total["latest_call_tokens"]["total_tokens"] == 81_000
    assert total["cumulative_tokens"]["total_tokens"] == 82_100


def test_reset_context_peak_usage_keeps_cumulative_token_totals() -> None:
    total = accumulate_usage(
        normalize_usage_totals(None),
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "total_tokens": 1200,
            "cached_prompt_tokens": 250,
            "active_input_tokens": 50_000,
            "context_limit_tokens": 100_000,
            "context_remaining_tokens": 50_000,
            "context_used_rate": 0.5,
            "context_available_rate": 0.5,
        },
    )

    reset = reset_context_peak_usage(total)

    assert reset["llm_calls"] == 1
    assert reset["prompt_tokens"] == 1000
    assert reset["completion_tokens"] == 200
    assert reset["total_tokens"] == 1200
    assert reset["cached_prompt_tokens"] == 250
    assert reset["cache_hit_rate"] == 0.25
    assert reset["context_available_rate"] == 0.5
    assert reset["context_used_rate"] == 0.5
    assert reset["cumulative_tokens"]["total_tokens"] == 1200
    assert reset["latest_call_context"]["context_remaining_tokens"] == 50_000
    assert "context_peak_available_rate" not in reset
    assert "context_peak_used_rate" not in reset
    assert "context_peak" not in reset


def test_usage_monitor_payload_names_latest_call_and_cumulative_totals_separately() -> None:
    first_usage = {
        "prompt_tokens": 1000,
        "total_tokens": 1100,
        "active_input_tokens": 50_000,
        "context_limit_tokens": 100_000,
        "context_remaining_tokens": 50_000,
        "context_used_rate": 0.5,
        "context_available_rate": 0.5,
    }
    latest_usage = {
        "prompt_tokens": 100,
        "total_tokens": 120,
        "active_input_tokens": 10_000,
        "context_limit_tokens": 100_000,
        "context_remaining_tokens": 90_000,
        "context_used_rate": 0.1,
        "context_available_rate": 0.9,
    }
    totals = accumulate_usage(normalize_usage_totals(None), first_usage)
    totals = accumulate_usage(totals, latest_usage)

    payload = build_usage_monitor_payload(latest_usage, totals, totals)

    assert payload["latest_call_tokens"]["total_tokens"] == 120
    assert payload["latest_call_context"]["context_remaining_tokens"] == 90_000
    assert payload["run_cumulative_tokens"]["total_tokens"] == 1220
    assert payload["session_cumulative_tokens"]["llm_calls"] == 2
    assert payload["run_context_peak"]["context_remaining_tokens"] == 50_000
