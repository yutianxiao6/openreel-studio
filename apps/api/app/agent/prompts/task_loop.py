NAME = "task_loop"
TRIGGER = "always"
ORDER = 25

PROMPT = """\
# Task Tracking

任务是轻量执行账本。

- Complex requests create a short outcome checklist.
- Use `task.create(items=[...], mode="sequential")` for staged work.
- Multi-node media, repair, retry, or user-tracked work creates tasks before content nodes; simple Q&A or one-node edits may skip tasks.
- Follow active skill; use `task.list` when continuing/repairing.
- Keep one active step: `task.update(status="in_progress")`; before `task.complete`, check output against user/active skill.
"""
