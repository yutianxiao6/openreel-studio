# 模型接入

[English](../en/model-providers.md) · [中文文档首页](../README.md)

## 配置分层

OpenReel Studio 把“用户账号配置”和“HTTP 协议定义”分开：

1. `config/runtime.jsonc` 保存 Provider 名称、Base URL、API Key、模型名、启用状态和协议 ID。
2. `config/*_provider_protocols/catalog.json` 保存请求路径、字段映射、轮询和结果提取规则。

这样同一个协议可以被多个账号复用，切换中转站时不需要复制整段协议 JSON。

## LLM Provider

LLM 配置通常包含：

- LiteLLM provider；
- 模型名；
- API Key；
- 可选 Base URL；
- 上下文窗口和最大输出；
- strong、balanced、small 等模型层级或任务映射。

Agent、评审、压缩和辅助任务可以使用不同模型。配置完成后先用设置页测试，再进行真实工作流运行。

## 媒体 Provider

图片、视频和音频 Provider 使用以下声明式格式：

- `image_http_v1`
- `video_http_v1`
- `audio_http_v1`

运行配置只保存对应的 `image_protocol_id`、`video_protocol_id` 或 `audio_protocol_id`。协议正文必须放在目录文件中，后端不会从单个 Provider 配置里读取一份私有协议对象。

## Base URL 规则

Base URL 是用户配置的真实 API 根地址，后端按字面使用：

```text
Base URL: https://relay.example/v1
协议 path: /videos
最终地址: https://relay.example/v1/videos
```

如果服务商要求 `/v1`、`/v2` 或 `/api/v3`，把版本放在 Base URL 中。协议 `path` 只写资源路径，例如 `/files`、`/videos`、`/generations`，不要重复版本：

```text
错误: Base URL https://relay.example/v1 + path /v1/videos
正确: Base URL https://relay.example/v1 + path /videos
```

后端不会擅自删除 Base URL 后缀。少数协议需要独立上传域名时，用协议声明的 `upload_base_url` 等参数明确配置。

## 协议目录

```text
config/
  image_provider_protocols/catalog.json
  video_provider_protocols/catalog.json
  audio_provider_protocols/catalog.json
```

每个目录文件包含版本号和 `protocols` 映射。常见字段：

| 字段 | 作用 |
| --- | --- |
| `id` | 稳定协议 ID。 |
| `request` | 提交请求的方法、路径、请求体和响应提取。 |
| `upload` | 可选上传步骤。 |
| `poll` | 可选异步轮询。 |
| `defaults` | 协议默认参数。 |
| `capabilities` | 比例、时长、分辨率、首尾帧等能力。 |

模型支持的比例、时长和分辨率应来自配置。视频时长未配置时，产品使用 5–15 秒默认范围，而不是为每个模型硬编码一组随机值。

## 添加一个 Provider

1. 在设置页选择媒体类型。
2. 填写名称、Base URL、API Key 和模型名。
3. 选择与服务商接口匹配的协议 ID。
4. 填写协议要求的额外参数。
5. 保存并运行连接测试。
6. 用最小提示词做一次真实生成。

不要只根据模型名称猜协议。应对照服务商 API 文档检查提交路径、鉴权、请求体、异步状态和结果 URL。

## 排障顺序

1. 检查 Provider 是否启用。
2. 检查 Base URL 是否包含服务商要求的版本前缀。
3. 检查协议 path 是否重复版本。
4. 检查模型名和协议 ID。
5. 检查 API Key 是否属于同一中转站。
6. 查看节点错误中的 endpoint、HTTP 状态和响应摘要。
7. 对异步视频检查 job id、轮询地址、成功状态和结果提取字段。

`bad response body` 往往表示接口返回格式与协议提取规则不一致，不应通过清空节点预览来处理。修复协议后重试，节点仍应保留最近一次成功结果。

## 安全

- API Key 只保存在本地运行配置或部署 Secret 中。
- 不要把 `runtime.jsonc` 的真实密钥、服务商完整响应或请求头贴到公开 Issue。
- 提交协议示例时使用 `example.test` 和占位值。
- 新协议应配套 endpoint、请求体、轮询、错误和 Base URL 合同测试。
