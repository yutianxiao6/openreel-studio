NAME = "assets_rule"
TRIGGER = "assets"
ORDER = 160

PROMPT = """\
# Assets

Asset library work is opt-in and read-heavy.

- Query current asset files with deferred `assets.list_project` / `assets.list_shared`; read details with `assets.read_asset`.
- When the latest user asks to save generated media or scripts to the asset library, use deferred `assets.save_to_project` or `assets.save_to_shared`.
- Asset library paths, deletion, and bulk management belong to the front-end 资产面板 or REST API.
- When the user asks to use an image asset, put `asset:<id>` or the resolved asset path in `fields.references`; use `visual_reference` for generation and `source_image` for direct adoption.
- Generated images, videos, and scripts stay in node output and project storage by default.
- Save generated media to the project/shared asset library only when the latest user message asks to save or reuse it there.
"""
