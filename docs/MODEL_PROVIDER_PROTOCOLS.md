# Model Provider Protocols / 模型协议自定义配置

This document explains how OpenReel Studio loads user-configured image, video,
and audio model providers. It covers the two configuration layers, the fields
accepted by the backend, and the errors users see when a configuration is
wrong.

本文说明 OpenReel Studio 如何加载用户自定义的图片、视频和音频模型。重点是两
层配置的职责、后端接受的字段，以及配置写错后会出现什么错误。

## 1. Two Layers / 两层配置

OpenReel separates shareable protocol definitions from private runtime provider
settings.

OpenReel 把“可共享的协议定义”和“带密钥的个人运行配置”分开。

| Layer | Purpose | Location | Secret allowed |
| --- | --- | --- | --- |
| Protocol catalog / 协议清单 | Defines HTTP method, path, request body, model capabilities, polling, and result parsing. / 定义 HTTP 方法、路径、请求体、模型能力、轮询和结果解析。 | `config/video_provider_protocols/catalog.json`, `config/image_provider_protocols/catalog.json`, `config/audio_provider_protocols/catalog.json` | No / 不允许 |
| Runtime provider / 运行 provider | Selects one protocol and supplies endpoint, API key, model name, and deployment-specific defaults. / 选择协议，并提供地址、密钥、模型名和部署相关默认值。 | `config/runtime.jsonc`, settings UI, or config API / 设置页或配置 API | Yes / 允许 |

Protocol catalogs are safe to commit when they contain no credentials. Runtime
provider config should stay local because it can contain API keys.

协议清单不包含密钥时可以提交到仓库。运行 provider 配置可能包含 API Key，应保
持在本地。

## 2. Catalog Files / 协议清单文件

The backend reads one catalog per media kind. A deployment can point to a
different catalog file with an environment variable.

后端按媒体类型读取一份协议清单。部署时可以用环境变量指向另一份单文件清单。

| Kind / 类型 | API format | Default catalog / 默认清单 | Override env / 覆盖环境变量 | Catalog version / 清单版本 | Protocol version / 协议版本 |
| --- | --- | --- | --- | --- | --- |
| Image / 图片 | `image_http_v1` | `config/image_provider_protocols/catalog.json` | `OPENREEL_IMAGE_PROTOCOLS_FILE` | `openreel.image_provider_catalog.v1` | `openreel.image_provider.v1` |
| Video / 视频 | `video_http_v1` | `config/video_provider_protocols/catalog.json` | `OPENREEL_VIDEO_PROTOCOLS_FILE` | `openreel.video_provider_catalog.v1` | `openreel.video_provider.v1` |
| Audio / 音频 | `audio_http_v1` | `config/audio_provider_protocols/catalog.json` | `OPENREEL_AUDIO_PROTOCOLS_FILE` | `openreel.audio_provider_catalog.v1` | `openreel.audio_provider.v1` |

The root shape is:

根结构如下：

```json
{
  "version": "openreel.video_provider_catalog.v1",
  "protocols": {
    "my_protocol_id": {
      "version": "openreel.video_provider.v1",
      "id": "my_protocol_id",
      "display_name": "My Provider Protocol"
    }
  }
}
```

`protocols` should be an object keyed by protocol id. The loader also accepts a
list of protocol objects with `id`, but the object form is preferred.

`protocols` 建议写成以协议 ID 为 key 的对象。加载器也兼容带 `id` 的数组，但推
荐使用对象形式。

## 3. Runtime Provider Config / 运行 Provider 配置

Runtime provider config lives in `config/runtime.jsonc` and is materialized into
the `media_providers` database table on load. The settings UI writes the same
shape through the config API.

运行 provider 配置位于 `config/runtime.jsonc`，加载后会同步到
`media_providers` 数据库表。设置页通过配置 API 写入同样的结构。

Accepted `media_providers` fields:

后端接受的 `media_providers` 字段：

