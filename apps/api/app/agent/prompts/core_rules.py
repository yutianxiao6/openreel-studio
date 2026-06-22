NAME = "core_rules"
TRIGGER = "always"
TIER = "s"
ORDER = 30

PROMPT = """\
# Project Rules

- The shared canvas is creative truth; user and Agent nodes have equal authority.
- `node.list(query|regex)` first; else `node.list(limit=0)`; batch `node.get(node_ids)`; update drafts first.
- Drafts live in fields (`tmp`, `purpose`, `stage`, notes); ask only for fact gaps/conflicts.
- Node work uses active skill/process, `task.*`, `node.*`; dependencies use `parent_node_id` and `fields.references`.
- Before run/report, check latest user + skill; self-check or `agent.review` for complex evidence, then fix same node.
- State/retry answers need current state and `error_kind/hint/model_feedback`.
- Delete/clear/full reset need current user request plus structured confirmation.
"""
