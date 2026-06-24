NAME = "assets_rule"
TRIGGER = "assets"
ORDER = 160

PROMPT = """\
# Assets

Asset library work is opt-in and read-heavy.

- Query current asset files with deferred `assets.list_project` / `assets.list_shared`; read details with `assets.read_asset`.
- Asset library paths, deletion, and bulk management belong to the front-end 资产面板 or REST API.
- When the user asks to use an asset, put the resolved id/path in `fields.references`; use `visual_reference` for generation and `source_image` for direct adoption.
- Generated images, videos, and scripts stay in node output and project storage by default.
- 不要自动保存 generated media to the project/shared asset library unless the latest user message asks to save or reuse it there.
"""
