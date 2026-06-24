NAME = "attachment_rule"
TRIGGER = "attachments"
ORDER = 170

PROMPT = """\
# Attachments

Treat attachments as current-turn evidence after checking runtime state.

- Use the attachment `reference` / `rel_path` value from runtime state.
- When a node needs an uploaded image, write it to `fields.references` as `{"ref":"upload:<rel_path>","role":"visual_reference"}`.
- Use `{ref, role}` when the purpose matters: `visual_reference` means generate with this image; `source_image` means an image node directly adopts this image as output.
- For uploaded text/document content, read the file with `file.read_text` or `file.extract_text_from_upload`; large files provide paged content with `next_offset`.
- If image understanding is unavailable, keep the reference link and say the image cannot be inspected.
- Save attachment analysis to long-term memory only when the user asks for a lasting preference.
"""
