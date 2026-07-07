# 工作流编辑器设计方案 / Workflow Editor Design

## English Summary

The workflow panel is a dedicated editor for reusable executable workflows. The
creative canvas remains focused on user-visible products: scripts, character
images, scene references, storyboard images, video prompts, videos, and audio.

This design separates three views:

- Template structure: reusable workflow blocks, nested loops, dependencies, and
  layout hints without runtime status.
- Runtime instance graph: concrete expanded steps for the current project,
  including progress, output, failures, and run controls.
- Creative canvas: product nodes only.

Loop blocks stay collapsed at the current level. Users open a loop to edit its
child workflow once; every runtime instance reuses that child layout. Execution
dependencies, read-only context links, dynamic media references, visual ordering,
and previous-instance continuity are separate relationship types.

The goal is a workflow editor that is understandable to non-technical users
while preserving a precise backend protocol.

## 中文正文

## 目标

流程面板是一个独立的工作流编辑器，用来设计、微调、运行和复用可执行工作流。创作画布继续只展示用户真正关心的产物节点，例如剧本、人物图、场景图、分镜图、视频提示词、视频和音频。

这次改造解决两个核心问题：

- 动态循环块被平铺成普通节点，用户看到像并行段落。
- 模板结构、运行实例和画布产物混在一张图里，导致状态、依赖和中间节点都显得混乱。

目标体验是：

```text
模板结构：输入 -> 剧本 -> 人物规划 -> [按人物生成参考图] -> [按集/段制作]
循环内部：场景集合 -> 场景参考图 -> 宫格分镜规划 -> 宫格分镜图 -> 视频提示词 -> 故事模板图
运行实例：第1集第1段、第1集第2段... 按实例展开
创作画布：只显示最终产物节点
```

## 三层视图

### 1. 模板结构

模板结构展示可复用框架。这里显示的是作者 spec 或编译后的模板骨架，不显示运行状态。

模板结构包含：

- 普通步骤节点。
- 循环块节点，例如“按主要人物生成参考图”“按集/段制作”。
- 插件节点。
- UI-only 顺序线。
- 真实执行依赖线。
- 数据读取关系线，可默认弱化或隐藏。

循环块在顶层只显示为一个块。点击循环块进入内部子流程编辑器。

### 2. 运行实例

运行实例展示当前项目根据输入和上游输出展开后的具体步骤。这里可以显示运行状态、运行输出、失败原因和下一步按钮。

如果输入是 2 集 4 段，运行实例会显示 8 个段落实例。每个实例复用模板里的同一份子流程，不需要用户手动复制 8 份。

### 3. 创作画布

创作画布只展示产物。流程中间节点、循环控制块、集合规划块都留在流程面板里。

例如：

- 剧本节点可以显示到画布。
- 人物参考图、场景图、分镜图显示到画布。
- 人物集合、场景集合、分段制作循环块不显示到画布。

## 节点类型

流程编辑器节点分为五类：

- `step`：普通可执行步骤。
- `loop`：动态循环块，有 `repeat` 或 `foreach`，内部有子流程。
- `collection`：集合提取或整理步骤，例如“主要人物集合”。
- `plugin`：插件节点。
- `input`：运行输入节点。

前端不能只通过 `node_type=text/image/video` 判断 UI 形态。`node_type` 是产物类型，`kind` 或 `role` 才是流程编辑器形态。

## 连线类型

流程图需要区分不同关系，不能全部塞进 `depends_on`。

- `depends_on`：真实执行依赖。上游没完成，下游不能运行。
- `reads_from` / `context_refs`：读取上游数据或上下文。影响提示词上下文，但不一定决定主流程连线。
- `reference_selectors`：运行时选择图片、视频或资产引用。常用于“按出场人物选择人物图”。
- `layout_after`：只控制视觉顺序，不影响执行。
- `depends_on_previous`：循环实例之间的连续依赖，例如后一段参考前一段分镜图。

默认展示策略：

- 主线显示 `depends_on` 和 `layout_after`。
- `reads_from` 用细线或悬浮详情展示。
- `reference_selectors` 在详情里展示，运行实例可按需显示参考缩略图。
- 用户可以开启“显示全部关系”。

## 动态循环块

循环块不在顶层展开内部所有节点。顶层只显示循环块卡片。

示例：

```json
{
  "id": "episode_segments",
  "title": "按集/段制作",
  "kind": "loop",
  "layout_after": ["main_character_images"],
  "repeat": {
    "mode": "per_episode_segment",
    "episode_count": "episodeCount",
    "segment_count": "segmentCount"
  },
  "steps": []
}
```

点击循环块后进入子流程：

```text
次要人物集合 -> 场景集合 -> 场景参考图 -> 宫格分镜规划 -> 宫格分镜图 -> 视频提示词 -> 故事模板图
```

