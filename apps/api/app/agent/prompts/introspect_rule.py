NAME = "introspect_rule"
TRIGGER = "introspect"
ORDER = 180

PROMPT = """\
# System Introspection

Answer model, tool, MCP, feature flag, and provider questions from tools.

- Query deferred `system` tools when current state is insufficient.
- If you know the tool name, call `tool.describe` before `tool.execute`.
- Settings writes, provider switching, MCP management, and prompt overrides belong to settings or admin APIs.
- Base the answer on returned data; mark unavailable values as unknown.
"""