| Field / 字段 | Type / 类型 | Meaning / 含义 |
| --- | --- | --- |
| `kind` | string | `image`, `video`, or `audio`. / `image`、`video` 或 `audio`。 |
| `name` | string | User-visible provider name. Unique with `kind`. / 用户可见名称，同一 `kind` 内唯一。 |
| `base_url` | string | Versioned or namespaced API Base URL, such as `/v1`, `/v2`, `/api/v3`, or `/suno`. It is not a bare host or complete resource endpoint. / 带版本或 API 命名空间的基础地址；不是裸域名，也不是完整资源接口。 |
| `api_key` | string or null | API key. `${ENV_VAR}` is resolved from environment without persisting the secret in DB config output. / API Key。`${ENV_VAR}` 会从环境变量解析，避免把真实密钥写死。 |
| `model_name` | string | Model id sent to the provider request body. / 发给服务商的模型 ID。 |
| `api_format` | string | Use `image_http_v1`, `video_http_v1`, or `audio_http_v1` for declarative protocols. Legacy values still exist for migration. / 声明式协议使用 `image_http_v1`、`video_http_v1` 或 `audio_http_v1`。旧值仅用于迁移兼容。 |
| `is_active` | boolean | At most one active provider per `kind`. / 每个 `kind` 最多只能有一个 active。 |
| `enabled` | boolean | Disabled providers stay configured but are not selected for generation. / 关闭后仍保存配置，但不会被生成流程选中。 |
| `notes` | string or null | Human note. / 备注。 |
| `params` | object | Runtime defaults and protocol id. / 运行时默认值和协议 ID。 |

For declarative providers, `params` must reference a protocol id:

声明式 provider 必须在 `params` 里引用协议 ID：

```jsonc
{
  "media_providers": [
    {
      "kind": "video",
      "name": "seedance-main",
      "base_url": "https://ark.cn-beijing.volces.com/api/v3",
      "api_key": "${VIDEO_API_KEY}",
      "model_name": "doubao-seedance-2-0-260128",
      "api_format": "video_http_v1",
      "is_active": true,
      "enabled": true,
      "notes": "Production video provider",
      "params": {
        "video_protocol_id": "seedance_2_0",
        "image_transport": "data_url",
        "public_base_url": "https://your-public-host.example.com"
      }
    }
  ]
}
```

Protocol objects are not allowed inside `params`. Store only the id plus runtime
overrides. For example, `params.video_protocol` or `params.protocol` as an
object is rejected.

`params` 里不能塞整段协议对象，只保存协议 ID 和运行时覆盖项。例如
`params.video_protocol` 或对象形式的 `params.protocol` 会被拒绝。

Protocol id keys:

协议 ID 字段：

| Kind / 类型 | Required param / 必填参数 |
| --- | --- |
| Image / 图片 | `params.image_protocol_id` |
| Video / 视频 | `params.video_protocol_id` |
| Audio / 音频 | `params.audio_protocol_id` |

`base_url` is always used literally. The backend does not add, remove, or guess
an API version. Protocol section paths contain resources only. For example,
`https://api.openai.com/v1` plus `/images/generations` forms the final endpoint.

`base_url` 始终按原值使用，后端不补写、删除或猜测 API 版本。协议 section 的
path 只写资源路径。例如 `https://api.openai.com/v1` 与
`/images/generations` 拼成最终接口。

If one provider spans multiple API bases, each base is configured explicitly.
For example, T8 video generation uses `base_url: https://ai.t8star.org/v2`,
while its upload section declares `base_url_param: upload_base_url` and runtime
params supplies `upload_base_url: https://ai.t8star.org/v1`. The upload path is
then only `/files`. This avoids rewriting `/v1` or `/v2` in backend code.

如果同一 provider 跨多个 API Base，每个地址都显式配置。例如 T8 视频生成的
`base_url` 是 `https://ai.t8star.org/v2`，上传 section 用
`base_url_param: upload_base_url` 指向运行配置中的
`upload_base_url: https://ai.t8star.org/v1`，上传 path 只写 `/files`。后端不再
改写 `/v1` 或 `/v2`。

## 4. Common Protocol Fields / 通用协议字段

Catalog protocol objects are intentionally permissive: unknown fields are kept
in the JSON file but ignored unless backend code reads them. The fields below
are the ones currently used by the backend.

