NAME = "assets_rule"
TRIGGER = "assets"
ORDER = 160

PROMPT = """\
# Assets

Asset library work is explicit.

- Read files with `assets.list_project` / `assets.list_shared`; inspect one file with `assets.read_asset`; list buckets with `assets.list_categories`.
- Save requested media or scripts with `assets.save_to_project` or `assets.save_to_shared`, choosing episode/kind or kind/category.
- When saving an image node, use its visible title as `name`; if generic, supply a concise asset name.
- Organize requested assets with `assets.create_category` and `assets.move_asset`; put requested files on canvas with `assets.add_to_canvas`.
- To use an image asset in generation, write `asset:<id>` or the resolved asset path into target node `fields.references` with role `visual_reference`; use `source_image` when adopting the image directly.
- Generated media and scripts stay in node output/project storage until the user asks to save or reuse them in the library.
"""
