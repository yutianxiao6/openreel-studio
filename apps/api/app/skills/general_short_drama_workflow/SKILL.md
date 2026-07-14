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
- `type`：视频类型，例如短剧、电影短片、预告片、广告片。
- `episodeCount`：集数，默认 1。大于 1 时启用分集规划。
- `durationSeconds`：整支视频或单集目标时长，默认 15 秒。
- `segmentSeconds`：每段目标时长，默认 15 秒；流程据此自动计算段数。

## 默认流程

公共流程：

```text
input -> episode_plan? -> script -> plan_characters_scenes -> main_characters -> main_character_images?
```

每集每段流程：

```text
segment_script -> segment_script_canvas -> minor_characters -> scene -> scene_reference -> plan_frames -> (storyboard -> storyboard_review) feedback loop -> video_prompt -> final_video
```

`episode_plan` 只在多集时运行。分段数量由 `durationSeconds / segmentSeconds` 向上取整得到。`script` 生成完整剧本正文；每个 `segment_script` 再把当前段剧本拆成独立正文，并由 `segment_script_canvas` 同步到画布。分镜格数由 `plan_frames` 提示词根据当前段动作复杂度决定，普通段落默认四宫格，复杂段落可增加。`minor_characters` 没有配角时可跳过。

## 可见产物

- `script`：完整剧本和分段结构，通过 `script_canvas` 显示在画布。
- `segment_script_canvas`：当前段剧本文本，按分段展开，显示在画布。
- `main_character_image`：主要人物参考图，按人物展开，显示在画布。
- `scene_reference`：当前段场景参考图，显示在画布。
- `storyboard`：当前段宫格分镜图，显示在画布。
- `final_video`：当前段最终视频节点，使用 `video_prompt` 正文，并参考 `storyboard`，显示在画布。

规划类步骤如 `episode_plan`、`plan_characters_scenes`、`main_characters`、`minor_characters`、`scene`、`plan_frames` 和 `video_prompt` 保存在运行态，供下游节点读取。`script` 和每个 `segment_script` 的可读正文会同步到画布文本节点；`video_prompt` 输出完整视频提示词正文；集合类结构由后端根据步骤 schema 自动处理。

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
- `storyboard_review` 依赖 `storyboard`，并通过 `uses: [{"from":"storyboard","as":["vision"]}]` 接收真实图片像素。审核输出声明 `score`、`reason`、`issues` 和 `regeneration_instruction`；协议本身不固定这些业务字段。
- `storyboard` 的提示词包含 `{{ previous }}`。首轮该值为空对象；后续轮次收到上一轮完整审核对象，并逐项落实原因、问题和修改要求后重写完整图片提示词。
- 依赖保持正向：候选产物到审核步骤，不添加审核到候选产物的公共反向边。运行器负责逐轮串行、反馈注入和选择最后一轮通过产物。
- 达标后下游 `video_prompt` 和 `final_video` 依赖整个循环并使用通过轮次。最大次数仍未达标、门控字段缺失或类型无效时停止下游，不把无效结果当成普通低分继续运行。
