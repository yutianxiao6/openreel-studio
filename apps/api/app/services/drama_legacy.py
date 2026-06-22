"""Internal legacy drama segment services.

These functions keep no-blueprint fallback behavior available to node.run, but
they are not intended to be registered as agent-facing tools.
"""
from __future__ import annotations

import json

from app.mcp_tools import drama_tools


async def plan_episode_segments(
    project_id: str,
    episode_number: int,
    target_duration_seconds: int = 15,
    episode_duration_seconds: int | None = None,
    node_id: str | None = None,
) -> dict:
    """Slice an episode script into segment records and persist state.segments."""
    async with drama_tools.session_scope() as session:
        project = await session.get(drama_tools.Project, project_id)
        if not project:
            return {"error": "Project not found"}

        state = json.loads(project.state_json or "{}")
        workflow_mode = drama_tools._default_segment_workflow_mode(state)
        script = state.get("episodes", {}).get(str(episode_number))
        if not script:
            return {"error": f"Episode {episode_number} has no script yet"}

        characters = state.get("characters", [])
        user_prompt = (
            f"剧本：\n{json.dumps(script, ensure_ascii=False)[:8000]}\n\n"
            f"已有人物：\n{json.dumps([c.get('name') for c in characters if isinstance(c, dict)], ensure_ascii=False)}\n\n"
            f"目标段落时长：约 {target_duration_seconds} 秒/段\n\n"
            "请把剧本切成若干段落,每段输出 JSON,字段:"
            "index(序号,从 1 开始) / duration_seconds / plot(剧情概要) / "
            "characters(出场人物名字数组) / scene_refs(场景描述数组,允许多场景) / "
            "segment_arc(开局/冲突/反转/钩子等)。输出 JSON 数组,不要其他文字。"
        )

        svc = drama_tools.LLMService(session)
        result = await svc.generate(
            task_type="storyboard_generation",
            messages=[{"role": "user", "content": user_prompt}],
            system=await drama_tools.resolve_prompt(
                "drama.plan_episode_segments",
                project_id,
                node_id,
                ctx=drama_tools.WorkerContext(
                    project_id=project_id,
                    node_id=node_id,
                    episode_number=episode_number,
                    duration_seconds=target_duration_seconds,
                ),
            ),
            project_id=project_id,
        )

        segments = drama_tools._extract_json(result["content"], default=[])
        if not isinstance(segments, list):
            segments = []
        resolved_episode_duration = (
            episode_duration_seconds
            or drama_tools._project_episode_duration_seconds(project, state)
        )
        segments = drama_tools._normalize_episode_segments(
            segments,
            episode_number=episode_number,
            target_duration_seconds=target_duration_seconds,
            workflow_mode=workflow_mode,
            episode_duration_seconds=resolved_episode_duration,
        )

        eps_segments = state.setdefault("segments", {})
        eps_segments[str(episode_number)] = segments
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()

        return {
            "episode_number": episode_number,
            "segment_count": len(segments),
            "segments": segments,
            "target_duration_seconds": target_duration_seconds,
            "episode_duration_seconds": resolved_episode_duration,
        }


async def update_segment(
    project_id: str,
    episode_number: int,
    segment_index: int,
    plot: str | None = None,
    characters: list[str] | None = None,
    scene_refs: list[str] | None = None,
    duration_seconds: int | None = None,
    segment_arc: str | None = None,
) -> dict:
    """Edit one segment in-place inside state.segments[episode]."""
    async with drama_tools.session_scope() as session:
        project = await session.get(drama_tools.Project, project_id)
        if not project:
            return {"error": "Project not found"}
        state = json.loads(project.state_json or "{}")
        segs = state.get("segments", {}).get(str(episode_number), [])
        target = next(
            (s for s in segs if isinstance(s, dict) and s.get("index") == segment_index),
            None,
        )
        if not target:
            return {"error": f"Segment {segment_index} of episode {episode_number} not found"}

        if plot is not None:
            target["plot"] = plot
        if characters is not None:
            target["characters"] = characters
        if scene_refs is not None:
            target["scene_refs"] = scene_refs
        if duration_seconds is not None:
            target["duration_seconds"] = duration_seconds
        if segment_arc is not None:
            target["segment_arc"] = segment_arc

        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()
        return {"ok": True, "segment": target}


