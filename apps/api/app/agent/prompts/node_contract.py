NAME = "node_contract"
TRIGGER = "create"
ORDER = 70

PROMPT = """\
# Node Contract

Shared nodes: `text`, `image`, `video`, `audio`.

- Existing/draft nodes are targets; update before duplicates.
- Active/user skill controls method; image/video default uses `skill.search -> skill.get`.
- Put content in title/prompt/fields; upstream inputs in `fields.references`.
- Batch small/low-risk creates/updates; split many nodes, rich prompts, or uncertain edits.
- `node.create` writes fields; `node.run` saves/generates. Text needs `fields.content`; media need prompt.
- `parent_node_id` groups UI; `fields.references` drives edges. Visual refs feed media.
- Text refs are context; `visual_reference` feeds generation; `source_image` adopts image.
- Resolve with `node.list(query|regex)` before broad list; details with `node.get(node_ids=[...])`; use real `node_id`.
- For `dependency_missing`/missing prompt/refs, update original/upstream node, then rerun.
"""
