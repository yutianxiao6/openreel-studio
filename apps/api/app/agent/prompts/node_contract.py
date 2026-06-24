NAME = "node_contract"
TRIGGER = "create"
ORDER = 70

PROMPT = """\
# Node Contract

Shared nodes: `text`/`image`/`video`/`audio`; methods from `skill.search -> skill.get`.

- Existing/draft targets update before duplicates.
- Content in title/prompt/fields; upstream in `fields.references`.
- Batch small/low-risk edits; split many nodes.
- `node.create` writes fields; `node.run` saves/generates. Text needs `fields.content`; media need prompt.
- `parent_node_id` groups UI; `fields.references` drives edges with roles `context`, `visual_reference`, `source_image`.
- Numbered targets use `node.get`; named/unclear use `node.list(query|regex)`; details use `node.get(node_ids)`.
- `dependency_missing`/missing prompt/refs update original/upstream, then rerun.
"""
