NAME = "repair_rule"
TRIGGER = "failure"
ORDER = 31
TIER = "h"

PROMPT = """\
# Node Repair

历史失败节点只是背景提醒。Repair failures only when the latest user asks to continue/fix/retry/regenerate or this turn just failed.

- Start with `node.get` for the failed node; use `node.list` to inspect real upstream/downstream nodes.
- For `dependency_missing`, missing reference images, storyboard, first/last frame, or story-template input, reuse existing upstream output before creating new nodes.
- If required upstream is absent, follow the active/user skill; default image/video repair uses `skill.video_production -> node.create -> node.run`.
- Fix local fields with `node.update`, then rerun the original node with `node.run(action='force')` when appropriate.
- Report blocked/failed while status is failed/pending/running, output is empty, or readiness is false.
"""
