# Video Model Calling Notes

This file is an editable reference for video provider call limits. It is not
loaded into the default prompt. When a video provider call fails with a model
capability or argument error, read this file with the existing workspace file
reader before repairing the video node.

Suggested existing tool call:

```json
{
  "name": "file.workspace_read",
  "input": {
    "path": "apps/api/app/skills/video_production/VIDEO_MODEL_CALLING.md"
  }
}
```

## Video Provider Templates / 视频 Provider 模板

The preferred extensibility path is `api_format: "video_http_v1"` plus
`provider.params.video_protocol_id`. Protocols live in one shareable catalog
file, `config/video_provider_protocols/catalog.json`; runtime provider settings keep
only base URL, API key, model name, and the protocol id. The backend reads the
catalog at runtime, fills prompt/media/duration variables, sends the configured
HTTP request, and polls using the configured status/result paths.

推荐扩展入口是 `api_format: "video_http_v1"` 加
`provider.params.video_protocol_id`。协议统一写在可共享的单文件
`config/video_provider_protocols/catalog.json`；运行时 provider 设置只保存 Base URL、
API Key、模型名和协议 ID。后端运行时读取 catalog，填入提示词、媒体引用、时长
等变量，按协议发送 HTTP 请求，并按协议里的状态和结果路径轮询。

Set `OPENREEL_VIDEO_PROTOCOLS_FILE` only when a deployment wants to point the
backend at a different single catalog file. The file shape stays the same.

部署时如需使用另一份单文件 catalog，可设置
`OPENREEL_VIDEO_PROTOCOLS_FILE`。文件结构保持一致。

| Model / provider | API format | Protocol id | Modes | Source media | Resolutions |
| --- | --- | --- | --- | --- | --- |
| custom declarative provider | `video_http_v1` | user-defined | protocol-defined | protocol-defined image/video/audio refs | protocol-defined |
| `doubao-seedance-2-0-260128` | `video_http_v1` | `seedance_2_0` | text, first frame, first+last frame, multimodal reference | images 0-9, videos 0-3, audios 0-3; audio requires image or video | `480p`, `720p`, `1080p`, `4k` |
| `doubao-seedance-2-0-fast-260128` | `video_http_v1` | `seedance_2_0` | same Seedance 2.0 modes | same Seedance 2.0 media limits | `480p`, `720p` |
| `doubao-seedance-2-0-mini-260615` | `video_http_v1` | `seedance_2_0` | same Seedance 2.0 modes | same Seedance 2.0 media limits | `480p`, `720p` |
| Lingke-style relay model id | `video_http_v1` | `lingke_media_generate_json_task` | relay task API | 0-12 images, protocol-configured URL/data URL input | `720p`, `1080p` |
| `grok-video-3` | `video_http_v1` | `t8_grok_video_3_json_task` | text-to-video or image-to-video | 0-7 images, upload-first URL list | `480p`, `720p`, `1080p`; >15s only `720p` |
| `grok-1.5-video-15s` | `video_http_v1` | `grok_1_5_multipart` | image-to-video only | exactly 1 multipart image | `480p`, `720p` |
| `grok-imagine-video-1.5` | `video_http_v1` | `xai_grok_imagine_video_1_5` | image-to-video only | exactly 1 image URL/data URL object | `480p`, `720p` |

`2k` is still a UI placeholder for future video models. `4k` is valid only when
the selected provider protocol/model profile lists it.

`2k` 仍是未来视频模型的界面占位。`4k` 只有在当前 provider 协议或模型档案明
确声明支持时才可写入视频节点。

### `video_http_v1` Protocol / 协议要点

`config/video_provider_protocols/catalog.json` contains
`version: "openreel.video_provider_catalog.v1"` and a `protocols` object keyed
by protocol id. Each protocol object must contain
`version: "openreel.video_provider.v1"` and an `id` matching the key. Common
sections are:

`config/video_provider_protocols/catalog.json` 包含
`version: "openreel.video_provider_catalog.v1"` 和以协议 ID 为 key 的
`protocols` 对象。每个协议对象必须包含
`version: "openreel.video_provider.v1"`，且 `id` 与 key 一致。常用部分如下：

- `model_profiles`: per-model constraints such as supported resolutions.
- `modes`: named generation modes and validation rules.
- `content`: how text/image/video/audio references become provider content
  items.
- `request`: HTTP method, path, auth style, task id paths, JSON body template,
  or multipart `form` and `files`.
- `upload`: optional upload-first media endpoint for protocols that need image
  files converted to provider URLs before the create request.
- `poll`: polling method, path, status path, status groups, interval, and
  timeout.
- `result`: paths used to read the final video URL and optional last-frame URL.

`request.body` and multipart `request.form` use `$variable` placeholders. Supported variables include
`$model`, `$prompt`, `$content`, `$duration_seconds`, `$aspect_ratio`,
`$resolution`, `$raw_resolution`, `$video_size`, `$mode`, `$media_references`,
`$image_urls`, `$first_image_url`, `$video_urls`, `$first_video_url`,
`$audio_urls`, `$first_audio_url`, `$generate_audio`, `$watermark`,
`$return_last_frame`, `$priority`, `$execution_expires_after`,
`$safety_identifier`, `$seed`, and `$tools`.
Use `$content` for providers that accept typed multimodal content items. Use
`$image_urls`, `$video_urls`, or `$audio_urls` for providers that accept plain URL
arrays. Empty values are omitted; `false` and `0` are preserved.

