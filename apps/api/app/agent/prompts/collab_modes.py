NAME = "collab_modes"
TRIGGER = "complex"  # 复杂任务:含 整部/全剧/多集/完整/从头到尾 或 视频+创作
ORDER = 80

PROMPT = """\
# Collaboration

Default to serial work by node dependencies.

- Search the `collab` category only for independent read-only analysis or review.
- A subagent is 只读: it cannot create, update, run, delete, approve, or reset project state.
- Use `agent.review` for a second view on complex work. Pass 审查目标, 用户需求, 工作摘要, guide topics, evidence, and focus.
- `agent.review` is 通用只读审查; 检查结果只返回给你. You decide whether to 继续修改, continue execution, or report a blocker.
- Custom standards and 自定义检查项 live in `data/review_skills/<key>.md`; inject them with `review_skill_key` or `custom_checklist`.
"""
