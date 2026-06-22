NAME = "clarify"
TRIGGER = "first_contact"  # 项目空 + 用户消息含创作意图
ORDER = 40

PROMPT = """\
# Clarification

Ask only for facts that block the active skill/process.

- Use `interaction.request_input(questions=[...])` for up to 6 concise questions; omit options for open text.
- Before asking, compare skill requirements with known facts: known, unknown, and questions needed.
- If the user gives custom edits, treat them as constraints, output the revised proposal/questions, and confirm again until they approve or stop customizing.
- Preserve facts the user already gave. If the user says “你决定/全权发挥”, choose concrete assumptions and write them into a planning `text` node.
- With enough context, fill matching empty/draft nodes or create/update nodes according to the active skill.
"""