`request.body` 和 multipart `request.form` 使用 `$variable` 占位。可用变量包括 `$model`、`$prompt`、
`$content`、`$duration_seconds`、`$aspect_ratio`、`$resolution`、`$mode`、
`$raw_resolution`、`$video_size`、`$media_references`、`$image_urls`、
`$first_image_url`、`$video_urls`、`$first_video_url`、`$audio_urls`、
`$first_audio_url`、`$generate_audio`、`$watermark`、`$return_last_frame`、
`$priority`、`$execution_expires_after`、`$safety_identifier`、`$seed` 和
`$tools`。服务商接收带类型的多模态 content 时使用 `$content`；服务商接收普通 URL 数组时使用
`$image_urls`、`$video_urls` 或 `$audio_urls`。空值会省略，`false` 和 `0`
会保留。

`lingke_media_generate_json_task` uses `POST /media/generate` relative to the
provider base URL with a body shaped like
`{model, params:{prompt, images, aspect_ratio, duration, resolution}}`, then
polls `GET /skills/task-status?task_id=...`. The provider `model_name` is
sent as the `model` field; users can fill any model id supported by the relay.
Polling stops when the response has `is_final=true`; successful tasks return
`state:"success"` and `result_url`, while failures return `state:"failed"` and
an `error` string.

Protocol entries adapt provider differences without hardcoding model ids in
core code. Use request templates for field paths, `resolution_output` for
provider casing, `supported_ratios` and `default_ratio` for ratio limits,
`duration` for duration ranges or fixed values, and `resolution_rules` for
duration-dependent resolution limits.

Runtime provider params should stay deployment-specific: base URL, API key,
model name, `video_protocol_id`, and optional public URL host settings such as
`public_base_url`. Request shape, limits, upload-first behavior, multipart
fields, and result parsing belong in the protocol catalog.

运行时 provider params 应保持部署相关：Base URL、API Key、模型名、
`video_protocol_id`，以及可选的 `public_base_url` 等公网访问设置。请求结构、
能力限制、先上传、multipart 字段和结果解析都写进协议 catalog。

媒体 `base_url` 是带版本或 API 命名空间的 API Base URL，后端按原值使用；协议
path 只写 `/videos`、`/files` 等资源路径。同一 provider 跨多个 API Base 时，
section 用 `base_url_param` 显式引用运行配置里的额外版本化地址。

`video_http_v1` also reads protocol-level `image_transport`. Video and audio
references are URL-based: use public `http(s)` URLs, or configure
`public_base_url` so project `/api/media/...` files can be converted to public
URLs.

`video_http_v1` 也会读取协议级 `image_transport`。视频和音频参考按 URL 传递：
可以使用公网 `http(s)` URL；如果要传项目内 `/api/media/...` 文件，需要配置
`public_base_url` 让后端转换成公网 URL。

## Node Fields

For video nodes, use:

- `fields.model`: one of the template model ids above, or leave empty to use
  the active video provider. It can also be a configured video provider name or
  a custom provider model id when the provider is configured with the matching
  `api_format`.
- `fields.aspect_ratio`: use the target provider ratio, commonly `16:9`,
  `9:16`, `3:2`, `2:3`, or `1:1`.
- `fields.duration_seconds`: the requested video duration in seconds.
- `fields.resolution`: one of the supported resolution values for the selected
  model.
- `fields.video_mode`: optional explicit mode such as `text_to_video`,
  `first_frame`, `first_last_frame`, or `multimodal_reference`. When omitted,
  the backend infers mode from frame fields and media references.
- `fields.references`: image references with `role:"visual_reference"` when the
  chosen model/path needs source images.
- `fields.reference_images`: image references for image-to-video or multimodal
  providers.
- `fields.reference_videos`: one URL/node/asset reference per item for
  multimodal providers that accept video references.
- `fields.reference_audios`: one URL/node/asset reference per item for
  multimodal providers that accept audio references.

视频节点字段：

- `fields.model`：可填模板模型 id、配置好的视频 Provider 名，或留空使用激活
  Provider。
- `fields.aspect_ratio`：目标比例，例如 `16:9`、`9:16`、`1:1`。
- `fields.duration_seconds`：目标时长，单位秒。
- `fields.resolution`：当前模型/协议支持的分辨率。
- `fields.video_mode`：可选显式模式，例如 `text_to_video`、`first_frame`、
  `first_last_frame`、`multimodal_reference`；留空时后端按首尾帧和媒体引用推断。
- `fields.reference_images`：图片参考。
- `fields.reference_videos`：视频参考，每项一行 URL、`node:` 或 `asset:`。
- `fields.reference_audios`：音频参考，每项一行 URL、`node:` 或 `asset:`。

For `grok-1.5-video-15s` and `grok-imagine-video-1.5`, keep exactly one source
image reference on the video node. If multiple images are needed for planning,
first choose or create a single final source image, then reference only that
image from the video node.

For `grok-video-3`, use 0-7 source images. If several images are referenced,
write the prompt so each referenced image has a clear role; when the user or
provider convention needs explicit image mentions, use `@img1` through `@img7`.
Duration is 6-30 seconds. Durations above 15 seconds only support `720p`.

## Repair Rules

If a video call fails because of source image count, resolution, or provider
format:

1. Read this file.
2. Update the original `video` node fields; do not create a replacement node
   unless the user asked for a new version.
3. Rerun the original node after the model, resolution, aspect ratio, prompt,
   and references match the table above.