用户在子流程里拖动节点，位置保存到循环块的 `steps[].ui.position`。运行实例根据这套位置自动偏移排布。

## 通用分镜模板的正确展示

通用宫格分镜模板容易被错误展示成两段并行流程，通常是因为：

- `主要人物集合` 是文本集合步骤，用来得到角色列表。
- `主要人物参考图集合` 是 `foreach` 循环块，用角色列表动态生成多张人物图。
- `分段制作` 是另一个循环块，用集数和段数动态生成每段流程。
- `分段制作` 不应该执行依赖所有人物图。每段里的 `宫格分镜图` 应根据 `appearing_characters` 动态选择需要的人物图。

因此模板结构应展示为：

```text
人物与场景规划
  -> 主要人物集合
  -> [按主要人物生成参考图]
  -> [按集/段制作]
```

这里 `[按集/段制作]` 可以用 `layout_after=["main_character_images"]` 排在人物图循环块后面，但执行依赖仍保持为 `main_characters` 和 `plan_characters_scenes`。

## 手动编辑体验

模板结构模式：

- 拖动顶层节点和循环块。
- 连接真实依赖线。
- 在右侧详情里切换线的类型：执行依赖、读取关系、视觉顺序。
- 双击循环块进入内部子流程。
- 自动对齐只作用于当前层级。

循环内部模式：

- 显示面包屑，例如 `通用宫格分镜 / 按集/段制作`。
- 编辑这份子流程一次，所有运行实例复用。
- 支持新增、删除、移动、连接内部节点。
- 支持返回顶层。

运行实例模式：

- 默认按集/段分组。
- 每组可折叠。
- 单步运行和一键执行都从实例图触发。
- 运行输出按用户可读结构展示。

创作画布：

- 不展示循环块和 flow-only 中间节点。
- 只展示 `output.canvas=true` 的产物。
- 产物节点保留真实依赖关系，但默认使用干净连线模式。

## 后端 API 形态

现有 `/workflow/templates` 需要继续返回 legacy `steps`，同时新增结构化图数据。

建议新增字段：

```json
{
  "templates": [
    {
      "id": "grid_storyboard_workflow_spec",
      "name": "通用宫格分镜工作流 Spec",
      "author_graph": {},
      "compiled_graph": {},
      "runtime_preview": {}
    }
  ]
}
```

更稳妥的分阶段方式：

- `/workflow/templates` 保持轻量列表。
- `/workflow/templates/{id}` 返回模板详情、作者 spec 摘要、编译 spec 摘要和图结构。
- `/workflow/preview` 返回带输入和已知运行上下文的展开实例。

## 保存策略

保存时不能只保存前端平铺后的节点。

保存对象应包含：

- `author_spec`：用户/Agent 可继续编辑的源结构。
- `compiled_spec`：后端编译校验后的可运行结构。
- `ui_state`：当前层级的节点位置、折叠状态、默认显示关系类型。

如果用户直接导入 compiled spec，后端可以生成一个最小 author spec wrapper，保留原 compiled spec 作为 source。

## 实施阶段

### 阶段 1：协议与图数据

- 扩展 authoring spec 和 compiled spec 字段。
- 后端生成 `template_graph`，保留层级和循环块。
- 后端生成 `runtime_graph`，表示运行实例。
- legacy `steps` 保持兼容。

### 阶段 2：前端模板结构图

- 模板结构图只展示当前层级。
- 循环块可进入内部子流程。
- 模板结构不显示运行状态。
- 支持 `layout_after` 视觉顺序线。

### 阶段 3：运行实例图

- 运行实例按 repeat group 和 instance scope 分组。
- 节点详情展示运行输出。
- 单步运行和一键执行从实例图触发。

### 阶段 4：通用模板结构整理

- 把通用分镜模板整理成清晰的 `collection + loop + nested steps` 结构。
- `episode_segments` 使用 `layout_after` 表达视觉顺序。
- 分镜图使用 `reference_selectors` 按出场人物选择人物参考图。

### 阶段 5：workflow_spec 子 Agent 更新

- 子 Agent 优先写 authoring spec。
- 子 Agent 分批提交时按顶层、循环块、循环内部子流程提交。
- 主 Agent 只拿 artifact_ref、preview 和 validation，不读取完整 spec。

## 验收标准

- 模板结构里不出现“完成/未运行”等运行状态。
- 通用分镜模板顶层不再看起来像两条并行段落。
- 进入“按集/段制作”后能看到段落子流程。
- 用户拖动循环内部节点后，所有运行实例复用同一布局。
- 2 集 4 段运行实例能按 8 个段落分组展示。
- `depends_on` 不再承担 UI 排版职责。
- 旧 workflow spec 仍能导入和运行。