协议对象本身是宽松的：未知字段会留在 JSON 文件里，但后端不读取就不会生效。下
表是当前后端实际使用的字段。

| Field / 字段 | Applies to / 适用 | Meaning / 含义 |
| --- | --- | --- |
| `version` | all / 全部 | Must match the protocol version for the media kind. / 必须等于对应媒体类型的协议版本。 |
| `id` | all / 全部 | Must match the key in `protocols`. / 必须与 `protocols` 中的 key 一致。 |
| `display_name` | all / 全部 | Name shown in settings UI. / 设置页展示名。 |
| `default_base_url` or `base_url` | all / 全部 | Fallback base URL when runtime provider has no base URL. / 运行 provider 未填地址时的默认地址。 |
| `headers` | all / 全部 | Static headers merged into request headers. Section headers override protocol headers. / 静态请求头；section 级别覆盖 protocol 级别。 |
| `auth` | all / 全部 | `bearer` / `authorization_bearer`, `api_key_header` / `header`, or `authorization_raw` / `raw`. / 授权方式。 |
| `api_key_header` | all / 全部 | Header name when `auth` is `api_key_header`. / `api_key_header` 模式下的 header 名。 |
| `model_profiles` or `models` | all / 全部 | Per-model matching and constraints. Supports `match`, `model`, `match_contains`, and `match_regex`. / 按模型匹配约束，支持精确、包含和正则匹配。 |
| `request` | all / 全部 | Create/generate HTTP request. / 创建或生成请求。 |
| `poll` | async image/video/audio / 异步图片/视频/音频 | Polling request and status rules. / 轮询请求和状态规则。 |
| `result` | all / 全部 | Paths used to extract generated media. / 解析生成媒体的路径。 |

### Request Section / `request` 字段

Common request fields:

通用 `request` 字段：

| Field / 字段 | Meaning / 含义 |
| --- | --- |
| `method` | HTTP method. Defaults to `POST`; `GET` sends the rendered body as query params for image/video/audio create calls. / HTTP 方法，默认 `POST`；`GET` 会把渲染后的 body 作为查询参数发送。 |
| `path` or `endpoint` | Resource path joined with the selected API Base URL, or an absolute URL. `{task_id}` is replaced in poll paths. Version prefixes belong to the API Base URL. / 与选定 API Base URL 拼接的资源路径或绝对 URL；版本前缀属于 API Base URL。 |
| `base_url_param` | Optional runtime `params` key containing a separate versioned API Base URL for this section. Required when declared. / 可选；指定本 section 使用的独立版本化 API Base URL 在运行 `params` 中的字段名，声明后必填。 |
| `base_url_label`, `base_url_hint` | Settings UI label and guidance for a declared `base_url_param`. / `base_url_param` 在设置页中的标签和说明。 |
| `auth`, `headers`, `api_key_header` | Same meaning as protocol-level fields, with section-level override. / 与 protocol 级别同义，section 级别覆盖。 |
| `body` | JSON body template. Must render to an object. / JSON 请求体模板，渲染后必须是对象。 |
| `task_id_paths` or `id_paths` | Dot paths used to read task id from create response. / 从创建响应里读取任务 ID 的点号路径。 |
| `merge_extra` | When true, non-internal runtime `params` keys are merged into the outgoing payload. / 为 true 时，把非内部运行参数合并进请求体。 |
| `required_context` | Audio only. Required placeholder names such as `input`. / 仅音频使用，声明必需上下文字段。 |
| `encoding`, `body_type`, or `content_type` | Video only. Use `multipart` / `multipart/form-data` for multipart requests. / 仅视频使用，multipart 请求用此字段声明。 |
| `form` | Video multipart form template. / 视频 multipart 表单模板。 |
| `files` | Video multipart file map. Supported selectors: `$first_image_file`, `$image_file`, `$source_image_file`. / 视频 multipart 文件字段映射。 |

Placeholders use the exact `$name` form. If a placeholder resolves to `null`,
empty string, empty array, or empty object, that field is omitted. `false` and
`0` are preserved.

占位符使用精确的 `$name` 格式。占位符渲染为 `null`、空字符串、空数组或空对象
时，该字段会被省略；`false` 和 `0` 会保留。

