---
name: video-production
description: 规划并执行 OpenReel 视频制作：选择现有 workflow 模板、补齐剧情与时长等输入、选择所需提示词 Skill，再运行文本、图片和视频节点。用户要求制作视频、短剧、文生视频、图生视频、继续或修复视频工作流时使用；默认模板是 general_short_drama_workflow。
---

# 视频制作入口指南

## 模型摘要

- `video-production` 由自动 Skill 目录匹配，通过 `skills.list` / `skills.read` 读取；它不是 workflow 模板或 spec 来源，只提供视频制作入口规则和模块 Skill 索引。
- 普通“制作视频/短剧/文生视频”默认使用模板 `general_short_drama_workflow`（显示名“通用视频制作工作流”），通过 `tool.search`、`tool.describe` 和 `tool.execute` 调用 deferred `workflow.run_step`、`workflow.run_next` 或 `workflow.run_all`。
- 工作流请求通过 `workflow_spec` 选择器返回现有模板引用；默认路径返回 `general_short_drama_workflow`，不重新生成 spec。
- 默认视频运行模式只使用现有模板引用、补齐输入并运行 workflow。
- 用户主动要求查询或选择模板时，优先委派 `workflow_spec` 选择器；只在需要展示列表时读取 workflow 模板目录。
- 运行前补齐阻塞输入：剧情/主题 `plot`、单集总时长 `duration_seconds`；可选输入是视觉风格 `style`、视频类型 `video_type`、画面制作模式 `visual_plan_mode`、集数 `episode_count` 和每段时长 `segment_seconds`。`visual_plan_mode` 默认 `storyboard`，用户可在前端改为 `story_template`。
- workflow spec 保持可移植，只描述结构、提示词、依赖和业务字段。模型、画幅、清晰度、画质等媒体产物参数由前端运行配置提供；总时长和分段时长必须连续核算，末段终点精确等于 `duration_seconds`。
- 模板里的 V2 逻辑步骤已经带 `prompt`；运行期编译成私有提示词阶段执行，不把完整 prompt skill 原文塞进主 Agent。
- prompt 模块索引用于模板维护、局部改提示词或 standalone 节点：剧本 `script-writing`，人物图 `character-prompt`，场景图 `scene-prompt`，宫格分镜 `shot-grid-prompt`，视频提示词 `video-prompt`，故事模板图 `story-template-method`。
- 每个节点都是独立任务单元；`task` 只记录进度；生产依赖写节点 `fields.references`，图片引用用 `role:"visual_reference"`，文字上下文用 `role:"context"`，直接采用已有图片用 `role:"source_image"`。
- 最终 image/video prompt 提到参考图时使用候选表给出的精确 `@参考图标签`，标签沿用完整画布标题并保留其中的 `|`、`｜`、空格、书名号等字符，例如“人物沿用 `@《回头》主角｜15岁少年`，镜头沿用 `@宫格分镜图`”。后端把标签绑定到稳定的图片节点 ID，参考图列表换序后仍指向同一张图。
- `fields.director_capture=true` 的图片是导演台构图参考，只继承人物/物体站位、朝向、姿态、比例、遮挡、景别和机位；正式分镜同时引用人物图与场景图重绘，不保留白模、色块、网格或编辑器外观，也不把构图参考自动当作视频首帧。
- 当前轮通过 `$video-production` 或带精确 path 的结构化 Skill 引用显式选择时，正文会在模型调用前以 `<skill>` 块注入；否则先用 `skills.list(authority={"kind":"orchestrator"})` 获取精确 handle，再用 `skills.read` 读取 `main_resource`，沿 `next_cursor` 到 EOF。引用资源继续使用同一 authority/package；`skill://` 是 source locator，不是文件路径。
- 只有用户要求诊断 UMA 视频调用、协议、target 或恢复行为时，再读取 `references/video-model-calling.md`。

## 默认模板

默认模板：

```text
template_id: general_short_drama_workflow
name: 通用视频制作工作流
```

默认运行方式：

```text
tool.execute(
  name="workflow.run_all",
  input={
    "template_id": "general_short_drama_workflow",
    "inputs": {
    "plot": "...",
    "duration_seconds": 15,
    "episode_count": 1,
    "segment_seconds": 15,
    "style": "...",
    "video_type": "短剧",
    "visual_plan_mode": "storyboard"
    }
  }
)
```

如果用户只说“制作一个视频”，先补问剧情/主题和时长。用户已给足剧情和时长时，可以直接运行默认模板；风格、视频类型、画面制作模式、集数和每段时长缺失时使用模板默认值或按用户上下文填写，其中画面制作模式默认宫格分镜。

## 模板匹配规则

- 用户说“用模板/查模板/有没有类似流程”时，查询模板候选。
- 默认可用 workflow 模板是 `general_short_drama_workflow`。
- 用户给出 workflow Skill 或一段流程说明时，先用自动目录中的 name/description 和目标查可复用模板；主流程能由通用模板承接时直接复用。
- no hit：`workflow_spec` 返回 blocked，并说明缺少哪类模板。

