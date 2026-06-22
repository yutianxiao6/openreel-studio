NAME = "rerun_rule"
TRIGGER = "rerun"
ORDER = 32

PROMPT = """\
# Rerun Nodes

Reuse the existing node for edits and retries.

- 历史 status=failed 节点不是本轮任务，除非最新用户消息要求修复、重试、继续或重新生成，否则不要主动重跑。
- With a known node_id, call `node.get`, update correctable fields or prompt with `node.update`, then `node.run(action='force')`.
- Without a target id, call `node.list`; story/fact changes update the relevant `text` node or create an explicit replacement.
- Media generation is triggered through `node.run`; repair provider, size, reference, missing_field, or dependency_missing errors from current node state.
- If local context is insufficient, read `skill.project_mentor(topic='node_repair_guide')` and continue on the original node.
"""
