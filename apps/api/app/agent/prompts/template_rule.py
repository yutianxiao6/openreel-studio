NAME = "template_rule"
TRIGGER = "template"
ORDER = 140

PROMPT = """\
# Skill Prompt Sources

User-facing prompt methods live in skills, not in a separate template library.

- For production, read the relevant skill and write the final node prompt yourself.
- If the user wants a reusable prompt method, tell them to add it to a skill.
- Write one-off style choices directly into the current node prompt/fields.
"""
