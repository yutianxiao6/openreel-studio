NAME = "audit_rule"
TRIGGER = "create"  # 凡是含 生成/做/写/画/创建/创作 的任务都触发收尾审核
ORDER = 190

PROMPT = """\
# Delivery Audit

Audit real state before declaring complex creative work complete.

- Read `project.get_state` and `node.list`; inspect specific nodes when status or output matters.
- Check requested scope, updated user nodes, failed/pending/running nodes, whether each prompt 是否可执行, and whether references resolve.
- For complex media work, check against the active/user skill before reporting completion; reread the active/default skill when needed and use `agent.review` with evidence.
- Fix evidenced gaps before reporting: create missing required nodes, update wrong fields, or repair failed nodes in place.
"""
