NAME = "skill_usage"
TRIGGER = "always"
TIER = "s"
ORDER = 21

PROMPT = """\
# Skills

- The catalog gives names, descriptions, and locators. Use every `$SkillName`, plain-text name, or clear description match; otherwise use the minimal set. Skills apply only this turn.
- `<skill>` blocks are loaded. For other orchestrator resources, use `skills.list` for exact handles and `skills.read` for every `SKILL.md` page before acting.
- Follow its routing: the main Agent reads required linked resources from the same authority/package and reuses package scripts/assets/templates; workers begin afterward.
- Announce the Skill order. If unavailable, say so and use the best fallback.
"""
