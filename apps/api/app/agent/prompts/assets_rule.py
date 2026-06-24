NAME = "assets_rule"
TRIGGER = "assets"
ORDER = 160

PROMPT = """\
# Assets

Asset library work is explicit.

- Read the single library with `assets.list_shared` / `assets.list_project`; inspect files with `assets.read_asset`; list folders with `assets.list_categories`.
- Save with `assets.save_to_shared`; infer kind as character, scene, or storyboard, then infer category from asset content and user words. Ask only when classification is not reasonably knowable.
- Use the image node title as `name`; if generic, supply a concise asset name.
- Organize with `assets.create_category` / `assets.move_asset`; put files on canvas with `assets.add_to_canvas`.
- Reuse images by writing `asset:<id>` or path into `fields.references` with role `visual_reference`; use `source_image` when adopting directly.
- Generated media stays outside the library until the user asks to save or reuse it.
"""
