# Video model calling / 视频模型调用

OpenReel video generation uses Universal Model Adapter (UMA) exclusively.
OpenReel manages node state, background jobs, SSE progress, local materialization,
restart recovery, and a server-side terminal-node event wait for execution clients.
UMA owns provider HTTP requests, uploads, task polling, status interpretation, and
output extraction. An execution client holds one wait request instead of polling
node status or submitting the same run again.

OpenReel 的视频生成统一使用 Universal Model Adapter。OpenReel 负责节点状态、
后台任务、SSE 进度、本地落盘、重启恢复和面向执行客户端的服务端终态事件等待；
UMA 负责供应商 HTTP 请求、素材上传、任务轮询、状态解释以及结果地址提取。执行
客户端只保持一个等待请求，不轮询节点状态，也不重复提交同一次运行。

## Configuration layers / 配置分层

- `config/universal_model_adapter/protocols/*.json` defines wire contracts only:
  authentication, HTTP request mapping, upload operations, task polling, exact
  status fields, success/failure values, and artifact paths.
- `config/universal_model_adapter/video_targets/catalog.json` defines product
  targets only: model matching, display labels, modes, media limits, ratios,
  resolutions, durations, defaults, and additional base URL slots.
- `config/runtime.jsonc` stores provider connection data and a target reference.
  It does not contain inline protocol documents or response parsing paths.

- `protocols/*.json` 只定义 HTTP 线协议：认证、请求映射、上传、轮询、精确状态字段、
  成功/失败值和产物路径。
- `video_targets/catalog.json` 只定义模型目标能力：模型匹配、名称、模式、媒体数量、
  比例、分辨率、时长、默认值和附加 Base URL 槽位。
- `runtime.jsonc` 只保存连接信息和目标引用，不内嵌协议或响应解析路径。

Example / 示例：

```json
{
  "kind": "video",
  "name": "seedance-production",
  "base_url": "https://ark.cn-beijing.volces.com/api/v3",
  "api_key": "${VIDEO_API_KEY}",
  "model_name": "doubao-seedance-2-0-260128",
  "api_format": "universal_adapter",
  "is_active": true,
  "enabled": true,
  "params": {
    "uma": {
      "protocol_id": "volcengine.seedance-video-task",
      "operation": "video.generate",
      "target_profile_id": "volcengine.seedance-video-task:doubao-seedance-2-0-260128"
    }
  }
}
```

## Runtime contract / 运行合同

1. The video node supplies prompt, selected model, `video_mode`, duration,
   aspect ratio, resolution, and structured references. Each media source is
   declared once in `fields.references`; compatibility alias fields are not
   duplicated.
2. OpenReel resolves node/asset references to media sources and submits a UMA
   invocation. Visible node `0` resolves normally. In `first_frame` mode, the
   first resolved image is promoted to the UMA `first_frame` role when no
   explicit first-frame asset field is present.
3. UMA selects the declared target and protocol variant, validates capabilities,
   uploads media when required, creates the provider task, and polls it.
4. OpenReel persists the UMA invocation id, provider task id, and a credential-free
   resume descriptor. After a restart it calls `resume_task`; UMA continues polling
   without submitting the generation again.
5. An execution client may hold one OpenReel node-wait request while the background
   job runs; the request sleeps on project canvas events and does not poll the node.
6. UMA returns one normalized `InvocationResult`; OpenReel updates the video node,
   optionally downloads the result, publishes the terminal canvas event, and
   resolves the waiting request.

Codex-facing create, update, run, and wait responses contain compact identity,
status, task, URL, and error fields. Full prompts, adapter resume requests, poll
history, and media history remain persisted and require an explicit node read.

## Modes and references / 模式与参考素材

The selected target is the source of truth. Common normalized modes are:

- `text_to_video`: prompt only.
- `first_frame`: one first-frame image.
- `first_last_frame`: first and last frame images.
- `multimodal_reference`: target-declared image, video, and audio references.
- `video_edit`: source video plus target-declared references.
- `video_continuation`: source clip used for continuation when the target declares it.

Reference roles are normalized as `first_frame`, `last_frame`, `reference_image`,
`reference_video`, and `reference_audio`. The target schema validates counts and
mode compatibility before an external request is sent. Protocol variants map those
normalized roles to the provider's exact wire format.

## Failure handling / 错误处理

UMA returns normalized error fields such as `code`, `stage`, `field_path`,
`provider_status`, and `retryable`. OpenReel records and displays these fields.
Protocol mismatches are fixed in the UMA protocol document; capability mismatches
are fixed in the video target catalog; credentials and base URLs are fixed in
runtime provider configuration.
