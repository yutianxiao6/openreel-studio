NAME = "core_rules"
TRIGGER = "always"
TIER = "s"
ORDER = 30

PROMPT = """\
# Project Rules

- The shared canvas is creative truth; user and Agent nodes have equal authority.
- Numbered targets (`#0`/`0`) use `node.get(node_id)`; named/unclear use `node.list(query|regex)`; broad uses `node.list(limit=0)`; batch details use `node.get(node_ids)`.
- Draft fields carry tmp/purpose/stage; ask only for fact gaps/conflicts.
- Node work uses active skill, `task.*`, `node.*`; dependencies use `parent_node_id` and `fields.references`.
- Before run/report, check latest user+skill; self-check/`agent.review`, then fix same node.
- State/retry answers use state and `error_kind/hint/model_feedback`.
- Destructive actions need current request plus structured confirmation.
"""
