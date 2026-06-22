NAME = "memory_write"
TRIGGER = "always"
ORDER = 150

PROMPT = """\
# Memory

Memory is long-lived context, not active project state.

- Use it only for stable facts or preferences.
- Runtime state owns current nodes, pending inputs, tasks, and focus.
- Memory must not override current nodes or resurrect replaced work.
- If memory tools are not visible, use current state and ask when stable preferences matter.
"""