### Poll Section / `poll` 字段

Polling fields:

轮询字段：

| Field / 字段 | Meaning / 含义 |
| --- | --- |
| `method` | `GET` by default; `POST` is supported. / 默认 `GET`，支持 `POST`。 |
| `path` or `endpoint` | Poll URL. Use `{task_id}` for the created job id. / 轮询 URL，用 `{task_id}` 表示任务 ID。 |
| `status_path` | Dot path to provider status. Defaults to `status`. / 状态字段路径，默认 `status`。 |
| `progress_path` | Dot path to progress value. / 进度字段路径。 |
| `succeeded` or `done_statuses` | Status values treated as success. / 成功状态集合。 |
| `failed` or `failed_statuses` | Status values treated as failure. / 失败状态集合。 |
| `running` or `running_statuses` | Status values treated as still running. / 运行中状态集合。 |
| `interval_seconds` | Local poll interval. / 本地轮询间隔。 |
| `timeout_seconds` | Local poll timeout. / 本地轮询超时。 |
| `body` | Audio poll only. Template rendered with `$task_id` / `$job_id` for POST polling. / 仅音频轮询使用，POST 轮询时可用 `$task_id` / `$job_id`。 |

If no `poll` section exists and the create response has no immediate media URL,
async generation cannot complete.

如果没有 `poll`，并且创建响应里也没有直接媒体 URL，异步生成无法完成。

## 5. Image Protocols / 图片协议

Image providers use `api_format: "image_http_v1"` and
`params.image_protocol_id`.

图片 provider 使用 `api_format: "image_http_v1"` 和
`params.image_protocol_id`。

Important image protocol fields:

图片协议关键字段：

| Field / 字段 | Meaning / 含义 |
| --- | --- |
| `image_transport` | How reference images are sent. Common values: `data_url`, `public_url`. / 参考图传输方式，常用 `data_url`、`public_url`。 |
| `default_response_format` | Fallback response format placeholder value. / 默认响应格式。 |
| `default_params` | Used by settings UI presets. Generation values still come from the node request and runtime provider params. / 用于设置页预设；生成时实际值仍来自节点请求和运行 provider 参数。 |
| `model_profiles[].default_params` | Per-model preset defaults for settings UI. / 单模型默认参数预设。 |
| `supported_sizes` or `sizes` | Size hints shown by catalog API. / 尺寸提示。 |

Image request placeholders:

图片请求占位符：

| Placeholder / 占位符 | Value / 值 |
| --- | --- |
| `$model` | Runtime `model_name` or `params.model`. |
| `$prompt` | Node prompt. |
| `$negative_prompt` | Negative prompt. |
| `$size` | Requested size such as `1024x1024`. |
| `$quality` | Requested quality. |
| `$count` / `$n` | Number of images. |
| `$response_format` | Runtime or protocol response format. |
| `$reference_images` / `$reference_image_urls` | All resolved reference images. |
| `$reference_image_input` | One reference image if there is exactly one, otherwise the full list. |
| `$first_reference_image` | First resolved reference image. |

Image result fields:

图片结果字段：

| Field / 字段 | Meaning / 含义 |
| --- | --- |
| `images_path` or `items_path` | Path to an array of image items. / 图片数组路径。 |
| `url_path` | URL field inside each item, default `url`. / 数组项内 URL 字段。 |
| `b64_path` | Base64 field inside each item, default `b64_json`. / 数组项内 base64 字段。 |
| `image_url_paths` or `url_paths` | Fallback paths for a single image URL. / 单图 URL 兜底路径。 |
| `b64_paths` or `b64_json_paths` | Fallback paths for base64 image data. / base64 图片兜底路径。 |

Image generation can return images immediately or return a task id and use
`poll`.

图片生成可以直接返回图片，也可以返回任务 ID 后进入 `poll`。

## 6. Video Protocols / 视频协议

Video providers use `api_format: "video_http_v1"` and
`params.video_protocol_id`.

视频 provider 使用 `api_format: "video_http_v1"` 和
`params.video_protocol_id`。

Video protocols describe capabilities so the frontend can show the correct mode
and reference limits without hardcoding model names.

