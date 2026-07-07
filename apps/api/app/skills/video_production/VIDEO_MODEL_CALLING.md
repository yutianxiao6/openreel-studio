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

## Video Provider Templates

The table lists known templates and capability presets. A configured video
provider can use a custom `model_name` when its `api_format` matches the request
structure used by the current endpoint. The configured `api_format` selects the
backend adapter; the configured `model_name` is sent to the provider as the
model field.

| Model | API format | Path | Source images | Resolutions |
| --- | --- | --- | --- | --- |
| custom model id | `lingke_media_generate` | relay `/v1/media/generate` task API | 0-12 images, default data URL or configured public URL | `480p`, `720p`, `1080p` |
| `grok-video-3` | `t8_grok_video_3` | text-to-video or image-to-video | 0-7 images | `480p`, `720p`, `1080p` |
| `grok-1.5-video-15s` | `grok_1_5` | image-to-video only | exactly 1 image | `480p`, `720p` |
| `grok-imagine-video-1.5` | `xai_video` | official xAI image-to-video | exactly 1 image | `480p`, `720p` |
| `doubao-seedance-2-0-260128` | `volcengine_ark` | text-to-video or image-to-video | 0 or more images | `480p`, `720p`, `1080p` |
| `doubao-seedance-2-0-fast-260128` | `volcengine_ark` | text-to-video or image-to-video | 0 or more images | `480p`, `720p` |
| `doubao-seedance-2-0-mini-260615` | `volcengine_ark` | text-to-video or image-to-video | 0 or more images | `480p`, `720p` |

`2k` and `4k` are UI placeholders for future video models. Do not write them to
`video.fields.resolution` for the current supported models.

`lingke_media_generate` uses `POST /v1/media/generate` with a body shaped like
`{model, params:{prompt, images, aspect_ratio, duration, resolution}}`, then
polls `GET /v1/skills/task-status?task_id=...`. The provider `model_name` is
sent as the `model` field; users can fill any model id supported by the relay.
Polling stops when the response has `is_final=true`; successful tasks return
`state:"success"` and `result_url`, while failures return `state:"failed"` and
an `error` string.

Provider params can adapt small protocol differences without hardcoding model
ids. Use `payload_fields` to remap logical fields such as `resolution` to
`params.size`, `resolution_output` or `size_output` for provider casing, and
`supported_ratios` plus `default_ratio` when a provider only accepts a narrower
ratio set. Video duration is user supplied by default; only configure
`supported_durations` for models that truly require fixed duration choices. Use
`duration_min` and `duration_max` when the provider supports a continuous range.

Provider `params.image_transport` controls JSON image inputs for adapters that
send image URLs or data URLs. The default is `data_url`: local project images
are converted to data URLs, while existing `http(s)` image references pass
through unchanged. Set `image_transport: "public_url"` plus `public_base_url`
when the provider must fetch project images through a public URL. Multipart and
upload-first protocols keep using their protocol-specific upload path.

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
- `fields.references`: image references with `role:"visual_reference"` when the
  chosen model/path needs source images.

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