async def set_segment_workflow_mode(
    project_id: str,
    episode_number: int,
    segment_index: int,
    mode: str,
) -> dict:
    """Switch a segment's visual workflow path."""
    if mode not in drama_tools._VALID_WORKFLOW_MODES:
        return {"error": f"mode 必须是 {drama_tools._VALID_WORKFLOW_MODES} 之一,收到 '{mode}'"}
    async with drama_tools.session_scope() as session:
        project = await session.get(drama_tools.Project, project_id)
        if not project:
            return {"error": "Project not found"}
        state = json.loads(project.state_json or "{}")
        segs = state.get("segments", {}).get(str(episode_number), [])
        target = next(
            (s for s in segs if isinstance(s, dict) and s.get("index") == segment_index),
            None,
        )
        if not target:
            return {"error": f"Segment {segment_index} of episode {episode_number} not found"}
        old_mode = target.get("workflow_mode", "grid")
        target["workflow_mode"] = mode
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()
        return {
            "ok": True,
            "episode_number": episode_number,
            "segment_index": segment_index,
            "old_mode": old_mode,
            "new_mode": mode,
        }


async def assign_segment_scene(
    project_id: str,
    episode_number: int,
    segment_index: int,
    scene_id: str,
) -> dict:
    """Attach an existing scene id to a segment."""
    async with drama_tools.session_scope() as session:
        project = await session.get(drama_tools.Project, project_id)
        if not project:
            return {"error": "Project not found"}
        state = json.loads(project.state_json or "{}")
        segs = state.get("segments", {}).get(str(episode_number), [])
        target = next(
            (s for s in segs if isinstance(s, dict) and s.get("index") == segment_index),
            None,
        )
        if not target:
            return {"error": f"Segment {segment_index} not found"}
        refs = target.get("scene_ids") or []
        if scene_id not in refs:
            refs.append(scene_id)
        target["scene_ids"] = refs
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()
        return {"ok": True, "segment": target}


async def plan_episode_cast_scene(
    project_id: str,
    episode_number: int,
    node_id: str | None = None,
) -> dict:
    """Plan per-episode cast, scenes, and segment assignments."""
    async with drama_tools.session_scope() as session:
        project = await session.get(drama_tools.Project, project_id)
        if not project:
            return {"error": "Project not found"}
        state = json.loads(project.state_json or "{}")
        script = state.get("episodes", {}).get(str(episode_number))
        if not script:
            return {"error": f"第 {episode_number} 集还没有剧本"}
        segments = state.get("segments", {}).get(str(episode_number), [])
        characters = state.get("characters", [])

        user_prompt = (
            f"剧本：\n{json.dumps(script, ensure_ascii=False)[:6000]}\n\n"
            f"切段方案：\n{json.dumps(segments, ensure_ascii=False)[:3000]}\n\n"
            f"已有人物：{json.dumps([c.get('name') for c in characters if isinstance(c, dict)], ensure_ascii=False)}\n\n"
            "请规划：(1) cast 本集出场人物（数组,字段 name + 出场段落 + 戏份占比 0-1）;"
            "(2) scenes 本集场景（数组,字段 name + description + 出场段落）;"
            "(3) segment_assignments 每段对应的人物子集和场景子集，**必须是数组**(不要用 object key),"
            "每项字段：segment_index(整数)、characters(数组)、scene(字符串场景名)。"
            "输出 JSON,不要解释。"
        )
        svc = drama_tools.LLMService(session)
        result = await svc.generate(
            task_type="storyboard_generation",
            messages=[{"role": "user", "content": user_prompt}],
            system=await drama_tools.resolve_prompt(
                "drama.plan_episode_cast_scene",
                project_id,
                node_id,
                ctx=drama_tools.WorkerContext(
                    project_id=project_id,
                    node_id=node_id,
                    episode_number=episode_number,
                ),
            ),
            project_id=project_id,
        )
        plan = drama_tools._extract_json(result["content"], default={})
        if not isinstance(plan, dict):
            plan = {}
        plan.setdefault("cast", [])
        plan.setdefault("scenes", [])
        plan.setdefault("segment_assignments", [])

        if isinstance(plan.get("segment_assignments"), dict):
            sa_list = []
            for k, v in plan["segment_assignments"].items():
                if not isinstance(v, dict):
                    continue
                row = {**v}
                if "segment_index" not in row:
                    try:
                        row["segment_index"] = int(k)
                    except (TypeError, ValueError):
                        row["segment_index"] = k
                sa_list.append(row)
            plan["segment_assignments"] = sa_list

        eps = state.setdefault("episodes_meta", {})
        ep_meta = eps.setdefault(str(episode_number), {})
        ep_meta["cast_scene_plan"] = plan
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()

        return {
            "episode_number": episode_number,
            "cast_count": len(plan["cast"]) if isinstance(plan.get("cast"), list) else 0,
            "scene_count": len(plan["scenes"]) if isinstance(plan.get("scenes"), list) else 0,
            "cast": plan.get("cast", []),
            "scenes": plan.get("scenes", []),
            "segment_assignments": plan.get("segment_assignments", []),
            "plan": plan,
        }
