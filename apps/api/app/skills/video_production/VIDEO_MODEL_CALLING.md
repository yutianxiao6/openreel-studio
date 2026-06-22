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

## Supported Models

| Model | API format | Path | Source images | Resolutions |
| --- | --- | --- | --- | --- |
| `grok-1.5-video-15s` | `grok_1_5` | image-to-video only | exactly 1 image | `480p`, `720p` |
| `grok-imagine-video-1.5` | `xai_video` | official xAI image-to-video | exactly 1 image | `480p`, `720p` |
| `doubao-seedance-2-0-260128` | `volcengine_ark` | text-to-video or image-to-video | 0 or more images | `480p`, `720p`, `1080p` |
| `doubao-seedance-2-0-fast-260128` | `volcengine_ark` | text-to-video or image-to-video | 0 or more images | `480p`, `720p` |
| `doubao-seedance-2-0-mini-260615` | `volcengine_ark` | text-to-video or image-to-video | 0 or more images | `480p`, `720p` |

`2k` and `4k` are UI placeholders for future video models. Do not write them to
`video.fields.resolution` for the current supported models.

## Node Fields

For video nodes, use:

- `fields.model`: one of the supported model ids above, or leave empty to use
  the active video provider.
- `fields.aspect_ratio`: `16:9` or `9:16`.
- `fields.resolution`: one of the supported resolution values for the selected
  model.
- `fields.references`: image references with `role:"visual_reference"` when the
  chosen model/path needs source images.

For `grok-1.5-video-15s` and `grok-imagine-video-1.5`, keep exactly one source
image reference on the video node. If multiple images are needed for planning,
first choose or create a single final source image, then reference only that
image from the video node.

## Repair Rules

If a video call fails because of source image count, resolution, or provider
format:

1. Read this file.
2. Update the original `video` node fields; do not create a replacement node
   unless the user asked for a new version.
3. Rerun the original node after the model, resolution, aspect ratio, prompt,
   and references match the table above.
