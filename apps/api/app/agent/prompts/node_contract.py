NAME = "node_contract"
TRIGGER = "create"
ORDER = 70

PROMPT = """\
# Node Contract

Shared nodes: `text`, `image`, `video`.

- Existing/empty/draft nodes are targets; update before duplicates.
- Active/user skill controls method; default media uses `skill.video_production`.
- Put content in title/prompt/fields; upstream inputs go in `fields.references`.
- Batch small framework or low-risk create/update sets; split many nodes, rich media prompts, or uncertain edits.
- `node.create` writes fields; `node.run` generates/adopts/saves. Text needs `fields.content`; media needs prompt.
- `parent_node_id` groups UI; `fields.references` drives edges. Visual image refs feed media.
- Text refs are context; `visual_reference` feeds generation; `source_image` adopts image.
- Resolve targets with `node.list`; fetch details with `node.get(node_ids=[...])`; use real `node_id`.
- For `dependency_missing`/missing prompt/refs, update original/upstream node, then rerun.
"""
