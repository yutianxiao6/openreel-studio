---
name: character_with_reference
tool_name: internal.character_with_reference
description: 内部 helper：基于一段参考图描述生成贴合视觉的人物设定；不作为 Agent 工具注册
when_to_use: 仅供内部 service 或测试直接调用；Agent 应走 skill.video_production -> node.create/node.update -> node.run
tags: [drama, character, reference]
source: internal_helper
---

# character_with_reference

把用户给的"参考图描述"塞进 requirements 里，让内部人物 runner 在生成时把视觉字段对齐到这段描述。当前不注册为 `skill.character_with_reference`，避免绕过 `node.*` 节点协议。

## 触发示例

- "按这个图给我一个女主：白衬衫、黑色长发、地铁站、清晨"
- "参考这张：30 岁左右、穿西装、办公楼、夜景，给我一个反派"

## 输入

- `project_id` (必填)
- `reference_description` (必填) — 一段视觉描述
- `role_type` (可选) — female_lead / male_lead / antagonist / supporting
- `name` (可选) — 想要的角色名
