---
topic: video_workflow_story_template
description: 故事模板图驱动视频流程
---

# 故事模板图生视频流程

适用：复杂动作、打斗、空间调度、强美术方向，或需要一张高信息密度参考图统一角色、场景、动作线和镜头设计。

先读 `skill.story_template_method`，再使用本流程。
最终 `video` 节点写 `fields.production_path="image_to_video"`，并用
`fields.references` 指向故事模板图 `image` 节点，图片引用使用 `role:"visual_reference"`。

每段流程：

1. 读取分段剧本、人物、场景和动作调度需求。
2. 写一张故事模板图 prompt：角色锚点、空间布局、动作线、镜头节奏、美术风格、关键道具、光线和连续性。
3. 运行故事模板 `image` 节点。
4. 读取完成的故事模板图输出，并先看图或读取视觉分析；当前模型看不了图时明确说明看不了，不要假装看过。
5. 根据分段剧本和真实故事模板图内容写该段 `video` 提示词。
6. 为这一段创建并运行一个 `video` 节点。

故事模板模式是图生视频路径：最终视频提示词必须等故事模板图完成、看图或读取视觉分析后再写。
需要复用的提示词写法写到 skill；当前流程直接按 `skill.story_template_method` 写故事模板图和最终 video prompt。
