NAME = "flow_paths"
TRIGGER = "video"
ORDER = 90

PROMPT = """\
# Video Path

For video with no user skill, read `skill.video_production`; story-template/故事模板 loads `skill.story_template_method` by `tool.execute`. Work on `text` / `image` / `video` nodes.

- Reuse matching user nodes first; normal output is script/planning `text`, character/scene/storyboard `image`, then final `video`.
- A 15s request is usually one segment, but still prepares script, character, scene, storyboard, and video when making a finished clip.
- Create at least one non-empty planning/script `text` node before downstream media; tasks are progress records, not creative content.
- Use `parent_node_id` for grouping and `fields.references` for production dependencies; use role `visual_reference` for images that media generation should see.
- If a `video` needs future images, create/update it after those image nodes complete so prompt, duration, aspect, and references are real.
"""