视频协议会描述模型能力，前端据此动态显示模式和参考图限制，而不是在界面里硬编
码模型名。

Important video fields:

视频关键字段：

| Field / 字段 | Meaning / 含义 |
| --- | --- |
| `supported_ratios`, `ratios`, or `supported_aspect_ratios` | Accepted aspect ratios. / 支持的画幅。 |
| `supported_resolutions` or `resolutions` | Accepted resolutions. / 支持的分辨率。 |
| `default_ratio`, `default_resolution` | Defaults for UI and request construction. / 界面和请求默认值。 |
| `duration` | Object with `min`, `max`, `allowed_values`, and optional `step`. / 时长规则。 |
| `resolution_rules` | Duration-dependent resolution limits. / 与时长相关的分辨率限制。 |
| `forbidden_fields` | Runtime fields rejected for this protocol or mode. / 该协议或模式不允许的运行字段。 |
| `image_transport` | Reference image transport: `data_url`, `public_url`, `upload_url`, etc. / 参考图传输方式。 |
| `modes` | Generation modes and media limits. / 生成模式和媒体数量限制。 |
| `content` | Converts prompt and media refs to provider content items. / 把提示词和媒体引用转换成服务商 content 项。 |
| `upload` or `uploads.image` | Optional upload-first image endpoint. / 可选的先上传图片接口。 |

Mode fields inside `modes`:

`modes` 内部字段：

| Field / 字段 | Meaning / 含义 |
| --- | --- |
| `label` | Human label. / 展示名。 |
| `prompt_required` | Whether prompt is required. / 是否必须填写 prompt。 |
| `required_roles` | Required reference roles, such as `first_frame`. / 必需引用角色。 |
| `allowed_roles` | Allowed reference roles. / 允许的引用角色。 |
| `min_images`, `max_images` | Image reference limits. / 图片引用数量限制。 |
| `min_videos`, `max_videos` | Video reference limits. / 视频引用数量限制。 |
| `min_audios`, `max_audios` | Audio reference limits. / 音频引用数量限制。 |
| `min_total_media`, `max_total_media` | Total media reference limits. / 总媒体引用数量限制。 |
| `audio_requires_visual` | Audio reference must be accompanied by image or video reference. / 音频参考必须搭配图片或视频。 |
| `supported_ratios`, `supported_resolutions`, `duration` | Mode-level overrides. / 模式级覆盖。 |

Video request placeholders:

视频请求占位符：

| Placeholder / 占位符 | Value / 值 |
| --- | --- |
| `$model` | Runtime `model_name` or `params.model`. |
| `$prompt` | Video prompt. |
| `$content` | Typed multimodal content list built from prompt and references. |
| `$media_references` | Resolved media reference metadata. |
| `$image_urls`, `$video_urls`, `$audio_urls` | Resolved media URL arrays by type. |
| `$first_image_url`, `$first_video_url`, `$first_audio_url` | First resolved media URL of each type. |
| `$first_frame_image_url` | First frame image URL in `first_frame` mode. |
| `$reference_image_urls` | Image URLs in `multimodal_reference` mode. |
| `$duration_seconds` / `$duration` | Validated duration. |
| `$aspect_ratio` / `$ratio` | Validated aspect ratio. |
| `$resolution` | Provider-formatted resolution. |
| `$raw_resolution` | Normalized internal resolution. |
| `$video_size` | Derived `WIDTHxHEIGHT` size for common resolutions. |
| `$mode` | Inferred or explicit mode. |
| `$generate_audio`, `$watermark`, `$return_last_frame` | Optional booleans from runtime params. |
| `$priority`, `$execution_expires_after`, `$seed` | Optional integers from runtime params. |
| `$safety_identifier`, `$tools` | Optional provider-specific values. |

Video result fields:

视频结果字段：

| Field / 字段 | Meaning / 含义 |
| --- | --- |
| `video_url_paths`, `url_paths`, or `result_url_paths` | Paths used to extract final video URL. / 读取最终视频 URL 的路径。 |
| `last_frame_url_paths` | Optional paths for generated last frame. / 可选的尾帧 URL 路径。 |

