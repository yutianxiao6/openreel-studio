from app.config_store.schema import RuntimeConfig


def test_llm_provider_accepts_model_context_metadata() -> None:
    cfg = RuntimeConfig.model_validate(
        {
            "$schema_version": 1,
            "llm_providers": [
                {
                    "name": "deepseek",
                    "provider": "deepseek",
                    "model_name": "deepseek-chat",
                    "base_url": "https://example.test/v1",
                    "api_key": "${DEEPSEEK_API_KEY}",
                    "context_window_tokens": 128000,
                    "max_input_tokens": 120000,
                    "max_output_tokens": 8192,
                    "supports_prompt_cache": True,
                    "supports_vision": False,
                    "tokenizer": "provider",
                    "tier": "strong",
                    # Legacy global default flag is accepted then dropped.
                    "is_default": True,
                    "enabled": True,
                    "notes": "runtime configured",
                    "params": {"cache_min_input_tokens": 1024},
                }
            ],
            "media_providers": [],
            "model_tier_defaults": {"strong": "deepseek"},
            "model_assignments": {"agent_loop": "deepseek"},
            "app_settings": {},
        }
    )

    provider = cfg.llm_providers[0]
    assert provider.context_window_tokens == 128000
    assert provider.max_input_tokens == 120000
    assert provider.max_output_tokens == 8192
    assert provider.supports_prompt_cache is True
    assert provider.supports_vision is False
    assert provider.tokenizer == "provider"
    assert provider.tier == "strong"
    assert not hasattr(provider, "is_default")
    assert provider.params == {"cache_min_input_tokens": 1024}
    assert "is_default" not in cfg.model_dump()["llm_providers"][0]
    assert cfg.model_tier_defaults["strong"] == "deepseek"
    assert cfg.model_tier_defaults["balanced"] is None
    assert cfg.model_tier_defaults["small"] is None


def test_runtime_config_accepts_tier_defaults_without_task_names() -> None:
    cfg = RuntimeConfig.model_validate(
        {
            "$schema_version": 1,
            "llm_providers": [
                {
                    "name": "strong-provider",
                    "provider": "openai",
                    "model_name": "strong-model",
                    "tier": "strong",
                    # Legacy global default flag no longer affects routing.
                    "is_default": True,
                },
                {
                    "name": "small-provider",
                    "provider": "openai",
                    "model_name": "small-model",
                    "tier": "small",
                },
            ],
            "media_providers": [],
            "model_tier_defaults": {
                "strong": "strong-provider",
                "small": "small-provider",
            },
            "model_assignments": {},
            "app_settings": {},
        }
    )

    assert cfg.llm_providers[0].tier == "strong"
    assert cfg.llm_providers[1].tier == "small"
    assert cfg.model_tier_defaults == {
        "strong": "strong-provider",
        "balanced": None,
        "small": "small-provider",
    }
