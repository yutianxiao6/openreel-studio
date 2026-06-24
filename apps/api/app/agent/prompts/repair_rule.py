NAME = "repair_rule"
TRIGGER = "failure"
ORDER = 31
TIER = "h"

PROMPT = """\
# Node Repair

历史失败节点只是背景提醒。Repair only when latest user asks to continue/fix/retry/regenerate or this turn just failed.

- Start with `node.get` for known failed nodes; use `node.list` for real upstream/downstream nodes.
- Missing dependencies, prompt, refs, storyboard, keyframes, or story-template input reuse upstream output before new nodes.
- If upstream is absent, follow active/user skill; default image/video repair uses `skill.search -> skill.get -> node.create -> node.run`.
- Fix local fields with `node.update`, then rerun the original node with `node.run(action='force')` when appropriate.
- Report blocked/failed while status is failed/pending/running or output is empty.
"""
