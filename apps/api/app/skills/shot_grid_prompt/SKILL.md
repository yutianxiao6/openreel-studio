---
name: shot_grid_prompt
description: 分镜宫格图提示词模块。用于 image 节点生成几宫格分镜图，按画格构图组织连续动作和镜头节奏。
category: prompt
applies_to: 分镜提示词 分镜图 宫格分镜 几宫格 镜头节奏 shot grid prompt
---

# 分镜宫格图提示词 Skill

用于 `image` 节点生成一张几宫格分镜图。它只处理分镜图片 prompt。

## 写法

prompt 开头直接写几宫格，例如“四宫格分镜图”或“六宫格分镜图”。接着写视觉风格、画面质感、色彩、光线和整体情绪。然后按从左到右、从上到下的阅读顺序，连续描述每一格的构图。

每一格聚焦一个关键 beat，写清景别、主体位置、前中后景、动作、视线方向、运动方向、人物相对位置、情绪和与下一格的连续性。画格内保持纯画面表达，依靠构图、动作和视线传达信息。

二宫格适合首尾帧或简单变化；四宫格适合 15 秒内的清晰起承转合；六宫格或九宫格适合动作更密、调度更复杂的段落。

## 推荐字段

- `fields.purpose`: `shot_grid`
- `fields.stage`: `storyboard_reference`
- `fields.aspect_ratio`: `16:9`
- `fields.resolution`: `2560x1440`
- `fields.quality`: `high`
- `fields.references`: 段落故事用 `context`，人物/场景图用 `visual_reference`

## 检查点

- 分镜顺序能还原故事节奏。
- 主体位置、视线和运动方向保持连续。
- 每一格都能支持后续 video prompt 的时间段描述。
