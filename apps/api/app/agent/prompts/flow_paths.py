NAME = "flow_paths"
TRIGGER = "video"
ORDER = 90

PROMPT = """\
# Video Path

For video, read skills with `skill.search -> skill.get`; user/local results come first, default `video_production` is fallback. Story-template/故事模板 loads `skill.story_template_method` by `tool.execute`. Work on `text` / `image` / `video` nodes.

- Reuse matching nodes first; normal output is planning `text`, character/scene/storyboard `image`, then final `video`.
- A 15s request is usually one segment, but still prepares script, character, scene, storyboard, and video when making a finished clip.
- Create non-empty planning/script `text` before downstream media; tasks are progress records.
- Use `parent_node_id` for grouping and `fields.references` for production dependencies; use role `visual_reference` for images that media generation should see.
- If a `video` needs future images, create/update it after images complete so prompt, duration, aspect, and refs are real.
"""