If `max_*` limits are exceeded by references, the backend keeps the first
allowed references and records a warning. If the remaining refs still violate
the selected mode, generation fails with `bad_request`.

如果引用超过 `max_*` 限制，后端会保留前面允许数量的引用并记录 warning。如果
截断后仍不满足模式规则，生成会以 `bad_request` 失败。

## 7. Audio Protocols / 音频协议

Audio providers use `api_format: "audio_http_v1"` and
`params.audio_protocol_id`.

音频 provider 使用 `api_format: "audio_http_v1"` 和
`params.audio_protocol_id`。

Audio supports both direct binary responses, such as text-to-speech APIs, and
async task APIs, such as music generation relays.

音频支持两类接口：直接返回音频二进制的接口，例如 TTS；以及异步任务接口，例如
音乐生成中转站。

Audio request placeholders:

音频请求占位符：

| Placeholder / 占位符 | Value / 值 |
| --- | --- |
| `$model` | Runtime `model_name` or `params.model`. |
| `$prompt` | User prompt. |
| `$input` / `$text` | Input text. Defaults to prompt unless overridden. |
| `$title` | Song or audio title. |
| `$style` | Style description. |
| `$lyrics` | Lyrics from runtime params. |
| `$mv` | Version or music model variant. |
| `$voice` | TTS voice. |
| `$speed` | Numeric speed. |
| `$instructions` | Voice instructions or style. |
| `$response_format` / `$format` / `$audio_format` | Audio format. |
| `$instrumental`, `$customMode`, `$custom_mode` | Music generation booleans. |
| `$negativeTags` | Negative tags. |
| `$callBackUrl`, `$notify_hook` | Callback fields when provider supports them. |
| `$seed` | Optional integer seed. |

Audio result fields:

音频结果字段：

| Field / 字段 | Meaning / 含义 |
| --- | --- |
| `type` or `response_type` | `binary` for direct audio bytes; otherwise JSON result parsing is used. / `binary` 表示直接音频二进制；否则按 JSON 结果解析。 |
| `items_paths` or `audio_items_paths` | Paths to arrays or objects that contain audio items. / 音频结果项路径。 |
| `url_paths` or `audio_url_paths` | Audio URL paths inside each item. / 结果项内音频 URL 路径。 |
| `id_paths`, `title_paths`, `source_url_paths`, `stream_url_paths`, `image_url_paths`, `duration_paths`, `tags_paths` | Optional metadata paths. / 可选元数据路径。 |
| `message_paths` | Paths used to extract provider error messages. / 读取服务商错误信息的路径。 |
| `complete_on_audio_items` | When true, audio items can complete the poll even if status is not in `succeeded`. / 为 true 时，只要轮询结果里出现音频项即可完成。 |

Audio `request.success_path` and `poll.success_path` can be used for providers
that return a separate success code. The accepted success values default to
`0`, `200`, `success`, `ok`, and `true`, or can be overridden with
`success_values`.

音频的 `request.success_path` 和 `poll.success_path` 可用于带独立成功码的服务商。
默认成功值包括 `0`、`200`、`success`、`ok`、`true`，也可以用
`success_values` 覆盖。

## 8. Frontend Discovery / 前端如何读取

The settings UI and node editor should discover protocols through backend
endpoints instead of hardcoding model capabilities.

设置页和节点编辑器应通过后端接口发现协议能力，而不是在前端硬编码模型能力。

| Endpoint / 接口 | Returns / 返回 |
| --- | --- |
| `GET /api/tools/config/image-protocols` | Image protocol ids, display names, model names, and size hints. / 图片协议 ID、展示名、模型名和尺寸提示。 |
| `GET /api/tools/config/video-protocols` | Video protocol ids, display names, model profiles, modes, ratio/resolution/duration limits. / 视频协议 ID、展示名、模型档案、模式、画幅/分辨率/时长限制。 |
| `GET /api/tools/config/audio-protocols` | Audio protocol ids, display names, model names, and result type. / 音频协议 ID、展示名、模型名和结果类型。 |

The UI uses these summaries for dropdowns, mode chips, reference count limits,
and validation hints. The full request body templates stay on the backend.

