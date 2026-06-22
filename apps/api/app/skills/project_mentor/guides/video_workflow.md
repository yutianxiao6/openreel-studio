---
topic: video_workflow
description: Legacy project mentor pointer for node-first video production
---

# 通用视频制作流程

当前视频制作走节点优先流程。普通图片/视频创作先读取
`skill.video_production`，再直接创建或更新 `text` / `image` / `video`
节点。这个文件只作为 `project_mentor` 的历史入口和导航，不再承载完整
业务教程。

## 优先级

1. 用户当前明确要求。
2. 用户点名的 skill 或自定义完整流程。
3. `skill.video_production` 的节点优先默认流程。

用户 skill 或自定义流程可以改变生成视频的方法和步骤。系统仍需满足
工具、安全、节点类型和最新用户指令。已读取的 skill 是当前制作合同，
不是灵感参考；自由发挥只限用户和 skill 都没有规定的创意细节，并写
清模型假设。

## 默认骨架

没有用户自定义流程时，默认骨架是：

详细剧本 text -> 主要人物图 image -> 分集/分段故事 text（需要时） ->
主场景图 image -> 分镜/首尾帧/故事模板图 image -> video 节点。

剧本、分集和分段 text 只写故事情节、动作、对白和情绪推进，不写运镜、
景别、构图或 prompt。15秒及以内通常单段，不补问分集分段；1集不建分集
节点，1段不建分段节点。超过15秒按约15秒上限拆 segment，每段写段落故事。
多集内容先写每集故事，再对每集按 segment 递归。

## 节点表达

- `text`：详细剧本、分集故事、分段故事、检查记录。
- `image`：人物设定集+3视图、无人物场景四宫格四视图、分镜图或故事模板图、首尾帧、风格板。
- `video`：文生视频、图生视频、分镜图生视频、首尾帧视频或最终视频目标。

使用 `parent_node_id` 做 UI 分组；使用 `fields.references` 表达制作依赖。
需要区分图片用途时使用 `{ref, role}`：`visual_reference` 表示参考生成，
`source_image` 表示 image 节点直接采用现有图片作为输出。后端会按这些字段自动创建画布连线，节点创建后会实时展示在画布上。

## 补问

如仍缺主题、人物、场景、视觉风格、关键动作节拍、画幅、参考素材或
硬约束等阻塞事实，用 `interaction.request_input` 补问最多 6 个关键问题。
用户继续自定义时先吸收修改并继续给出修订确认；用户说“全权决定/模型发挥”时，把缺项写成模型假设继续建节点。

## Prompt

图片和视频 prompt 写法、参考图规则、分镜、首尾帧和故事模板图规则
统一放在 `skill.video_production`。不要把 prompt 模板检索作为默认
制作步骤。
