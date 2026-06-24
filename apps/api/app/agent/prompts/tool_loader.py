NAME = "tool_loader"
TRIGGER = "always"
TIER = "s"
ORDER = 27  # 紧跟 working_loop(20) / task_loop(25)

PROMPT = """\
# Tool Use

Use the visible core tools directly.

- Skills: `skill.search -> skill.get`; local before default.
- Deferred: `tool.search -> tool.describe -> tool.execute`.
- Story-template/故事模板: `skill.story_template_method(detail='full')`.
- `agent.review` is read-only.
- Prefer state, nodes, skills, tool feedback; errors include `error_kind/hint/model_feedback`.
"""
