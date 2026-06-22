NAME = "assets_rule"
TRIGGER = "assets"
ORDER = 160

PROMPT = """\
# Assets

Asset library work is opt-in and read-heavy.

- Current asset files are queried with deferred `assets.list_project` / `assets.list_shared`; read file details with `assets.read_asset` when needed.
- Asset library paths, deletion, and bulk management belong to the front-end 资产面板 or REST API.
- When the user asks to use an asset, put the resolved id/path in `fields.references`; use role `visual_reference` for generation reference and `source_image` when an image node directly adopts it as output.
- Generated images, videos, and scripts stay in node output and project storage by default.
- 不要自动保存 generated media to the project/shared asset library unless the latest user message asks to save or reuse it there.
"""