前端用这些摘要做下拉框、模式胶囊、参考图数量限制和校验提示。完整请求模板留在
后端清单文件里。

## 9. What Happens When It Is Wrong / 写错了会怎么样

Configuration is checked in several places.

配置会在多个位置被检查。

### Runtime Config Save or Load / 保存或加载运行配置

`config/runtime.jsonc` is parsed as JSON5 and validated with a strict schema.
Unknown top-level fields or unknown `media_providers` fields are rejected.

`config/runtime.jsonc` 按 JSON5 解析，并用严格 schema 校验。未知顶层字段或未
知 `media_providers` 字段会被拒绝。

Examples:

示例：

| Mistake / 错误 | Result / 结果 |
| --- | --- |
| Invalid JSON5 / JSON5 语法错误 | `JSON5 parse error: ...` |
| `kind: "videoo"` | `kind must be 'image', 'video', or 'audio'` |
| Unsupported `api_format` | `api_format must be ...` |
| Two active video providers / 两个 active 视频 provider | `media_providers[video]: is_active 至多 1 条` |
| Missing `params.video_protocol_id` for `video_http_v1` | `video_http_v1 provider 必须设置 params.video_protocol_id` |
| Protocol id not in catalog / 协议 ID 不存在 | `params.video_protocol_id='...' 不在 config/video_provider_protocols/catalog.json 的 protocols 中` |
| Embedded protocol object in params / 在 params 中塞协议对象 | `provider 只保存 params.<kind>_protocol_id；协议 JSON 必须写在 config/.../catalog.json` |

If validation fails, the config file is not loaded into the runtime database.

校验失败时，该配置不会同步到运行数据库。

### Catalog Read / 读取协议清单

Catalog loading returns `ok: false` through the protocol list endpoints and
returns `error_kind: "bad_config"` during generation.

清单读取失败时，协议列表接口会返回 `ok: false`；生成阶段会返回
`error_kind: "bad_config"`。

Common catalog errors:

常见清单错误：

| Mistake / 错误 | Result / 结果 |
| --- | --- |
| File missing / 文件不存在 | `未找到 protocol catalog 文件` |
| Invalid JSON / JSON 非法 | `protocol catalog 无法读取或不是合法 JSON` |
| Wrong catalog version / 清单版本错误 | `protocol catalog.version 必须是 ...` |
| Missing `protocols` / 缺少 `protocols` | `protocol catalog 缺少 protocols` |
| Wrong protocol version / 协议版本错误 | `protocol.version 必须是 ...` |
| `id` does not match key / `id` 与 key 不一致 | `protocol id 不匹配` |

### Generation Time / 生成阶段

Generation returns structured errors. Important `error_kind` values:

生成阶段会返回结构化错误。常见 `error_kind`：

| `error_kind` | Meaning / 含义 |
| --- | --- |
| `bad_config` | Provider or protocol is malformed: missing API key, missing `request.body`, missing `poll.path`, wrong protocol id, no base URL, etc. / provider 或协议配置错误。 |
| `bad_request` | User/node request violates protocol: unsupported duration, unsupported resolution, missing prompt, too few/too many references, invalid reference role, invalid number field. / 用户或节点请求不符合协议能力。 |
| `bad_response` | Provider response does not contain required task id or media URL. / 服务商响应缺少任务 ID 或媒体 URL。 |
| `empty_response` | Provider response has no usable image/audio/video output. / 响应没有可用产物。 |
| `provider_failed` | Provider reported a failed task or failure status. / 服务商返回失败状态。 |
| `timeout` | Local polling exceeded configured timeout. / 本地轮询超时。 |
| `network` | HTTP request failed before receiving a valid response. / 网络请求失败。 |
| `unsupported_action` | Operation cannot be performed with this protocol state, such as polling without `poll`. / 当前协议状态不支持该操作。 |

The backend includes endpoint, job id, status, raw provider response, and poll
history when available. These details are for debugging and should not be shown
as the main user-facing creative result.

后端会尽量带上 endpoint、job id、状态、原始服务商响应和轮询历史。这些信息用于
排障，不应该作为主要创作结果展示给普通用户。

## 10. Minimal Examples / 最小示例

