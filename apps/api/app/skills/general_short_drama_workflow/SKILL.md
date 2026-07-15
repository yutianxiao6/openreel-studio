---
name: general_short_drama_workflow
description: "通用视频制作工作流。用于默认视频制作路径：输入剧情和基础参数，生成总剧本、逐段剧本文本、人物参考图、场景参考图、宫格分镜、视频提示词和最终视频节点。"
category: workflow
applies_to: "通用视频制作 默认视频制作 workflow 宫格分镜 人物参考 场景参考 视频提示词 最终视频"
---

# 通用视频制作工作流

## 用途

这是内置默认视频制作模板说明。用户没有指定自定义流程、指定模板或指定 workflow skill 时，优先复用模板 `general_short_drama_workflow`，中文名为“通用视频制作工作流”。

模板只保存 V2 可复用结构：输入字段、逻辑步骤、依赖、循环、`uses` 引用关系和每一步的 `prompt` 合同。本次剧情、剧本文字、人物描述、图片 prompt、视频 prompt 成品在运行实例中生成，不写死进模板。

执行时由主 Agent 填写或更新流程输入，再调用 `workflow.run_step`、`workflow.run_next` 或 `workflow.run_all`。运行器按依赖执行各步骤；中间规划和集合整理保存在 workflow runtime，用户可见产物显示为画布节点。

## 输入字段

- `plot`：故事主题、简要情节或完整剧本。
- `style`：视觉风格、时代质感、摄影质感和整体情绪。
- `video_type`：视频类型，例如短剧、电影短片、预告片、广告片。
- `episode_count`：集数，默认 1。大于 1 时启用分集规划。
- `duration_seconds`：整支视频或单集目标时长，默认 15 秒。
- `segment_seconds`：每段目标时长，默认 15 秒；流程据此自动计算段数。
- `aspect_ratio`：画面比例，默认 `16:9`。

图片和视频步骤的 `fields.aspect_ratio` 绑定 `{{ inputs.aspect_ratio }}`；投影和运行时使用本次流程输入，不把模板保存时看到的默认比例写死。剧本和制作规划必须让首段从 0 秒开始、相邻段首尾相接、末段精确结束于 `duration_seconds`，各段时长之和与总时长完全一致。

## 默认流程

公共流程：

```text
inputs -> episode_plan? -> script -> production_plan -> character_images
```

每集每段流程：

```text
production_plan.segments[] -> segment_script -> scene_plan -> scene_reference -> frame_plan -> (storyboard -> storyboard_review) feedback loop -> final_video
```

`episode_plan` 只在多集时运行。分段数量由 `duration_seconds / segment_seconds` 向上取整得到。`script` 生成完整剧本正文；`production_plan` 输出统一风格、主要人物和逐段计划；每个 `segment_script` 再整理当前段独立正文。`scene_plan` 输出当前段场景与次要人物，`frame_plan` 根据动作复杂度决定宫格数量。用户可见文本由公开步骤的画布输出生成，图片和视频步骤会先运行私有提示词阶段。

## 可见产物

- `script`：完整剧本和分段结构，显示在画布。
- `segment_script`：当前段剧本文本，按 `segment_id` 展开并显示在画布。
- `character_image`：主要人物参考图，按 `character_id` 展开并显示在画布。
- `scene_reference`：当前段场景参考图，显示在画布。
- `storyboard`：当前段宫格分镜图，显示在画布。
- `final_video`：当前段最终视频节点，使用私有提示词阶段生成的正文，并参考本段 `storyboard`、`scene_reference` 和选中的人物图。

`final_video.execution` 决定流程运行方式：`auto` 会继续生成，`manual` 只准备视频提示词和画布节点，等待用户在视频节点上点击生成。流程编辑器中的“手动运行”直接保存为这一字段。

规划类步骤如 `episode_plan`、`production_plan`、`scene_plan`、`frame_plan` 和 `storyboard_review` 保存在运行态，供下游节点读取。`script` 和每个 `segment_script` 的可读正文会同步到画布文本节点；`final_video__prompt` 是由公开 `final_video.prompt` 编译出的私有阶段；集合类结构由后端根据步骤 schema 自动处理。

## Prompt Skill

模板步骤使用这些内置 prompt skill：

- 剧本与规划：`script_writing`
- 人物参考图：`character_prompt`
- 场景参考图：`scene_prompt`
- 宫格分镜：`shot_grid_prompt`
- 视频提示词：`video_prompt`

节点运行阶段以模板公开步骤里的 `prompt` 为准，运行器会生成私有提示词阶段。需要局部改提示词时，修改对应步骤的 `prompt`，再重跑该步骤及受影响下游。

## 分镜审核反馈循环

默认模板把分镜生成与分镜审核放进通用 V2 有界反馈循环。循环不是分镜专属协议：任何候选产物和结构化审核都可以使用同一合同。

- 循环使用固定整数 `foreach.count` 限制最大尝试次数，并用 `foreach.until` 读取本轮终点审核步骤的声明输出字段；默认分镜审核读取 `steps.storyboard_review.output.score >= 80`。
- `storyboard_review` 依赖 `storyboard`，并通过 `uses: [{"from":"storyboard","as":["vision"]}]` 接收真实图片像素。审核输出声明 `score`、`dimension_scores`、`reason`、`issues` 和 `regeneration_instruction`；模板按剧情主题、画面表达、镜头语言、轴线与空间连续、构图与调度、动作节奏、角色场景连续、技术可用性八个维度严格复核，重大剧情错配、无解释越轴或不可用构图不能达到 80 分。协议本身不固定这些业务字段。
- `storyboard` 的提示词包含 `{{ previous }}`。首轮该值为空对象；后续轮次收到上一轮完整审核对象，并逐项落实原因、问题和修改要求后重写完整图片提示词。
- 依赖保持正向：候选产物到审核步骤，不添加审核到候选产物的公共反向边。运行器负责逐轮串行、反馈注入和选择最后一轮通过产物。
- 达标后下游 `final_video` 及其私有提示词阶段依赖整个循环并使用通过轮次。最大次数仍未达标、门控字段缺失或类型无效时停止下游，不把无效结果当成普通低分继续运行。
- 外层 `segment_production` 使用 `foreach.key: "segment_id"`。内层审核循环中的 `scene_reference`、`storyboard` 等逻辑引用先按共同 `segment_id` 隔离，再按当前 `attempt` 选择；因此每段分镜只接收本段场景，每个审核只看本段当前分镜，每段视频只使用本段最后通过的分镜。画布投影和真实运行复用同一作用域解析。
