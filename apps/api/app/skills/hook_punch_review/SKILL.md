---
name: hook_punch_review
tool_name: internal.hook_punch_review
description: 内部 helper：只针对钩子和爆点审稿，输出短反馈；不作为 Agent 工具注册
when_to_use: 仅供内部 service 或测试直接调用；Agent 应通过 node 审稿路径处理
tags: [drama, review, hook]
source: internal_helper
---

# hook_punch_review

调用内部审稿 runner 拿到全量审稿，但只挑出钩子和爆点相关字段返回，方便快速决策。当前不注册为 `skill.hook_punch_review`，避免绕过 `node.*` 路径。

## 触发示例

- "第 1 集钩子怎么样"
- "看下第 3 集爆点够不够"

## 输入

- `project_id` (必填)
- `episode_number` (必填)
