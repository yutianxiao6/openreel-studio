# Model providers

English · [简体中文](../zh-CN/model-providers.md) · [Documentation home](../README.en.md)

## Configuration layers

OpenReel Studio separates account configuration from HTTP protocol definitions:

1. `config/runtime.jsonc` stores provider names, base URLs, API keys, model names, enabled state, and protocol IDs.
2. `config/*_provider_protocols/catalog.json` stores request paths, field mapping, polling, and result extraction.

One protocol can therefore serve several accounts or relays without embedding a private protocol object in every provider entry.

## LLM providers

An LLM entry commonly includes:

- LiteLLM provider;
- model name;
- API key;
- optional base URL;
- context window and maximum output;
- strong, balanced, and small tiers or task assignments.

Agent, review, compaction, and auxiliary tasks may use different models. Test the configuration in Settings before running a full workflow.

## Media providers

Image, video, and audio services use declarative formats:

- `image_http_v1`
- `video_http_v1`
- `audio_http_v1`

Runtime entries store only `image_protocol_id`, `video_protocol_id`, or `audio_protocol_id`. Protocol bodies belong in the catalog files and are not embedded in individual provider configuration.

## Base URL contract

The configured base URL is treated literally:

```text
Base URL: https://relay.example/v1
Protocol path: /videos
Final endpoint: https://relay.example/v1/videos
```

If a provider requires `/v1`, `/v2`, or `/api/v3`, keep that version in the base URL. Protocol paths contain resource paths such as `/files`, `/videos`, or `/generations`, without repeating the version:

```text
Wrong:   https://relay.example/v1 + /v1/videos
Correct: https://relay.example/v1 + /videos
```

The backend does not strip user-provided base URL suffixes. When a protocol requires a separate upload origin, configure an explicit parameter such as `upload_base_url` as declared by that protocol.

## Protocol catalogs

```text
config/
  image_provider_protocols/catalog.json
  video_provider_protocols/catalog.json
  audio_provider_protocols/catalog.json
```

Each catalog contains a version and a `protocols` map. Common fields include:

| Field | Purpose |
| --- | --- |
| `id` | Stable protocol identifier. |
| `request` | Submission method, path, payload, and response extraction. |
| `upload` | Optional upload phase. |
| `poll` | Optional asynchronous polling phase. |
| `defaults` | Protocol defaults. |
| `capabilities` | Aspect ratios, durations, resolutions, frame inputs, and related features. |

Supported ratios, durations, and resolutions come from configuration. When video duration metadata is absent, the product uses a 5–15 second default range rather than inventing per-model values.

## Add a provider

1. Select the media type in Settings.
2. Enter a name, base URL, API key, and model name.
3. Select the protocol ID that matches the provider API.
4. Enter any protocol-specific parameters.
5. Save and run the connection test.
6. Perform one real generation with a minimal prompt.

Do not infer a protocol from the model name alone. Compare the provider documentation for submission paths, authentication, payloads, asynchronous status, and output URLs.

## Troubleshooting order

1. Confirm that the provider is enabled.
2. Check whether the base URL includes the required API version.
3. Confirm that the protocol path does not duplicate that version.
4. Verify the model name and protocol ID.
5. Verify that the API key belongs to the configured relay.
6. Inspect the endpoint, HTTP status, and response summary in the node error.
7. For asynchronous video, inspect the job ID, poll endpoint, success states, and result extraction.

`bad response body` usually means that the provider response does not match the protocol extraction rule. Fix the protocol and retry; the node should retain its latest successful preview while the attempt fails.

## Security

- Store API keys only in local runtime configuration or deployment secrets.
- Do not paste real `runtime.jsonc`, response bodies, or request headers into public issues.
- Use `example.test` and placeholders in committed examples.
- New protocols should include endpoint, payload, polling, error, and base-URL contract tests.
