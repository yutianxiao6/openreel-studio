NAME = "tool_loader"
TRIGGER = "always"
TIER = "s"
ORDER = 27  # 紧跟 working_loop(20) / task_loop(25)

PROMPT = """\
# Tool Use

Use the visible core tools directly.

- `skill.video_production`: default image/video guide.
- Named skills/methods: `tool.search -> tool.describe -> tool.execute`.
- Story-template/故事模板: execute `skill.story_template_method` with `detail='full'`; `skill.video_production(request=...)` is not a router.
- `agent.review`: read-only checkpoint.
- Prefer state, nodes, skill guidance, and tool feedback over memory.
- On errors read `error_kind/hint/model_feedback`; fix args, ask, or stop.
"""
