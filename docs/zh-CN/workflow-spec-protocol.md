# Workflow Spec 协议

OpenReel 只接受一种可移植工作流格式：`openreel.workflow.v2`。导入、导出、
模板存储和 Workflow Build Mode 都直接读写这份公开文档。编译后的执行阶段和
项目运行状态属于私有实现，不进入可复用 Spec。

## 公开文档

```json
{
  "schema": "openreel.workflow.v2",
  "id": "storyboard_video",
  "title": "分镜视频",
  "description": "根据剧情生成分镜和视频。",
  "tags": ["video"],
  "inputs": {
    "plot": {
      "type": "long_text",
      "label": "剧情",
      "required": true
    }
  },
  "steps": [
    {
      "id": "storyboard",
      "title": "分镜图",
      "kind": "image",
      "prompt": {
        "role": "分镜导演",
        "task": "根据 {{ inputs.plot }} 设计分镜。",
        "check": "保持人物和镜头方向连续。"
      }
    },
    {
      "id": "final_video",
      "title": "成片",
      "kind": "video",
      "needs": ["storyboard"],
      "prompt": {
        "task": "根据 {{ steps.storyboard.output }} 编写最终视频提示词。"
      },
      "uses": [
        {"from": "storyboard", "as": ["vision", "reference"]}
      ]
    }
  ]
}
```

根字段只有 `schema`、`id`、`title`、`description`、`tags`、`inputs`、
`steps`、`ui` 和带命名空间的 `extensions`。输入按 id 建立对象，可包含
`type`、`label`、`description`、`required`、`default`、`min`、`max` 和
`options`。

步骤类型只有 `text`、`object`、`collection`、`image`、`video`、`audio`、
`loop` 和 `plugin`。步骤可包含 `id`、`title`、`kind`、`description`、
`needs`、`prompt`、`output`、`fields`、`uses`、`when`、`execution`、
`on_error`、`foreach`、嵌套 `steps`、`plugin` 和 `ui`。未知字段会被拒绝。

## 数据与执行

数据路径统一使用 `inputs.<id>`、`steps.<id>.output` 和当前循环变量。
只有在数据路径没有表达顺序关系时才写 `needs`。运行条件是正向结构化条件：

```json
{"when": {"path": "inputs.episode_count", "op": "gt", "value": 1}}
```

`execution` 取 `auto` 或 `manual`；`on_error` 取 `stop` 或 `continue`。
重复任务只能使用一个 `loop` 步骤、嵌套步骤，以及唯一的
`foreach.items` 或 `foreach.count` 来源。

媒体引用统一写 `uses`。`vision` 会把解析后的真实图片像素交给提示词模型，
`reference` 会把媒体交给生成模型，`source` 会直接采用一个已有媒体输出。
同一来源可以同时使用 `vision` 和 `reference`。

Provider、模型 id、模型档位、API 地址、本次生成正文、运行状态、画布节点 id、
私有 runner 和内部提示词阶段都不是可移植字段，由运行器根据项目与用户配置解析。

旧工作流协议和作者字段不再受支持，导入前必须重写为 V2。
