---
name: video_production
description: 视频制作入口指南。用于选择通用视频制作 workflow、补齐运行输入、选择 prompt skill；视频请求默认使用 general_short_drama_workflow。
category: workflow
applies_to: 视频制作 视频工作流 默认视频流程 workflow template general_short_drama_workflow 文生视频 15秒分段 text image video
---

# 视频制作入口指南

## 模型摘要

- `video_production` 是通过 `skill.search` / `skill.get` 读取的 workflow skill，不是 workflow 模板，也不是 spec 来源；它只是视频制作入口规则和模块 skill 索引。
- 普通“制作视频/短剧/文生视频”默认直接使用模板 `general_short_drama_workflow`（显示名“通用视频制作工作流”），把用户输入传给 `workflow.run_step`、`workflow.run_next` 或 `workflow.run_all` 运行。
- 工作流请求通过 `workflow_spec` 选择器返回现有模板引用；默认路径返回 `general_short_drama_workflow`，不重新生成 spec。
- 默认视频运行模式只使用现有模板引用、补齐输入并运行 workflow。
- 用户主动要求查询或选择模板时，优先委派 `workflow_spec` 选择器；只在需要展示列表时读取 workflow 模板目录。
- 运行前补齐阻塞输入：剧情/主题 `plot`、单集总时长 `duration_seconds`；可选输入是视觉风格 `style`、视频类型 `video_type`、集数 `episode_count`、每段时长 `segment_seconds` 和画幅 `aspect_ratio`。
- workflow 图片/视频 step 可在 `fields.aspect_ratio` 写完整输入绑定 `{{ inputs.aspect_ratio }}`，运行器按本次流程输入解析；总时长和分段时长必须连续核算，末段终点精确等于 `duration_seconds`。
- 模板里的 V2 逻辑步骤已经带 `prompt`；运行期编译成私有提示词阶段执行，不把完整 prompt skill 原文塞进主 Agent。
- prompt 模块索引用于模板维护、局部改提示词或 standalone 节点：剧本 `script_writing`，人物图 `character_prompt`，场景图 `scene_prompt`，宫格分镜 `shot_grid_prompt`，视频提示词 `video_prompt`，故事模板图 `story_template_method`。
- 每个节点都是独立任务单元；`task` 只记录进度；生产依赖写节点 `fields.references`，图片引用用 `role:"visual_reference"`，文字上下文用 `role:"context"`，直接采用已有图片用 `role:"source_image"`。
- 最终 image/video prompt 提到参考图时使用候选表给出的精确 `@参考图标签`，例如“人物沿用 `@凌澈人物参考图`，镜头沿用 `@宫格分镜图`”。后端把标签绑定到稳定的图片节点 ID，参考图列表换序后仍指向同一张图。
- `skill.get(detail="full")` 返回的正文是指南内容；`path` 只做诊断来源，不作为 `file.read_text` 目标。

## 默认模板

默认模板：

```text
template_id: general_short_drama_workflow
name: 通用视频制作工作流
```

默认运行方式：

```text
workflow.run_all(
  template_id="general_short_drama_workflow",
  inputs={
    "plot": "...",
    "duration_seconds": 15,
    "episode_count": 1,
    "segment_seconds": 15,
    "style": "...",
    "video_type": "短剧",
    "aspect_ratio": "16:9"
  }
)
```

如果用户只说“制作一个视频”，先补问剧情/主题和时长。用户已给足剧情和时长时，可以直接运行默认模板；风格、视频类型、集数和每段时长缺失时使用模板默认值或按用户上下文填写。

## 模板匹配规则

- 用户说“用模板/查模板/有没有类似流程”时，查询模板候选。
- 默认可用 workflow 模板是 `general_short_drama_workflow`。
- 用户给出 workflow skill 或一段流程说明时，先用 skill 摘要和目标查可复用模板；主流程能由通用模板承接时直接复用。
- no hit：`workflow_spec` 返回 blocked，并说明缺少哪类模板。

## 输入和运行

运行 graph workflow 前，主 Agent 读取项目状态和流程运行态：

```text
project.get_state
workflow.runtime_status
```

填写或更新输入时，把事实放进 `inputs`，不要写进模板本体。多个流程并行运行时带 `instance_id`，避免覆盖别的流程胶囊。

常用运行：

- 开始完整流程：`workflow.run_all(template_id="general_short_drama_workflow", inputs=...)`
- 继续下一步：`workflow.run_next(template_id="general_short_drama_workflow", instance_id=...)`
- 指定步骤：`workflow.run_step(template_id="general_short_drama_workflow", step_id=..., inputs=..., instance_id=...)`

视频 step 的 `execution=auto` 表示流程运行时继续生成视频；`execution=manual` 表示只准备提示词和视频节点，等待用户在该节点上点击生成。以流程编辑器“手动运行”保存的选择为准。

遇到失败、阻塞或依赖未完成时，以工具返回的 `runtime`、`progress`、`waiting_on`、`error_kind` 为准。

## Prompt Skill 索引

这些 skill 只作为写法来源，不是默认运行时反复读取的大上下文：

| 阶段 | 默认内置 skill | 用途 |
| --- | --- | --- |
| 剧本 | `script_writing` | 剧本、分段和基础规划 |
| 人物参考图 | `character_prompt` | 主要人物或配角参考图 |
| 场景参考图 | `scene_prompt` | 无人物或低人物干扰的场景参考 |
| 宫格分镜 | `shot_grid_prompt` | 分镜规划和宫格分镜图 |
| 视频提示词 | `video_prompt` | 最终视频提示词 |
| 故事模板图 | `story_template_method` | 故事模板图/视觉开发板 |

用户自定义 prompt skill 优先于内置 skill。模板维护时，把稳定写法写进对应公开 step 的 `prompt`。

## Standalone 节点

用户只要求一个单独图片、一个直接文生视频、或明确不需要完整流程时，可以不启动 graph workflow，直接创建/更新 `text`、`image` 或 `video` 节点并运行。

Standalone 节点运行使用 `node.run`；graph workflow 运行使用 `workflow.run_step`、`workflow.run_next` 或 `workflow.run_all`，由 workflow runner 按步骤调用节点 runner。

Standalone 节点仍要写清：

- `fields.purpose`
- `fields.stage`
- `fields.references`
- `fields.video_mode` 与媒体引用一致：留空可由后端推断；显式 `text_to_video` 不携带图片、视频或音频参考
- prompt 中每个需要明确指代的参考图都使用对应的精确 `@参考图标签`
- image/video 的可执行 prompt
- video 的 `duration_seconds`、`aspect_ratio` 和 `production_path`

## 验收

最终汇报或运行下游前确认：

- text 节点有非空 `fields.content` 或 output。
- image/video 节点有非空 prompt。
- `duration_seconds`、`aspect_ratio` 和用户硬约束已写入。
- `fields.references` 指向真实上游 node、asset 或上传路径。
- workflow runtime 的 `progress` 没有未处理的 failed/running 状态。

阶段结果复杂或用户要求检查时，用 `agent.review` 做只读复核。
