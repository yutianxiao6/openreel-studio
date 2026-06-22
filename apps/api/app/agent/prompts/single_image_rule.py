NAME = "single_image_rule"
TRIGGER = "create"
ORDER = 45

PROMPT = """\
# Single Image

Keep one-off image requests atomic; 不要进入蓝图或计划流程。

- One requested image usually means one `image` node.
- Use `node.list` to find reusable or empty image nodes; if the user names a node/title or says “这张图”, read it with `node.get`.
- For a new image, use the active/user skill for prompt rules; if none is supplied, read `skill.video_production`, then `node.create -> node.run`.
- 图生图 requests reuse completed visual nodes or assets; put them in `fields.references` with role `visual_reference`. If the node should directly use an existing image without generation, use role `source_image`.
- If content is too vague, ask with `interaction.request_input` for the missing facts required by the active skill.
"""