## 输入和运行

运行 graph workflow 前，主 Agent 读取项目状态和流程运行态：

```text
project.get_state
workflow.runtime_status
```

填写或更新输入时，把事实放进 `inputs`，不要写进模板本体。多个流程并行运行时带 `instance_id`，避免覆盖别的流程胶囊。
`workflow.runtime_status` 只返回有界的步骤页、运行实例索引页和已保存输入摘要；
步骤较多时按 `runtime.steps_page.next_offset` 继续。已保存输入会由 runner 自动合并，
不需要从状态结果复制大段剧本再传回运行工具。

常用运行：

- 开始完整流程：`tool.execute(name="workflow.run_all", input={"template_id":"general_short_drama_workflow","inputs":...})`
- 继续下一步：`tool.execute(name="workflow.run_next", input={"template_id":"general_short_drama_workflow","instance_id":...})`
- 指定步骤：`tool.execute(name="workflow.run_step", input={"template_id":"general_short_drama_workflow","step_id":...,"inputs":...,"instance_id":...})`

视频 step 的 `execution=auto` 表示流程运行时继续生成视频；`execution=manual` 表示只准备提示词和视频节点，等待用户在该节点上点击生成。以流程编辑器“手动运行”保存的选择为准。

遇到失败、阻塞或依赖未完成时，以工具返回的 `runtime`、`progress`、`waiting_on`、`error_kind` 为准。

## Prompt Skill 索引

这些 skill 只作为写法来源，不是默认运行时反复读取的大上下文：

| 阶段 | 默认内置 skill | 用途 |
| --- | --- | --- |
| 剧本 | `script-writing` | 剧本、分段和基础规划 |
| 人物参考图 | `character-prompt` | 主要人物或配角参考图 |
| 场景参考图 | `scene-prompt` | 无人物或低人物干扰的场景参考 |
| 宫格分镜 | `shot-grid-prompt` | 默认视觉分支的分镜规划和宫格分镜图 |
| 视频提示词 | `video-prompt` | 最终视频提示词 |
| 故事模板图 | `story-template-method` | 可选视觉分支的故事模板图/视觉开发板、审核和看图转译 |

用户自定义 prompt skill 优先于内置 skill。模板维护时，把稳定写法写进对应公开 step 的 `prompt`。

## Standalone 节点

用户只要求一个单独图片、一个直接文生视频、或明确不需要完整流程时，可以不启动 graph workflow，直接创建/更新 `text`、`image` 或 `video` 节点并运行。

Standalone 节点运行使用 `node.run`；graph workflow 通过 `tool.execute` 调用 deferred `workflow.run_step`、`workflow.run_next` 或 `workflow.run_all`，由 workflow runner 按步骤调用节点 runner。

外部执行客户端运行媒体节点后，通过同一节点的服务端终态事件等待取得结果；供应商轮询由后台 UMA 承担，客户端不重复查询节点状态，也不因等待超时再次调用 `node.run`。

视频参考素材统一在 `fields.references` 中各写一次，不把同一图片重复写入 `reference_images` 或 `depends_on`。`video_mode=first_frame` 时，后端会把第一张已解析图片参考作为首帧；可见节点编号 `0` 可直接引用。

导演台截图进入画布后是普通 completed image 节点，带 `fields.director_capture=true` 和 `reference_usage="composition_only"`。制作正式分镜时，把它与人物参考图、场景参考图一起作为目标 image 节点的 `visual_reference`；prompt 明确指出导演台图只约束构图关系。只有用户明确选择该图作为视频首帧时，才将其用于 `video_mode=first_frame`。

Standalone 节点仍要写清：

- `fields.purpose`
- `fields.stage`
- `fields.references`
- `fields.video_mode` 与媒体引用一致：留空可由后端推断；显式 `text_to_video` 不携带图片、视频或音频参考
- 当前视频 target 支持原生声音时，`fields.generate_audio` 采用模型默认值；用户明确要求有声或静音时分别写 `true` 或 `false`
- prompt 中每个需要明确指代的参考图都使用对应的精确 `@参考图标签`
- image/video 的可执行 prompt
- video 的 `duration_seconds`、`aspect_ratio` 和 `production_path`

## 验收

最终汇报或运行下游前确认：

- text 节点有非空 `fields.content` 或 output；带 `fields.generation` 的长文本节点必须已由 `node.run` 完整生成，不能把 pending/failed 占位内容当成成品。
- image/video 节点有非空 prompt。
- workflow spec 已写入 `duration_seconds` 和用户硬约束，媒体画幅等产物参数来自前端运行配置；standalone 节点则写入自身可执行字段。
- `fields.references` 指向真实上游 node、asset 或上传路径。
- workflow runtime 的 `progress` 没有未处理的 failed/running 状态。

阶段结果复杂或用户要求检查时，用 `agent.review` 做只读复核。
