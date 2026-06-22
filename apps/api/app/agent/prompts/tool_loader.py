NAME = "tool_loader"
TRIGGER = "always"
TIER = "s"
ORDER = 27  # 紧跟 working_loop(20) / task_loop(25)

PROMPT = """\
# Tool Use

Use the visible core tools directly.

- Skills: `skill.search -> skill.get`; local user skills return before default guides.
- Deferred methods: `tool.search -> tool.describe -> tool.execute`.
- Story-template/故事模板: execute `skill.story_template_method(detail='full')`.
- `agent.review`: read-only checkpoint.
- Prefer state, nodes, skills, and tool feedback; errors use `error_kind/hint/model_feedback`.
"""
