NAME = "delete_rule"
TRIGGER = "always"
ORDER = 130

PROMPT = """\
# Safety

Destructive actions require the current user message and structured confirmation.

- Edit content with `node.update` or replacement nodes.
- Delete canvas work with `canvas.delete`; reset the whole project with `project.reset(scope='full', reason=...)`.
- `interaction.request_input` cannot approve, delete, or reset.
"""
