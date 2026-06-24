NAME = "flow_paths"
TRIGGER = "video"
ORDER = 90

PROMPT = """\
# Video Path

For video, read skills with `skill.search -> skill.get`; user/local results come first, default `video_production` is fallback.

- Work on `text` / `image` / `video` nodes; reuse matching nodes first.
- Finished clips normally build planning `text`, character/scene/storyboard `image`, then final `video`; 15s usually remains one segment.
- Tasks are progress records; production dependencies live in `fields.references`.
- Story-template/故事模板 loads `skill.story_template_method` by `tool.execute`.
- If a `video` needs future images, update it after images complete so prompt, duration, aspect, and refs are real.
"""
