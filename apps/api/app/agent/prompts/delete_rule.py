NAME = "delete_rule"
TRIGGER = "always"
ORDER = 130

PROMPT = """\
# Safety

Destructive actions require the current user request plus structured confirmation.

- Edit node text, fields, and prompts with `node.update` or replacement nodes.
- `interaction.request_input` cannot approve, delete, or reset.
"""