### Image / 图片

```json
{
  "version": "openreel.image_provider_catalog.v1",
  "protocols": {
    "openai_images_generations": {
      "version": "openreel.image_provider.v1",
      "id": "openai_images_generations",
      "display_name": "OpenAI-compatible images.generations",
      "image_transport": "data_url",
      "request": {
        "method": "POST",
        "path": "/images/generations",
        "auth": "bearer",
        "merge_extra": true,
        "body": {
          "model": "$model",
          "prompt": "$prompt",
          "n": "$count",
          "size": "$size",
          "quality": "$quality",
          "response_format": "$response_format",
          "image": "$reference_image_input"
        }
      },
      "result": {
        "images_path": "data",
        "url_path": "url",
        "b64_path": "b64_json"
      }
    }
  }
}
```

### Video / 视频

```json
{
  "version": "openreel.video_provider_catalog.v1",
  "protocols": {
    "example_video_task": {
      "version": "openreel.video_provider.v1",
      "id": "example_video_task",
      "display_name": "Example video task API",
      "default_base_url": "https://api.example.com",
      "supported_ratios": ["16:9", "9:16", "1:1"],
      "supported_resolutions": ["720p", "1080p"],
      "duration": {"min": 4, "max": 15},
      "modes": {
        "text_to_video": {
          "label": "Text to video",
          "prompt_required": true,
          "max_images": 0
        },
        "first_frame": {
          "label": "First frame",
          "prompt_required": false,
          "required_roles": ["first_frame"],
          "allowed_roles": ["first_frame"],
          "min_images": 1,
          "max_images": 1
        }
      },
      "request": {
        "method": "POST",
        "path": "/tasks",
        "auth": "bearer",
        "task_id_paths": ["id", "data.id"],
        "body": {
          "model": "$model",
          "prompt": "$prompt",
          "image_urls": "$image_urls",
          "duration": "$duration_seconds",
          "aspect_ratio": "$aspect_ratio",
          "resolution": "$resolution"
        }
      },
      "poll": {
        "method": "GET",
        "path": "/tasks/{task_id}",
        "status_path": "status",
        "succeeded": ["succeeded", "completed"],
        "failed": ["failed", "error"],
        "running": ["queued", "running"],
        "interval_seconds": 8,
        "timeout_seconds": 1200
      },
      "result": {
        "video_url_paths": ["video_url", "data.video_url", "result.url"]
      }
    }
  }
}
```

### Audio / 音频

```json
{
  "version": "openreel.audio_provider_catalog.v1",
  "protocols": {
    "openai_audio_speech": {
      "version": "openreel.audio_provider.v1",
      "id": "openai_audio_speech",
      "display_name": "OpenAI-compatible audio.speech",
      "default_params": {
        "voice": "alloy",
        "response_format": "mp3"
      },
      "request": {
        "method": "POST",
        "path": "/audio/speech",
        "auth": "bearer",
        "required_context": ["input"],
        "body": {
          "model": "$model",
          "input": "$input",
          "voice": "$voice",
          "response_format": "$response_format",
          "speed": "$speed",
          "instructions": "$instructions"
        }
      },
      "result": {
        "type": "binary",
        "format_param": "response_format"
      }
    }
  }
}
```

## 11. Checklist / 配置检查清单

- Put shared HTTP shape and model capabilities in the matching catalog file.
  把可共享的 HTTP 结构和模型能力写到对应 catalog。
- Put `base_url`, `api_key`, `model_name`, active state, and protocol id in
  `config/runtime.jsonc` or the settings UI. 把地址、密钥、模型名、启用状态和
  协议 ID 写到运行配置或设置页。
- Keep one active provider per media kind. 每个媒体类型最多保留一个 active。
- Use the backend protocol list endpoints to verify the catalog is readable.
  用协议列表接口确认 catalog 能被后端读取。
- Test one small request before using the provider in a workflow. 在工作流中使
  用前先跑一个小请求。
- When debugging, read `error_kind` first, then inspect `endpoint`, `job_id`,
  provider `raw`, and `polls`. 排障时先看 `error_kind`，再看 endpoint、job id、
  原始响应和轮询记录。
