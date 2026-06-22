"""Task planner: converts parsed intent into a model-authored execution shape."""
from __future__ import annotations

import json
from typing import Any

from app.services.llm_service import llm_service


PLANNER_PROMPT = """You are the planner for a creative video Agent.
Return exactly one JSON object.

For a multi-stage request return:
{"mode":"plan","plan_doc":{"title":"...","summary":"...","phases":[
  {"phase":1,"title":"...","goal":"...","depends_on":[],"steps":[
    {"step":1,"tool":"node.create","title":"...","input":{"type":"text","fields":{"title":"...","content":"..."}}},
    {"step":2,"tool":"node.run","title":"...","input":{"node_id":"<由 step 1 产出>"}}
  ]}
]}}

Rules:
1. Plans may only use node.create and node.run.
2. Public creative node types are text, image, video, and audio.
3. Expand every phase into concrete steps. Pair each node.create with node.run when the node is ready to execute.
4. Use "<由 step N 产出>" as node_id when a node.run consumes a node created earlier in the same plan.
5. Use phase depends_on and node fields references / depends_on to express ordering.
6. Text nodes hold story, structure, captions, prompt drafts, analysis, or production notes.
7. Image nodes hold character references, scene references, storyboard boards, keyframes, style boards, or other visual references.
8. Video nodes hold final video generation requests and require fields.prompt at run time.
9. Audio nodes hold pure audio requests and require fields.prompt at run time.
10. When a video prompt depends on an image result, create and run the image node first, then create or update the video node with a prompt grounded in that image output.
11. Simple atomic requests can return {"mode":"execute","plan":[]} so the main Agent loop executes directly.
12. Visual-preproduction requests produce text and image nodes.
13. Leave fields.model empty unless the user names a provider.
"""

def _normalize_planner_output(raw: dict[str, Any]) -> dict[str, Any]:
    """Tolerate older planner outputs that only return `{plan: [...]}`."""
    if not isinstance(raw, dict):
        return {"mode": "execute", "plan": []}
    if "mode" in raw:
        if raw["mode"] == "plan":
            doc = raw.get("plan_doc") or {}
            if not isinstance(doc, dict):
                doc = {}
            doc.setdefault("title", "执行方案")
            doc.setdefault("summary", "")
            doc.setdefault("sections", [])
            return {"mode": "plan", "plan_doc": doc}
        return {"mode": "execute", "plan": raw.get("plan") or []}
    if isinstance(raw.get("plan"), list):
        return {"mode": "execute", "plan": raw["plan"]}
    return {"mode": "execute", "plan": []}


class Planner:
    async def plan(
        self,
        intent: dict[str, Any],
        project_state: dict[str, Any],
        recent_messages: list[dict] | None = None,
        memory_facts: list[dict] | None = None,
    ) -> dict[str, Any]:
        state = project_state or {}
        meta = state.get("metadata", {})
        ctx_parts = [
            f"Intent:\n{json.dumps(intent, ensure_ascii=False, indent=2)}",
            "",
            "Project state summary:",
            f"  title: {meta.get('title', 'N/A')}",
            f"  episode_count: {meta.get('episode_count', 0)}",
            f"  characters: {len(state.get('characters', []))}",
            f"  episodes generated: {len(state.get('episodes', {}))}",
        ]
        if memory_facts:
            ctx_parts.append("")
            ctx_parts.append("Long-term facts (project + user):")
            for f in memory_facts[:15]:
                ctx_parts.append(f"  - [{f.get('kind', '?')}] {f.get('content', '')}")
        if recent_messages:
            ctx_parts.append("")
            ctx_parts.append("Recent conversation (oldest → newest):")
            for m in recent_messages[-6:]:
                role = m.get("role", "?")
                content = (m.get("content") or "")[:400]
                ctx_parts.append(f"  [{role}] {content}")

        messages = [{"role": "user", "content": "\n".join(ctx_parts)}]

        try:
            result = await llm_service.generate(
                task_type="planning",
                messages=messages,
                system=PLANNER_PROMPT,
            )
            text = result.get("content", "").strip()
            if text.startswith("```"):
                lines = text.splitlines()
                if lines[-1].strip() == "```":
                    text = "\n".join(lines[1:-1])
                else:
                    text = "\n".join(lines[1:])
            parsed = json.loads(text)
            return _normalize_planner_output(parsed)
        except Exception as exc:
            return {
                "ok": False,
                "error": f"planner LLM failed: {exc}",
                "error_kind": "planner_llm_failed",
            }


async def plan_tasks(
    intent: dict[str, Any],
    project_state: dict[str, Any],
    recent_messages: list[dict] | None = None,
    memory_facts: list[dict] | None = None,
) -> dict[str, Any]:
    return await Planner().plan(intent, project_state, recent_messages, memory_facts)
