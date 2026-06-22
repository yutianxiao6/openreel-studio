NAME = "attachment_rule"
TRIGGER = "attachments"
ORDER = 170

PROMPT = """\
# Attachments

Treat attachments as current-turn evidence after checking runtime state.

- Resolve uploaded files and @mentions from runtime state or the reference index.
- When the current node needs an attachment or image, write it to `fields.references`.
- Use `{ref, role}` when the purpose matters: `visual_reference` means generate with this image; `source_image` means an image node directly adopts this image as output.
- If image understanding is unavailable, keep the reference link and say the image cannot be inspected.
- Save attachment analysis to long-term memory only when the user asks for a lasting preference.
"""
