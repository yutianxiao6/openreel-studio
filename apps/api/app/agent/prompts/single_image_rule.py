NAME = "single_image_rule"
TRIGGER = "create"
ORDER = 45

PROMPT = """\
# Single Image

Keep one-off image requests atomic; 不要进入蓝图或计划流程。

- One requested image usually means one `image` node.
- Numbered or named existing targets follow the shared node lookup rule; reusable empty/draft image nodes are updated first.
- For a new image, use active/user skill prompt rules; otherwise read default skill with `skill.search -> skill.get`, then `node.create -> node.run`.
- 图生图 reuses completed visual nodes/assets through `fields.references`; `visual_reference` guides generation, `source_image` adopts an existing image.
- If content is too vague, ask only for active-skill blocking facts with `interaction.request_input`.
"""
