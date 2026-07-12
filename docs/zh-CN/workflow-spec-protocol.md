# Workflow Spec 协议

[English](../workflow-spec-protocol.md) · [中文文档首页](../README.md)

OpenReel Workflow Spec 用于描述可复用创作流程。推荐作者层协议是 `openreel.workflow.authoring.v1`，后端会编译成画布运行使用的 `openreel.workflow.v1`。

## 最小作者层 Spec

```json
{
  "schema": "openreel.workflow.authoring.v1",
  "id": "storyboard_workflow",
  "title": "分镜工作流",
  "inputs": {
    "plot": { "type": "long_text", "label": "剧情", "required": true }
  },
  "steps": [
    {
      "id": "script",
      "title": "剧本",
      "kind": "text",
      "prompt": {
        "role": "编剧",
        "task": "把输入剧情写成分段剧本。",
        "output": "包含人物、场景和动作的可读剧本。",
        "check": "每一段都有明确画面变化。"
      },
      "output": { "canvas": true, "key": "script" }
    }
  ]
}
```

普通用户通过 Workflow Build Mode 搭建，不需要手写 JSON。

## 顶层字段

| 字段 | 作用 |
| --- | --- |
| `schema` | 协议版本。 |
| `id` | 稳定 ASCII 工作流 ID。 |
| `title` | 用户看到的标题。 |
| `inputs` | 运行前必须提供的值。 |
| `steps` | 可复用步骤。 |
| `required_capabilities` | 工作流依赖的引擎能力。 |
| `required_extensions` | 导入或运行前必须安装的扩展。 |
| `extensions` | 可选命名空间扩展元数据。 |

## 步骤字段

- `id`：稳定步骤 ID。
- `title`：用户可见名称。
- `kind`：输入、文本、集合、循环、插件、图片、视频、音频等作者层类型。
- `needs`：真实执行依赖。
- `for_each`：循环数据源。
- `item_name`：当前循环元素的局部名称。
- `references`：动态视觉或上下文参考选择器。
- `prompt`：当前步骤的结构化提示词。
- `output`：输出 key 以及是否创建可见画布节点。
- `fields`：写入画布节点的字段。
- `phase`、`group`、`ui`：可选展示信息。
- `extension_config`：步骤级命名空间扩展配置。

## Prompt 分段

`prompt` 支持 role/system、task/instruction、output 和 check。编译器生成稳定分段；每个运行实例只接收当前步骤需要的输入和上游结果。

## 画布产物与运行时中间态

可见产物：

```json
{ "output": { "canvas": true, "key": "storyboards" } }
```

只保留在运行时：

```json
{ "output": { "canvas": false, "key": "scene_plan" } }
```

运行时提供等价的 `canvas_output` 和 `runtime_only` 元数据。显式输出字段优先于旧 `surface` 和 `visibility`。

## 动态展开

重复结构只定义一次，再从输入或上游输出展开：

```json
{
  "id": "scene_image",
  "kind": "image",
  "for_each": "scene_plan.output.scenes",
  "item_name": "scene"
}
```

兼容层接受集合/list、repeat group 和字符串 `prompt_template` 等别名。编译后实例保留稳定 `template_step_id`，同时获得具体实例 ID。

每个 repeat group 必须通过 `for_each`、`repeat.count` 或协议支持的其他基数表达式定义来源。缺少基数时必须在运行前校验失败。

## 参考选择器

选择器可以从上游集合动态选择素材：

```json
{
  "references": {
    "characters": {
      "source": "frame_plan.output.appearing_characters",
      "candidates": "character_reference"
    }
  }
}
```

Runner 解析成具体候选，并写入可见节点的 `fields.references`。

## 扩展与兼容

核心协议保持稳定，可选能力通过命名空间 capability 和 extension 声明。未知可选扩展元数据会保留；未知且必需的能力或扩展会阻止导入和运行。

旧运行时 Spec 的 `node_type`、`depends_on`、`prompt_template`、`surface` 和 `visibility` 仍可读取。新工作流应使用作者层协议，由编译器生成运行字段。
