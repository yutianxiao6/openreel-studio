"""Drama generation MCP-style tools."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from sqlmodel import select

from app.agent.project_blueprint import (
    UNTITLED_PROJECT_TITLE,
    clear_blueprint_state,
    delete_blueprint_files_report,
)
from app.config import settings
from app.db.models import Character, Episode, Message, Project
from app.db.session import session_scope
from app.prompts import resolve_prompt
from app.prompts._section import WorkerContext
from app.services.llm_service import LLMService


_VALID_WORKFLOW_MODES = ("grid", "frames", "story_template")

_FULL_RESET_CONTEXT_KEYS = (
    "characters",
    "outline",
    "episodes",
    "segments",
    "scenes",
    "episodes_meta",
    "relationships",
    "shots",
    "assets",
    "asset_library",
    "session",
    "memory",
    "prompt_overrides",
    "_canvas_summary",
    "guide_loaded",
    "_mentor_guides_loaded",
    "_skills_loaded",
    "_last_template_lookup",
    "_template_lookups_by_category",
    "_last_agent_review",
    "project_mode",
    "project_sub_mode",
    "selected_video_mode",
    "pending_video_mode_choice",
    "pending_video_brief",
    "pending_video_blueprint_request",
    "active_plan_checklist",
    "active_plan_id",
    "pending_plan",
    "pending_plan_preview_checklist",
    "plan_history",
    "last_finished_plan_id",
    "last_finished_plan_status",
    "last_finished_plan_at",
    "panel_layout",
    "_pending_reset_confirm",
    "_pending_tool_confirm",
    "agent_token_usage",
    "pending_blueprint_intake",
    "pending_blueprint_review",
    "pending_blueprint_draft",
    "pending_blueprint_revision",
    "pending_blueprint_section_review",
    "pending_blueprint_confirmation",
    "project_blueprint",
    "semantic_blueprint",
    "blueprint_partial_plan_doc",
    "reference_assets",
    "blueprint_progress",
    "blueprint_generation_progress",
    "blueprint_section_results",
    "blueprint_window_progress",
    "blueprint_stale_nodes",
    "blueprint_history",
    "creative_blueprint_history",
)


def _extract_json(text: str, default: object) -> object:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    pattern = r"\[.*\]" if isinstance(default, list) else r"\{.*\}"
    match = re.search(pattern, text, re.DOTALL)
    try:
        return json.loads(match.group()) if match else default
    except Exception:
        return default


def _normalize_requirements(requirements: list[str] | str | None) -> list[str]:
    if isinstance(requirements, str):
        requirements = [requirements]
    return [
        item.strip()
        for item in (requirements or [])
        if isinstance(item, str) and item.strip()
    ]


def _legacy_prompt_hint(category: str, query: str = "") -> str:
    return ""


def _default_segment_workflow_mode(state: dict) -> str:
    mode = (
        state.get("selected_video_mode")
        or state.get("project_sub_mode")
        or "grid"
    )
    return mode if mode in _VALID_WORKFLOW_MODES else "grid"


async def _archive_project_chat_messages(session, project_id: str) -> int:
    """Hide old project chat from future prompt assembly after full reset."""
    result = await session.exec(
        select(Message).where(
            Message.project_id == project_id,
            Message.archived == False,  # noqa: E712
        )
    )
    messages = list(result.all())
    for message in messages:
        message.archived = True
        session.add(message)
    return len(messages)


def _project_episode_duration_seconds(project: Project, state: dict) -> int | None:
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    raw = metadata.get("duration_per_episode") or getattr(project, "duration_per_episode", None)
    try:
        duration = int(raw)
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def _normalize_episode_segments(
    segments: list,
    *,
    episode_number: int,
    target_duration_seconds: int,
    workflow_mode: str,
    episode_duration_seconds: int | None,
) -> list[dict]:
    normalized = [dict(seg) for seg in segments if isinstance(seg, dict)]
    target = max(1, int(target_duration_seconds or 15))
    max_segments = None
    if episode_duration_seconds:
        max_segments = max(1, math.ceil(episode_duration_seconds / target))
        normalized = normalized[:max_segments]

    for i, seg in enumerate(normalized):
        seg["index"] = i + 1
        seg["episode_number"] = episode_number
        seg["id"] = f"seg-{episode_number}-{i + 1}"
        seg["workflow_mode"] = workflow_mode
        if episode_duration_seconds:
            remaining = max(1, episode_duration_seconds - (target * i))
            seg["duration_seconds"] = min(target, remaining)
        else:
            seg.setdefault("duration_seconds", target)
    return normalized


def _extract_single_character(data: object, requirements: list[str] | str | None = None) -> dict:
    if isinstance(data, list):
        data = data[0] if data else {}
    if isinstance(data, dict) and isinstance(data.get("characters"), list):
        data = data["characters"][0] if data["characters"] else {}
    if not isinstance(data, dict):
        data = {}

    result = dict(data)
    fallback = "，".join(_normalize_requirements(requirements))
    if fallback:
        if not result.get("appearance"):
            result["appearance"] = fallback
        if not result.get("visual_prompt"):
            result["visual_prompt"] = fallback
    return result


def _is_actionable_image_prompt(prompt: object) -> bool:
    if not isinstance(prompt, str) or len(prompt.strip()) < 12:
        return False
    blocked = ("请输入", "请提供", "请补充", "需要补充", "以便我生成", "无法生成")
    return not any(token in prompt for token in blocked)


async def generate_characters(
    project_id: str,
    requirements: list[str] | None = None,
    tier: str = "main",
    episode_number: int | None = None,
    segment_id: str | None = None,
    node_id: str | None = None,
) -> dict:
    """Generate multiple characters in one call.

    tier ∈ main / recurring / guest. Guest characters typically attach to a
    specific episode (and optionally a segment); main and recurring are global.
    """
    if tier not in {"main", "recurring", "guest"}:
        tier = "main"

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}

        state = json.loads(project.state_json or "{}")
        metadata = state.get("metadata", {})

        tier_hint = {
            "main": "主要人物（贯穿全剧）",
            "recurring": "常驻配角（多次出现）",
            "guest": "客串人物（本集/本段偶尔出场）",
        }[tier]
        scope_line = f"归属：{tier_hint}"
        if episode_number:
            scope_line += f"，第 {episode_number} 集"
        if segment_id:
            scope_line += f"，段落 {segment_id}"

        user_prompt_lines: list[str] = []
        req_lines = _normalize_requirements(requirements)
        if req_lines:
            user_prompt_lines.append(
                "用户当前要求(以此为准,优先级最高)：\n" + "\n".join(req_lines)
            )
        else:
            user_prompt_lines.append(
                f"项目信息(参考世界观)：\n{json.dumps(metadata, ensure_ascii=False, indent=2)}"
            )
        user_prompt_lines.append(scope_line)
        user_prompt_lines.append("请生成人物设定，输出 JSON 数组。")
        user_prompt = "\n\n".join(user_prompt_lines)

        svc = LLMService(session)
        result = await svc.generate(
            task_type="character_generation",
            messages=[{"role": "user", "content": user_prompt}],
            system=await resolve_prompt(
                "drama.generate_characters", project_id, node_id,
                ctx=WorkerContext(
                    project_id=project_id, node_id=node_id,
                    episode_number=episode_number,
                    extras={"tier": tier, "segment_id": segment_id},
                ),
            ),
            project_id=project_id,
        )

        characters_data = _extract_json(result["content"], default=[])
        if not isinstance(characters_data, list):
            characters_data = []

        for char_data in characters_data:
            if isinstance(char_data, dict):
                char_data["tier"] = tier
                if episode_number:
                    char_data["episode_number"] = episode_number
                if segment_id:
                    char_data["segment_id"] = segment_id
            char = Character(
                project_id=project_id,
                name=char_data.get("name", "") if isinstance(char_data, dict) else "",
                role_type=char_data.get("role_type", "support") if isinstance(char_data, dict) else "support",
                age=char_data.get("age") if isinstance(char_data, dict) else None,
                identity=char_data.get("identity", "") if isinstance(char_data, dict) else "",
                personality=char_data.get("personality", "") if isinstance(char_data, dict) else "",
                appearance=char_data.get("appearance", "") if isinstance(char_data, dict) else "",
                motivation=char_data.get("motivation", "") if isinstance(char_data, dict) else "",
                relationship_json=json.dumps(
                    char_data.get("relationships", {}) if isinstance(char_data, dict) else {},
                    ensure_ascii=False,
                ),
                visual_prompt=char_data.get("visual_prompt", "") if isinstance(char_data, dict) else "",
            )
            session.add(char)

        existing = state.get("characters", []) or []
        if tier == "main":
            state["characters"] = characters_data
        else:
            state["characters"] = existing + [c for c in characters_data if isinstance(c, dict)]

        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()

        return {
            "characters": characters_data,
            "count": len(characters_data),
            "tier": tier,
            "episode_number": episode_number,
            "segment_id": segment_id,
        }


async def generate_outline(
    project_id: str,
    requirements: list[str] | None = None,
    node_id: str | None = None,
    story_template_key: str | None = None,
) -> dict:
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}

        state = json.loads(project.state_json or "{}")
        metadata = state.get("metadata", {})
        characters = state.get("characters", [])

        user_prompt = (
            f"项目信息：\n{json.dumps(metadata, ensure_ascii=False, indent=2)}\n\n"
            f"人物设定：\n{json.dumps(characters, ensure_ascii=False, indent=2)}\n\n"
            f"额外要求：{chr(10).join(_normalize_requirements(requirements))}\n\n"
            "请生成分集大纲，输出 JSON。"
        )

        story_template = None

        outline_system = await resolve_prompt(
            "drama.generate_outline", project_id, node_id,
            ctx=WorkerContext(project_id=project_id, node_id=node_id),
        )
        # Combine explicit story template guidance with the outline output format.
        if story_template:
            system_prompt = f"{story_template}\n\n---\n\n{outline_system}"
        else:
            system_prompt = outline_system

        svc = LLMService(session)
        result = await svc.generate(
            task_type="outline_generation",
            messages=[{"role": "user", "content": user_prompt}],
            system=system_prompt,
            project_id=project_id,
        )

        outline_data = _extract_json(result["content"], default={})
        if not isinstance(outline_data, dict):
            outline_data = {}

        state["outline"] = outline_data
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()

        return {"outline": outline_data}


async def generate_episode_script(
    project_id: str,
    episode_number: int,
    requirements: list[str] | None = None,
    node_id: str | None = None,
) -> dict:
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}

        state = json.loads(project.state_json or "{}")
        metadata = state.get("metadata", {})
        characters = state.get("characters", [])
        outline = state.get("outline", {})

        episodes_outline = outline.get("episodes", [])
        ep_outline = next(
            (e for e in episodes_outline if e.get("episode_number") == episode_number),
            {},
        )

        user_prompt = (
            f"项目信息：\n{json.dumps(metadata, ensure_ascii=False, indent=2)}\n\n"
            f"人物设定：\n{json.dumps(characters, ensure_ascii=False, indent=2)}\n\n"
            f"第{episode_number}集大纲：\n{json.dumps(ep_outline, ensure_ascii=False, indent=2)}\n\n"
            f"额外要求：{chr(10).join(_normalize_requirements(requirements))}\n\n"
            f"请生成第{episode_number}集完整剧本，输出 JSON。"
        )

        svc = LLMService(session)
        result = await svc.generate(
            task_type="script_generation",
            messages=[{"role": "user", "content": user_prompt}],
            system=await resolve_prompt(
                "drama.generate_episode_script", project_id, node_id,
                ctx=WorkerContext(
                    project_id=project_id, node_id=node_id,
                    episode_number=episode_number,
                ),
            ),
            project_id=project_id,
        )

        script_data = _extract_json(
            result["content"], default={"script": result["content"]}
        )
        if not isinstance(script_data, dict):
            script_data = {"script": str(script_data)}

        stmt = select(Episode).where(
            Episode.project_id == project_id,
            Episode.episode_number == episode_number,
        )
        existing = (await session.exec(stmt)).first()

        if existing:
            existing.title = script_data.get("title", f"第{episode_number}集")
            existing.hook = ""
            existing.summary = script_data.get("summary", "")
            existing.script = script_data.get("script", "")
            existing.cliffhanger = script_data.get("cliffhanger", "")
            existing.status = "done"
            session.add(existing)
            episode_id = existing.id
        else:
            ep = Episode(
                project_id=project_id,
                episode_number=episode_number,
                title=script_data.get("title", f"第{episode_number}集"),
                hook="",
                summary=script_data.get("summary", ""),
                script=script_data.get("script", ""),
                cliffhanger=script_data.get("cliffhanger", ""),
                status="done",
            )
            session.add(ep)
            await session.flush()
            episode_id = ep.id

        state.setdefault("episodes", {})[str(episode_number)] = script_data
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()

        return {
            "episode_number": episode_number,
            "episode_id": episode_id,
            "script": script_data,
        }


async def rewrite_episode(
    project_id: str,
    episode_number: int,
    rewrite_scope: str,
    requirements: list[str] | None = None,
    node_id: str | None = None,
) -> dict:
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}

        state = json.loads(project.state_json or "{}")
        current_script = state.get("episodes", {}).get(str(episode_number), {})

        user_prompt = (
            f"当前第{episode_number}集剧本：\n"
            f"{json.dumps(current_script, ensure_ascii=False, indent=2)}\n\n"
            f"修改范围：{rewrite_scope}\n"
            f"修改要求：{chr(10).join(_normalize_requirements(requirements))}\n\n"
            "请按要求修改剧本，保留未修改部分，输出完整 JSON。"
        )

        svc = LLMService(session)
        result = await svc.generate(
            task_type="script_generation",
            messages=[{"role": "user", "content": user_prompt}],
            system=await resolve_prompt(
                "drama.rewrite_episode", project_id, node_id,
                ctx=WorkerContext(
                    project_id=project_id, node_id=node_id,
                    episode_number=episode_number,
                ),
            ),
            project_id=project_id,
        )

        new_script = _extract_json(
            result["content"], default={"script": result["content"]}
        )
        if not isinstance(new_script, dict):
            new_script = {"script": str(new_script)}

        state.setdefault("episodes", {})[str(episode_number)] = new_script
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)

        stmt = select(Episode).where(
            Episode.project_id == project_id,
            Episode.episode_number == episode_number,
        )
        ep = (await session.exec(stmt)).first()
        if ep:
            ep.script = new_script.get("script", ep.script or "")
            ep.cliffhanger = new_script.get("cliffhanger", ep.cliffhanger)
            session.add(ep)

        await session.commit()

        return {
            "episode_number": episode_number,
            "script": new_script,
            "rewrite_scope": rewrite_scope,
        }


async def review_script(project_id: str, episode_number: int, node_id: str | None = None) -> dict:
    """Score a single episode and surface issues."""
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}

        state = json.loads(project.state_json or "{}")
        characters = state.get("characters", [])
        script = state.get("episodes", {}).get(str(episode_number))
        if not script:
            return {"error": f"Episode {episode_number} has no script yet"}

        user_prompt = (
            f"人物设定：\n{json.dumps(characters, ensure_ascii=False, indent=2)}\n\n"
            f"第 {episode_number} 集剧本：\n"
            f"{json.dumps(script, ensure_ascii=False, indent=2)}\n\n"
            "请按要求审稿。"
        )

        svc = LLMService(session)
        result = await svc.generate(
            task_type="script_review",
            messages=[{"role": "user", "content": user_prompt}],
            system=await resolve_prompt(
                "drama.review_script", project_id, node_id,
                ctx=WorkerContext(
                    project_id=project_id, node_id=node_id,
                    episode_number=episode_number,
                ),
            ),
            project_id=project_id,
        )

        review = _extract_json(result["content"], default={})
        if not isinstance(review, dict):
            review = {"summary": str(review)}

        from app.db.models import Episode  # local import to avoid cycle warnings

        stmt = select(Episode).where(
            Episode.project_id == project_id,
            Episode.episode_number == episode_number,
        )
        ep = (await session.exec(stmt)).first()
        if ep:
            ep.score_json = json.dumps(review, ensure_ascii=False)
            session.add(ep)

        state.setdefault("reviews", {})[str(episode_number)] = review
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()

        return {"episode_number": episode_number, "review": review}


async def generate_storyboard(
    project_id: str,
    episode_number: int,
    requirements: list[str] | None = None,
    node_id: str | None = None,
) -> dict:
    """Generate a per-shot storyboard for one episode and persist to shots table."""
    from app.db.models import Shot

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}

        state = json.loads(project.state_json or "{}")
        script = state.get("episodes", {}).get(str(episode_number))
        if not script:
            return {"error": f"Episode {episode_number} has no script yet"}
        characters = state.get("characters", [])

        user_prompt = (
            f"项目信息：\n{json.dumps(state.get('metadata', {}), ensure_ascii=False)}\n\n"
            f"人物：\n{json.dumps(characters, ensure_ascii=False)}\n\n"
            f"剧本：\n{json.dumps(script, ensure_ascii=False)}\n\n"
            f"额外要求：{chr(10).join(_normalize_requirements(requirements))}\n\n"
            "输出 JSON 分镜表。"
        )

        svc = LLMService(session)
        result = await svc.generate(
            task_type="storyboard_generation",
            messages=[{"role": "user", "content": user_prompt}],
            system=await resolve_prompt(
                "drama.generate_storyboard", project_id, node_id,
                ctx=WorkerContext(
                    project_id=project_id, node_id=node_id,
                    episode_number=episode_number,
                ),
            ),
            project_id=project_id,
        )

        sb = _extract_json(result["content"], default={})
        if not isinstance(sb, dict):
            sb = {}
        shots = sb.get("shots", []) if isinstance(sb.get("shots"), list) else []

        ep_stmt = select(Episode).where(
            Episode.project_id == project_id,
            Episode.episode_number == episode_number,
        )
        episode = (await session.exec(ep_stmt)).first()
        episode_id = episode.id if episode else None

        for shot_data in shots:
            if not isinstance(shot_data, dict):
                continue
            shot = Shot(
                project_id=project_id,
                episode_id=episode_id,
                shot_number=int(shot_data.get("shot_number", 0) or 0),
                shot_type=shot_data.get("shot_type"),
                camera=shot_data.get("camera_movement"),
                duration=shot_data.get("duration"),
                content=shot_data.get("action"),
                dialogue=shot_data.get("dialogue"),
                image_prompt=shot_data.get("image_prompt"),
                video_prompt=shot_data.get("video_prompt"),
            )
            session.add(shot)

        state.setdefault("storyboards", {})[str(episode_number)] = sb
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()

        return {
            "episode_number": episode_number,
            "shot_count": len(shots),
            "storyboard": sb,
        }


async def generate_image_prompt(
    project_id: str,
    shot_id: str | None = None,
    description: str | None = None,
    character_name: str | None = None,
    node_id: str | None = None,
) -> dict:
    """Generate a text-to-image prompt for a given shot or arbitrary description."""
    from app.db.models import Shot, Character

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}

        shot = await session.get(Shot, shot_id) if shot_id else None
        appearance = ""
        if character_name:
            stmt = select(Character).where(
                Character.project_id == project_id,
                Character.name == character_name,
            ).order_by(Character.created_at.desc())
            char = (await session.exec(stmt)).first()
            if char:
                appearance = char.appearance or char.visual_prompt or ""

        # ⭐ 注入本集本段剧情上下文(分镜/首尾帧必须参考剧情,人物/场景不参考)
        story_context = ""
        if shot is not None:
            state = json.loads(project.state_json or "{}")
            ep_num = shot.episode_id
            seg_id = getattr(shot, "segment_id", None) or ""
            ep_script = state.get("episodes", {}).get(str(ep_num)) if ep_num else None
            seg = None
            if ep_num and seg_id:
                seg_list = state.get("segments", {}).get(str(ep_num), [])
                seg = next((s for s in seg_list if isinstance(s, dict) and s.get("id") == seg_id), None)
            parts = []
            if ep_script:
                synopsis = ep_script.get("synopsis") or ep_script.get("hook") or ""
                if synopsis:
                    parts.append(f"本集剧情概要:{str(synopsis)[:400]}")
            if seg:
                parts.append(f"本段剧情:{seg.get('plot','')[:400]}")
                if seg.get("segment_arc"):
                    parts.append(f"段落弧线:{seg['segment_arc']}")
                if seg.get("characters"):
                    parts.append(f"段内人物:{','.join(seg['characters']) if isinstance(seg['characters'], list) else seg['characters']}")
            if parts:
                story_context = "\n".join(parts) + "\n\n"

        user_prompt = (
            f"{story_context}"
            f"镜头描述：{description or (shot.content if shot else '')}\n"
            f"对白：{shot.dialogue if shot else ''}\n"
            f"景别：{shot.shot_type if shot else ''}\n"
            f"人物外貌：{appearance}\n"
        )

        # Route to appropriate template category based on context:
        # - character_name given → character_image
        # - shot with shot_type → storyboard_image
        # - else (pure description) → scene_image
        if character_name:
            template_category = "character_image"
        elif shot is not None:
            template_category = "storyboard_image"
        else:
            template_category = "scene_image"

        system_prompt = await resolve_prompt(
            "drama.generate_image_prompt", project_id, node_id,
            ctx=WorkerContext(
                project_id=project_id, node_id=node_id,
                episode_number=shot.episode_id if shot else None,
                extras={"character_name": character_name, "shot_id": shot_id},
            ),
        )
        system_prompt = system_prompt + _legacy_prompt_hint(
            template_category,
            query=" ".join([description or "", appearance or "", shot.content if shot else ""]),
        )

        svc = LLMService(session)
        result = await svc.generate(
            task_type="image_prompt_generation",
            messages=[{"role": "user", "content": user_prompt}],
            system=system_prompt,
            project_id=project_id,
        )
        data = _extract_json(result["content"], default={"prompt": result["content"]})
        if not isinstance(data, dict):
            data = {"prompt": str(data)}
        if not _is_actionable_image_prompt(data.get("prompt")):
            return {
                "error": "图片提示词生成结果不可执行，缺少有效画面描述",
                "error_kind": "invalid_image_prompt",
                "raw_prompt": str(data.get("prompt") or "")[:300],
            }

        if shot:
            shot.image_prompt = data.get("prompt", shot.image_prompt)
            session.add(shot)
            await session.commit()

        return {"shot_id": shot_id, **data}


async def generate_video_prompt(
    project_id: str,
    shot_id: str | None = None,
    description: str | None = None,
    first_frame_description: str | None = None,
    last_frame_description: str | None = None,
    node_id: str | None = None,
) -> dict:
    """Generate an image-to-video prompt describing motion between first and last frame."""
    from app.db.models import Shot

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}

        shot = await session.get(Shot, shot_id) if shot_id else None

        user_prompt = (
            f"镜头描述：{description or (shot.content if shot else '')}\n"
            f"首帧：{first_frame_description or ''}\n"
            f"尾帧：{last_frame_description or ''}\n"
            f"时长：{shot.duration if shot else 4} 秒\n"
        )

        svc = LLMService(session)
        system_prompt = await resolve_prompt(
            "drama.generate_video_prompt", project_id, node_id,
            ctx=WorkerContext(
                project_id=project_id, node_id=node_id,
                duration_seconds=int(shot.duration) if shot and shot.duration else None,
                extras={"shot_id": shot_id},
            ),
        )
        system_prompt = system_prompt + _legacy_prompt_hint(
            "video_prompt",
            query=" ".join([
                description or "",
                first_frame_description or "",
                last_frame_description or "",
            ]),
        )
        result = await svc.generate(
            task_type="video_prompt_generation",
            messages=[{"role": "user", "content": user_prompt}],
            system=system_prompt,
            project_id=project_id,
        )
        data = _extract_json(result["content"], default={"prompt": result["content"]})
        if not isinstance(data, dict):
            data = {"prompt": str(data)}

        if shot:
            shot.video_prompt = data.get("prompt", shot.video_prompt)
            session.add(shot)
            await session.commit()

        return {"shot_id": shot_id, **data}


async def parse_uploaded_script(
    project_id: str,
    text: str | None = None,
    upload_rel_path: str | None = None,
    episode_number: int | None = None,
    node_id: str | None = None,
) -> dict:
    """Parse a raw uploaded script (txt/docx already converted to text) into structured
    episode + scenes + characters and merge into project state.

    Either pass `text` directly, or pass `upload_rel_path` and the tool will
    read + extract the file via `file.extract_text_from_upload`.
    """
    if not text and upload_rel_path:
        from app.mcp_tools.file_tools import extract_text_from_upload

        extracted = await extract_text_from_upload(project_id, upload_rel_path)
        if extracted.get("error"):
            return {"error": f"无法读取附件 {upload_rel_path}: {extracted['error']}"}
        text = extracted.get("text", "")
    if not text:
        return {"error": "需要 text 或 upload_rel_path 之一"}

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}

        system = await resolve_prompt(
            "drama.parse_uploaded_script", project_id, node_id,
            ctx=WorkerContext(
                project_id=project_id, node_id=node_id,
                episode_number=episode_number,
            ),
        )
        user_prompt = f"剧本原文（前 12000 字符）：\n{text[:12000]}"

        svc = LLMService(session)
        result = await svc.generate(
            task_type="script_review",
            messages=[{"role": "user", "content": user_prompt}],
            system=system,
            project_id=project_id,
        )
        parsed = _extract_json(result["content"], default={})
        if not isinstance(parsed, dict):
            parsed = {}

        state = json.loads(project.state_json or "{}")
        ep_no = episode_number or (
            max([int(k) for k in state.get("episodes", {}).keys() if str(k).isdigit()] + [0]) + 1
        )

        ep_record = {
            "title": parsed.get("title", f"第{ep_no}集"),
            "summary": parsed.get("summary", ""),
            "cliffhanger": parsed.get("cliffhanger", ""),
            "script": text,
            "scenes": parsed.get("scenes", []),
        }
        state.setdefault("episodes", {})[str(ep_no)] = ep_record
        project.state_json = json.dumps(state, ensure_ascii=False)

        ep = Episode(
            project_id=project_id,
            episode_number=ep_no,
            title=ep_record["title"],
            hook="",
            summary=ep_record["summary"],
            script=text,
            cliffhanger=ep_record["cliffhanger"],
            status="done",
        )
        session.add(ep)
        session.add(project)
        await session.commit()

        return {"episode_number": ep_no, "parsed": parsed}


# ─────────────────────────────────────────────────────────────────────────
# Single-grain tools (one call = one artifact). Use these when the user
# asks for a single character / shot / image-prompt / video-prompt. The
# batch versions above stay for "give me a full set in one go".
# ─────────────────────────────────────────────────────────────────────────

async def generate_character(
    project_id: str,
    name: str | None = None,
    role_type: str | None = None,
    tier: str = "main",
    episode_number: int | None = None,
    segment_id: str | None = None,
    requirements: list[str] | None = None,
    node_id: str | None = None,
) -> dict:
    """Generate exactly one character. tier ∈ main/recurring/guest.
    Guest characters typically attach to an episode (and optionally a segment)."""
    if tier not in {"main", "recurring", "guest"}:
        tier = "main"
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}

        state = json.loads(project.state_json or "{}")
        metadata = state.get("metadata", {})
        existing = state.get("characters", [])

        constraint_lines: list[str] = []
        if name:
            constraint_lines.append(f"姓名必须是「{name}」")
        if role_type:
            constraint_lines.append(f"角色类型必须是「{role_type}」")
        if existing:
            constraint_lines.append(
                f"已有人物（不要重名）：{[c.get('name') for c in existing if isinstance(c, dict)]}"
            )

        # 节点级 requirements 优先,项目 metadata 仅在 requirements 为空时作为兜底世界观。
        # 之前 metadata 直接塞顶,会让旧项目的 genre/setting 反过来覆盖用户当前要求(舞者
        # 视频项目里冒出"林霄/玄冥"这种)。
        req_lines = _normalize_requirements(requirements)
        if req_lines:
            user_prompt = (
                f"用户当前要求(以此为准,优先级最高)：\n{chr(10).join(req_lines)}\n\n"
                f"约束：\n{chr(10).join(constraint_lines)}\n\n"
                "只生成 1 个人物。输出 JSON 对象（不是数组）。"
            )
        else:
            user_prompt = (
                f"项目信息(参考世界观)：\n{json.dumps(metadata, ensure_ascii=False, indent=2)}\n\n"
                f"约束：\n{chr(10).join(constraint_lines)}\n\n"
                "只生成 1 个人物。输出 JSON 对象（不是数组）。"
            )

        svc = LLMService(session)
        result = await svc.generate(
            task_type="character_generation",
            messages=[{"role": "user", "content": user_prompt}],
            system=await resolve_prompt(
                "drama.generate_character", project_id, node_id,
                ctx=WorkerContext(
                    project_id=project_id, node_id=node_id,
                    episode_number=episode_number,
                    extras={"tier": tier, "segment_id": segment_id, "name": name, "role_type": role_type},
                ),
            ),
            project_id=project_id,
        )
        data = _extract_single_character(
            _extract_json(result["content"], default={}),
            requirements=requirements,
        )

        data["tier"] = tier
        if episode_number:
            data["episode_number"] = episode_number
        if segment_id:
            data["segment_id"] = segment_id

        char = Character(
            project_id=project_id,
            name=data.get("name", name or ""),
            role_type=data.get("role_type", role_type or "support"),
            age=data.get("age"),
            identity=data.get("identity", ""),
            personality=data.get("personality", ""),
            appearance=data.get("appearance", ""),
            motivation=data.get("motivation", ""),
            relationship_json=json.dumps(
                data.get("relationships", {}), ensure_ascii=False
            ),
            visual_prompt=data.get("visual_prompt", ""),
        )
        session.add(char)

        existing.append(data)
        state["characters"] = existing
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()
        await session.refresh(char)

        return {"character": data, "character_id": char.id, "tier": tier}


async def generate_shot(
    project_id: str,
    episode_number: int,
    shot_number: int,
    scene_id: str | None = None,
    requirements: list[str] | None = None,
    node_id: str | None = None,
) -> dict:
    """Generate exactly one shot for a given episode + shot number."""
    from app.db.models import Shot

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}

        state = json.loads(project.state_json or "{}")
        script = state.get("episodes", {}).get(str(episode_number), {})
        characters = state.get("characters", [])

        ep_stmt = select(Episode).where(
            Episode.project_id == project_id,
            Episode.episode_number == episode_number,
        )
        episode = (await session.exec(ep_stmt)).first()
        episode_id = episode.id if episode else None

        user_prompt = (
            f"剧本上下文：\n{json.dumps(script, ensure_ascii=False)[:4000]}\n\n"
            f"人物：\n{json.dumps(characters, ensure_ascii=False)}\n\n"
            f"目标镜头编号：{shot_number}\n"
            f"额外要求：{chr(10).join(_normalize_requirements(requirements))}\n\n"
            "只生成 1 个分镜。输出 JSON 对象（包含 shot_number, shot_type, "
            "camera_movement, location, time_of_day, action, dialogue, duration, "
            "image_prompt, video_prompt）。"
        )

        svc = LLMService(session)
        result = await svc.generate(
            task_type="storyboard_generation",
            messages=[{"role": "user", "content": user_prompt}],
            system=await resolve_prompt(
                "drama.generate_shot", project_id, node_id,
                ctx=WorkerContext(
                    project_id=project_id, node_id=node_id,
                    episode_number=episode_number,
                    workflow_mode="frames",
                    extras={"shot_number": shot_number, "scene_id": scene_id},
                ),
            ),
            project_id=project_id,
        )
        data = _extract_json(result["content"], default={})
        if isinstance(data, dict) and "shots" in data and isinstance(data["shots"], list):
            data = data["shots"][0] if data["shots"] else {}
        if not isinstance(data, dict):
            data = {}

        shot = Shot(
            project_id=project_id,
            episode_id=episode_id,
            scene_id=scene_id,
            shot_number=int(data.get("shot_number", shot_number) or shot_number),
            shot_type=data.get("shot_type"),
            camera=data.get("camera_movement"),
            duration=data.get("duration"),
            content=data.get("action"),
            dialogue=data.get("dialogue"),
            image_prompt=data.get("image_prompt"),
            video_prompt=data.get("video_prompt"),
        )
        session.add(shot)
        await session.commit()
        await session.refresh(shot)

        return {"shot_id": shot.id, "shot": data}


async def generate_shot_image_prompt(
    project_id: str,
    shot_id: str,
    character_name: str | None = None,
    extra: str | None = None,
    node_id: str | None = None,
) -> dict:
    """Single-shot image-prompt generator. Thin wrapper over generate_image_prompt
    that requires a concrete shot_id (no free-form description path)."""
    return await generate_image_prompt(
        project_id=project_id,
        shot_id=shot_id,
        description=extra,
        character_name=character_name,
        node_id=node_id,
    )


async def generate_shot_video_prompt(
    project_id: str,
    shot_id: str,
    first_frame_description: str | None = None,
    last_frame_description: str | None = None,
    extra: str | None = None,
    node_id: str | None = None,
) -> dict:
    """Single-shot video-prompt generator."""
    return await generate_video_prompt(
        project_id=project_id,
        shot_id=shot_id,
        description=extra,
        first_frame_description=first_frame_description,
        last_frame_description=last_frame_description,
        node_id=node_id,
    )


# ─────────────────────────────────────────────────────────────────────────
# Segment tools — episodes are sliced into ~15s segments. Each segment has
# a plot summary, character set, and scene reference(s). Shots roll up to
# segments, not directly to episodes.
# ─────────────────────────────────────────────────────────────────────────

async def plan_episode_segments(
    project_id: str,
    episode_number: int,
    target_duration_seconds: int = 15,
    episode_duration_seconds: int | None = None,
    node_id: str | None = None,
) -> dict:
    """Slice an episode's script into ~target_duration_seconds segments.

    Output: list of segments with plot, characters, scene_refs (may be multiple
    if action moves locations within one segment), duration, segment_arc.
    """
    from app.services import drama_legacy

    return await drama_legacy.plan_episode_segments(
        project_id=project_id,
        episode_number=episode_number,
        target_duration_seconds=target_duration_seconds,
        episode_duration_seconds=episode_duration_seconds,
        node_id=node_id,
    )


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
    from app.services import drama_legacy

    return await drama_legacy.update_segment(
        project_id=project_id,
        episode_number=episode_number,
        segment_index=segment_index,
        plot=plot,
        characters=characters,
        scene_refs=scene_refs,
        duration_seconds=duration_seconds,
        segment_arc=segment_arc,
    )


async def set_segment_workflow_mode(
    project_id: str,
    episode_number: int,
    segment_index: int,
    mode: str,
) -> dict:
    """切换某个段落的视觉路径（grid 多宫格 / frames 首尾帧 / story_template 故事模板）。

    三模式互斥,每段只能选一种。切换后,已经按旧 mode 生成的产物保留在画布上,
    但后续工具调用按新 mode 校验。
    """
    from app.services import drama_legacy

    return await drama_legacy.set_segment_workflow_mode(
        project_id=project_id,
        episode_number=episode_number,
        segment_index=segment_index,
        mode=mode,
    )


async def assign_segment_scene(
    project_id: str,
    episode_number: int,
    segment_index: int,
    scene_id: str,
) -> dict:
    """Attach an existing scene (by id) to a segment's scene_refs."""
    from app.services import drama_legacy

    return await drama_legacy.assign_segment_scene(
        project_id=project_id,
        episode_number=episode_number,
        segment_index=segment_index,
        scene_id=scene_id,
    )


async def generate_segment_shots(
    project_id: str,
    episode_number: int,
    segment_index: int,
    requirements: list[str] | None = None,
    node_id: str | None = None,
) -> dict:
    """Generate a shot list for one segment. Shots get persisted with both
    episode_id and a segment_id reference (segment_id stored in input/output)."""
    from app.db.models import Shot

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}

        state = json.loads(project.state_json or "{}")
        segs = state.get("segments", {}).get(str(episode_number), [])
        target = next((s for s in segs if isinstance(s, dict) and s.get("index") == segment_index), None)
        if not target:
            return {"error": f"Segment {segment_index} of episode {episode_number} not found"}

        characters = state.get("characters", [])
        user_prompt = (
            f"段落剧情：{target.get('plot', '')}\n"
            f"出场人物：{json.dumps(target.get('characters', []), ensure_ascii=False)}\n"
            f"场景：{json.dumps(target.get('scene_refs') or target.get('scene_ids', []), ensure_ascii=False)}\n"
            f"段落时长：{target.get('duration_seconds', 15)} 秒\n"
            f"全部人物清单：{json.dumps([c.get('name') for c in characters if isinstance(c, dict)], ensure_ascii=False)}\n"
            f"额外要求：{chr(10).join(_normalize_requirements(requirements))}\n\n"
            "为该段落生成 1-3 个镜头,每镜约 5-10 秒。输出 JSON {shots: [...]},"
            "每个 shot 包含 shot_number, shot_type, camera_movement, location, "
            "time_of_day, action, dialogue, duration, image_prompt, video_prompt。"
        )

        svc = LLMService(session)
        result = await svc.generate(
            task_type="storyboard_generation",
            messages=[{"role": "user", "content": user_prompt}],
            system=await resolve_prompt(
                "drama.generate_segment_shots", project_id, node_id,
                ctx=WorkerContext(
                    project_id=project_id, node_id=node_id,
                    episode_number=episode_number,
                    segment_index=segment_index,
                    workflow_mode=target.get("workflow_mode") or "frames",
                    grid=target.get("grid"),
                    duration_seconds=target.get("duration_seconds", 15),
                ),
            ),
            project_id=project_id,
        )

        sb = _extract_json(result["content"], default={})
        if not isinstance(sb, dict):
            sb = {}
        shots = sb.get("shots", []) if isinstance(sb.get("shots"), list) else []

        ep_stmt = select(Episode).where(
            Episode.project_id == project_id,
            Episode.episode_number == episode_number,
        )
        episode = (await session.exec(ep_stmt)).first()
        episode_id = episode.id if episode else None

        segment_id = target.get("id") or f"ep{episode_number:02d}-seg{segment_index:02d}"
        target["id"] = segment_id

        shot_records = []
        for shot_data in shots:
            if not isinstance(shot_data, dict):
                continue
            shot_data["segment_id"] = segment_id
            shot = Shot(
                project_id=project_id,
                episode_id=episode_id,
                shot_number=int(shot_data.get("shot_number", 0) or 0),
                shot_type=shot_data.get("shot_type"),
                camera=shot_data.get("camera_movement"),
                duration=shot_data.get("duration"),
                content=shot_data.get("action"),
                dialogue=shot_data.get("dialogue"),
                image_prompt=shot_data.get("image_prompt"),
                video_prompt=shot_data.get("video_prompt"),
            )
            session.add(shot)
            await session.flush()
            shot_data["shot_id"] = shot.id
            shot_records.append(shot_data)

        target["shots"] = shot_records
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()

        return {
            "episode_number": episode_number,
            "segment_id": segment_id,
            "segment_index": segment_index,
            "shot_count": len(shot_records),
            "shots": shot_records,
        }


async def generate_storyboard_grid(
    project_id: str,
    episode_number: int,
    segment_index: int | None = None,
    layout: int = 4,
    node_id: str | None = None,
) -> dict:
    """STUB — generate a multi-cell storyboard grid image (4/6/9 cells).

    Preview-only. Does not feed downstream shot generation. Real backend
    (ComfyUI / Fal / MidJourney composite) is wired in P3.
    """
    if layout not in {4, 6, 9}:
        layout = 4
    return {
        "status": "stub",
        "layout": layout,
        "episode_number": episode_number,
        "segment_index": segment_index,
        "note": "storyboard_grid 是预览专用产物,不接入分镜生产链。后端待接入,当前返回占位。",
        "url": None,
    }


# ---------------------------------------------------------------------------
# Coordinated delete: keep state.* and the canvas graph in sync.
#
# These tools are the *only* sanctioned way to remove a character / episode /
# outline. They:
#   1. Strip the entry out of project.state_json
#   2. Remove the matching DB rows (Character / Episode / Shot)
#   3. Walk workflow_nodes by input_json link keys and delete the matching
#      nodes (plus their supersedes-chain children and any edges referencing
#      them).
#
# The orchestrator translates the returned `deleted_node_ids` into
# `canvas_action: delete_node` SSE events so the frontend store stays in sync.
# ---------------------------------------------------------------------------

from app.db.models import WorkflowEdge, WorkflowNode  # noqa: E402


def _node_input_field(node: WorkflowNode, key: str):
    if not node.input_json:
        return None
    try:
        data = json.loads(node.input_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return data.get(key)


async def _delete_nodes_by_ids(session, node_ids: set[str]) -> list[str]:
    """Delete nodes + their supersedes chain + edges. Returns deleted ids."""
    if not node_ids:
        return []

    # Expand to include nodes whose supersedes_id points at any to-delete node.
    pending = set(node_ids)
    while True:
        stmt = select(WorkflowNode).where(WorkflowNode.supersedes_id.in_(pending))
        chained = (await session.exec(stmt)).all()
        new_ids = {n.id for n in chained} - pending
        if not new_ids:
            break
        pending |= new_ids

    # Drop edges first to satisfy FK constraints.
    edge_stmt = select(WorkflowEdge).where(
        (WorkflowEdge.source_node_id.in_(pending))
        | (WorkflowEdge.target_node_id.in_(pending))
    )
    for edge in (await session.exec(edge_stmt)).all():
        await session.delete(edge)

    deleted: list[str] = []
    node_stmt = select(WorkflowNode).where(WorkflowNode.id.in_(pending))
    for node in (await session.exec(node_stmt)).all():
        deleted.append(node.id)
        await session.delete(node)
    return deleted


_CHARACTER_NODE_TYPES = {
    "character",
    "character_generation",
    "character_image_prompt",
    "character_reference_image",
    "character_relationship",
}

_EPISODE_NODE_TYPES = {
    "episode_script",
    "episode_review",
    "episode_segment_plan",
    "episode_export",
    "script_generation",
    "script_review",
    "storyboard_generation",
    "storyboard_grid",
    "segment",
    "scene",
    "scene_image",
    "scene_image_prompt",
    "shot",
    "shot_list",
    "shot_image_prompt",
    "shot_reference_image",
    "shot_first_frame",
    "shot_last_frame",
    "shot_video_prompt",
    "shot_video_clip",
    "image_prompt_generation",
    "image_generation",
    "video_prompt_generation",
    "video_generation",
}


async def delete_character(project_id: str, name: str) -> dict:
    """Remove a character from state + DB + canvas in one transaction.

    Matches canvas nodes by either input.character_name == name OR
    (for character_generation nodes) title startswith name.
    """
    if not name:
        return {"error": "name is required"}

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}

        # 1. Strip from state.characters
        state = json.loads(project.state_json or "{}")
        chars = state.get("characters", []) or []
        kept = [
            c for c in chars
            if not (isinstance(c, dict) and c.get("name") == name)
        ]
        removed_state = len(chars) - len(kept)
        if removed_state:
            state["characters"] = kept
            project.state_json = json.dumps(state, ensure_ascii=False)
            session.add(project)

        # 2. Delete Character rows
        char_stmt = select(Character).where(
            Character.project_id == project_id,
            Character.name == name,
        )
        char_rows = (await session.exec(char_stmt)).all()
        char_ids = {c.id for c in char_rows}
        for c in char_rows:
            await session.delete(c)

        # 3. Find matching canvas nodes
        node_stmt = select(WorkflowNode).where(
            WorkflowNode.project_id == project_id,
            WorkflowNode.type.in_(_CHARACTER_NODE_TYPES),
        )
        candidate_ids: set[str] = set()
        for node in (await session.exec(node_stmt)).all():
            cname = _node_input_field(node, "character_name")
            cid = _node_input_field(node, "character_id")
            if cname == name or (cid and cid in char_ids):
                candidate_ids.add(node.id)
                continue
            # Fallback for fusion character nodes where the name is in title.
            if node.type == "character" and node.title == name:
                candidate_ids.add(node.id)

        deleted_node_ids = await _delete_nodes_by_ids(session, candidate_ids)

        await session.commit()

        return {
            "ok": True,
            "name": name,
            "removed_from_state": removed_state,
            "deleted_character_rows": len(char_rows),
            "deleted_node_ids": deleted_node_ids,
        }


async def delete_episode_script(project_id: str, episode_number: int) -> dict:
    """Remove a single episode: state.episodes[N], Episode + Shot rows, and
    every canvas node whose input links to that episode number."""
    if episode_number is None:
        return {"error": "episode_number is required"}

    ep_key = str(episode_number)

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}

        # 1. Strip from state
        state = json.loads(project.state_json or "{}")
        mutated = False
        for section in ("episodes", "reviews", "storyboards", "segments"):
            sec = state.get(section)
            if isinstance(sec, dict) and ep_key in sec:
                sec.pop(ep_key, None)
                mutated = True
        if mutated:
            project.state_json = json.dumps(state, ensure_ascii=False)
            session.add(project)

        # 2. Drop DB rows. Episode → Shot (FK) order.
        ep_stmt = select(Episode).where(
            Episode.project_id == project_id,
            Episode.episode_number == episode_number,
        )
        episodes = (await session.exec(ep_stmt)).all()
        episode_ids = {e.id for e in episodes}

        from app.db.models import Shot  # local import to dodge cycle
        if episode_ids:
            shot_stmt = select(Shot).where(Shot.episode_id.in_(episode_ids))
            for shot in (await session.exec(shot_stmt)).all():
                await session.delete(shot)
        for ep in episodes:
            await session.delete(ep)

        # 3. Match canvas nodes by input.episode_number
        node_stmt = select(WorkflowNode).where(
            WorkflowNode.project_id == project_id,
            WorkflowNode.type.in_(_EPISODE_NODE_TYPES),
        )
        candidate_ids: set[str] = set()
        for node in (await session.exec(node_stmt)).all():
            ep_num = _node_input_field(node, "episode_number")
            if ep_num == episode_number:
                candidate_ids.add(node.id)

        deleted_node_ids = await _delete_nodes_by_ids(session, candidate_ids)

        await session.commit()

        return {
            "ok": True,
            "episode_number": episode_number,
            "removed_from_state": mutated,
            "deleted_episode_rows": len(episodes),
            "deleted_node_ids": deleted_node_ids,
        }


async def delete_outline(project_id: str) -> dict:
    """Clear state.outline and delete outline-generation nodes. Episodes are
    preserved so the user doesn't lose their scripts by accident."""
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}

        state = json.loads(project.state_json or "{}")
        had_outline = "outline" in state
        if had_outline:
            state.pop("outline", None)
            project.state_json = json.dumps(state, ensure_ascii=False)
            session.add(project)

        node_stmt = select(WorkflowNode).where(
            WorkflowNode.project_id == project_id,
            WorkflowNode.type.in_({"outline", "outline_generation"}),
        )
        candidate_ids = {n.id for n in (await session.exec(node_stmt)).all()}

        deleted_node_ids = await _delete_nodes_by_ids(session, candidate_ids)

        await session.commit()
        return {
            "ok": True,
            "had_outline": had_outline,
            "deleted_node_ids": deleted_node_ids,
        }


async def reset_project(
    project_id: str,
    scope: str = "failed",
    _confirm_token: str = "",
    reason: str | None = None,
    new_theme: dict | None = None,
) -> dict:
    """Canvas + state reset entry. ONE tool, automatic canvas sync.

    scope='failed': 只删 status=failed 且没真正产出过的节点（cleanup 等价物）。
                    state 不动,用于清理测试残骸。**这是默认且安全的清理。**
    scope='full':   清空 state.characters/outline/episodes/segments/scenes
                    + state.metadata 里的题材字段(genre/description/world_setting)
                    + 顶级列 genre/description
                    + 删项目下所有 workflow_nodes / workflow_edges。
                    用户明说"重置项目""清空画布""全部删除""换主题"才走这条。
                    **禁止 agent 擅自传 _confirm_token** —— 这个参数只有
                    后端在 state 存在待确认 reset 且模型再次调用 reset 时注入。
                    agent 调用 scope='full' 时会直接返回 requires_user_confirm,
                    然后 agent 必须等待用户下一轮明确决定；确认时由模型再次调用 reset。

    _confirm_token: **仅限 orchestrator 内部使用,agent 不可传此参数。**
    reason:     scope='full' 时建议附原因摘要(展示给用户)。
    new_theme:  scope='full' 同时切换主题用,可选 dict,字段:
                  title / genre / description / format / episode_count /
                  duration_per_episode / budget_level
                清完旧数据后写入 metadata + 顶级列,避免"清→换"两步之间产生
                "无主题孤儿状态"或污染下次生成。

    Returns:
      {ok, scope, deleted_node_ids, cleared_all, deleted_edges, state_keys_cleared,
       new_theme_applied}
      或 {requires_user_confirm:True, ...} 等待用户拍板。
    Orchestrator 据此发画布事件:cleared_all=True → canvas_action:clear_all,
    否则 deleted_node_ids 逐个 → canvas_action:delete_node。
    """
    import hashlib, hmac, time

    if scope not in {"failed", "full"}:
        return {"error": f"scope must be 'failed' or 'full', got {scope!r}"}

    # Quick pre-check for full reset: if project is already barren, skip
    # confirmation and return success immediately. This prevents the LLM
    # from getting stuck in a confirm loop on an already-empty project.
    if scope == "full" and not _confirm_token:
        async with session_scope() as session:
            project = await session.get(Project, project_id)
            if project:
                state = json.loads(project.state_json or "{}")
                has_resettable_context = any(key in state for key in _FULL_RESET_CONTEXT_KEYS)
                has_content = bool(
                    state.get("characters")
                    or state.get("episodes")
                    or state.get("outline")
                    or state.get("segments")
                    or state.get("scenes")
                    or state.get("project_blueprint")
                    or state.get("blueprint_progress")
                    or state.get("pending_blueprint_intake")
                    or has_resettable_context
                    or project.title != UNTITLED_PROJECT_TITLE
                )
                if not has_content:
                    # Also check for actual DB rows
                    char_count = (await session.exec(
                        select(Character).where(Character.project_id == project_id)
                    )).first()
                    ep_count = (await session.exec(
                        select(Episode).where(Episode.project_id == project_id)
                    )).first()
                    node_count = (await session.exec(
                        select(WorkflowNode).where(WorkflowNode.project_id == project_id)
                    )).first()
                    if not char_count and not ep_count and not node_count:
                        archived_messages = await _archive_project_chat_messages(session, project_id)
                        await session.commit()
                        return {
                            "ok": True,
                            "scope": "full",
                            "deleted_node_ids": [],
                            "deleted_edges": 0,
                            "cleared_all": False,
                            "state_keys_cleared": [],
                            "archived_messages": archived_messages,
                            "note": "Project was already empty, nothing to reset.",
                        }

    # 全量重置守卫:agent 不能擅自清空整个画布,必须经用户确认。
    # _confirm_token 由后端在 state 存在待确认 reset 且模型再次调用 reset 后注入,
    # 格式为 HMAC(project_id + timestamp, secret)。agent 无法伪造。
    if scope == "full":
        token_valid = False
        if _confirm_token and len(_confirm_token) > 20:
            try:
                parts = _confirm_token.split(":")
                if len(parts) == 2:
                    ts_str, sig = parts
                    ts = int(ts_str)
                    # token 有效期 120 秒
                    if abs(time.time() - ts) < 120:
                        secret = (project_id or "drama-studio").encode()
                        expected = hmac.new(secret, f"{project_id}:{ts}".encode(), hashlib.sha256).hexdigest()[:32]
                        if hmac.compare_digest(sig, expected):
                            token_valid = True
            except (ValueError, TypeError):
                pass

        if not token_valid:
            return {
                "ok": False,
                "requires_user_confirm": True,
                "scope": "full",
                "reason": reason or "agent 请求清空整个项目(画布+state),需用户确认",
                "hint": (
                    "这是破坏性操作:会删除所有节点、连边、人物、剧本、分镜等。"
                    "若确实要重置,请等待用户下一轮明确确认后再次调用。"
                    "如果只是想清理失败/测试节点,改用 scope='failed'(默认)。"
                ),
            }

    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}

        if scope == "failed":
            node_stmt = select(WorkflowNode).where(
                WorkflowNode.project_id == project_id,
                WorkflowNode.status == "failed",
            )
            candidates = (await session.exec(node_stmt)).all()
            target_ids: set[str] = set()
            for n in candidates:
                if n.output_json and n.output_json.strip() not in ("", "null", "{}"):
                    continue
                target_ids.add(n.id)

            edge_stmt = select(WorkflowEdge).where(
                (WorkflowEdge.source_node_id.in_(target_ids))
                | (WorkflowEdge.target_node_id.in_(target_ids))
            ) if target_ids else None
            edges = (await session.exec(edge_stmt)).all() if edge_stmt is not None else []
            for e in edges:
                await session.delete(e)

            deleted_ids = await _delete_nodes_by_ids(session, target_ids)
            await session.commit()
            return {
                "ok": True,
                "scope": "failed",
                "deleted_node_ids": deleted_ids,
                "deleted_edges": len(edges),
                "cleared_all": False,
                "state_keys_cleared": [],
            }

        state = json.loads(project.state_json or "{}")
        cleared_keys: list[str] = []
        # 内容产物:整段抹掉
        for key in _FULL_RESET_CONTEXT_KEYS:
            if key in state:
                state.pop(key, None)
                cleared_keys.append(key)
        cleared_keys.extend(clear_blueprint_state(state))
        # 主题向字段:metadata 内容字段 + story_bible 全部清空(保留 metadata 的容量类
        # 字段如 episode_count/duration/format/budget_level,免得用户连排版偏好都丢)
        meta = state.get("metadata") or {}
        if isinstance(meta, dict):
            meta["title"] = UNTITLED_PROJECT_TITLE
            cleared_keys.append("metadata.title")
            for k in ("genre", "description", "logline", "theme", "world_setting"):
                if meta.get(k):
                    meta[k] = ""
                    cleared_keys.append(f"metadata.{k}")
            state["metadata"] = meta
        if "story_bible" in state:
            state["story_bible"] = {
                "logline": "", "theme": "", "tone": "",
                "world_setting": "", "visual_style": "",
            }
            cleared_keys.append("story_bible")

        # 顶级列同样要清,否则 project_list / runtime_context 里 genre 还在
        project.title = UNTITLED_PROJECT_TITLE
        project.genre = None
        project.description = None

        # new_theme:一站式切换。覆盖 metadata + 顶级列,避免"清→改"两步竞态
        applied_theme: dict = {}
        if isinstance(new_theme, dict):
            meta = state.get("metadata") or {}
            for k in ("title", "genre", "description", "format",
                      "episode_count", "duration_per_episode", "budget_level"):
                if k in new_theme and new_theme[k] is not None:
                    meta[k] = new_theme[k]
                    applied_theme[k] = new_theme[k]
                    if hasattr(project, k):
                        setattr(project, k, new_theme[k])
            state["metadata"] = meta

        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)

        from app.db.models import Character as _Char, Episode as _Ep, Shot as _Shot
        for model_cls in (_Shot, _Ep, _Char):
            stmt = select(model_cls).where(model_cls.project_id == project_id)
            for row in (await session.exec(stmt)).all():
                await session.delete(row)

        edge_stmt = select(WorkflowEdge).where(WorkflowEdge.project_id == project_id)
        edges = (await session.exec(edge_stmt)).all()
        for e in edges:
            await session.delete(e)

        node_stmt = select(WorkflowNode).where(WorkflowNode.project_id == project_id)
        nodes = (await session.exec(node_stmt)).all()
        deleted_ids = [n.id for n in nodes]
        for n in nodes:
            await session.delete(n)

        archived_messages = await _archive_project_chat_messages(session, project_id)
        await session.commit()

        # 删除旧执行清单文件；任务状态现在由 task_graph/节点状态承担。
        try:
            (Path(settings.PROJECT_ROOT) / "data" / "projects" / project_id / "checklist.md").unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass
        try:
            from app.agent.task_graph import task_graph
            task_graph.clear_project(project_id)
        except Exception:
            pass
        blueprint_file_cleanup = delete_blueprint_files_report(project_id)
        deleted_blueprint_files = list(blueprint_file_cleanup.get("deleted") or [])
        blueprint_file_delete_errors = list(blueprint_file_cleanup.get("errors") or [])

        return {
            "ok": True,
            "scope": "full",
            "deleted_node_ids": deleted_ids,
            "deleted_edges": len(edges),
            "cleared_all": True,
            "state_keys_cleared": cleared_keys,
            "new_theme_applied": applied_theme,
            "blueprint_cleared": True,
            "deleted_blueprint_files": deleted_blueprint_files,
            "blueprint_file_delete_errors": blueprint_file_delete_errors,
            "archived_messages": archived_messages,
            "title": applied_theme.get("title") or UNTITLED_PROJECT_TITLE,
        }


async def plan_episode_cast_scene(
    project_id: str,
    episode_number: int,
    node_id: str | None = None,
) -> dict:
    """规划一集的出场人物 + 场景 + 段落分配。

    输入：剧本 + 切段方案。
    输出：{cast: [...], scenes: [...], segment_assignments: {seg_index: {cast: [...], scenes: [...]}}}
    并落到 state.episodes[N].cast_scene_plan，方便后续段落级工具读取。
    """
    from app.services import drama_legacy

    return await drama_legacy.plan_episode_cast_scene(
        project_id=project_id,
        episode_number=episode_number,
        node_id=node_id,
    )


async def generate_segment_video_prompt(
    project_id: str,
    segment_id: str,
    node_id: str | None = None,
) -> dict:
    """段落级视频提示词生成。

    引用该段落的图清单（多宫格 / 首尾帧 / 故事模板，三选一已落到节点）+ 段落剧情,
    产出 image-to-video 的 prompt 数据,后续由 segment_video_clip / media_generation service 使用。
    """
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}
        state = json.loads(project.state_json or "{}")

        target_seg = None
        target_episode = None
        for ep_num, segs in (state.get("segments") or {}).items():
            if not isinstance(segs, list):
                continue
            for seg in segs:
                if isinstance(seg, dict) and seg.get("id") == segment_id:
                    target_seg = seg
                    target_episode = ep_num
                    break
            if target_seg:
                break
        if not target_seg:
            return {"error": f"找不到段落 {segment_id}"}

        workflow_mode = target_seg.get("workflow_mode") or "grid"
        plot = target_seg.get("plot", "")
        duration = target_seg.get("duration_seconds", 15)

        # 把该段落的分镜内容(cells / shots / 整段 prompt)拉出来塞进 user_prompt
        # —— LLM 看不到分镜表就只能空洞地写镜头,根因是这里没投喂
        from app.db.models import WorkflowNode
        from sqlmodel import select as _select
        storyboard_block = ""
        sb_stmt = _select(WorkflowNode).where(
            WorkflowNode.project_id == project_id,
            WorkflowNode.type == "segment_storyboard",
        )
        sb_rows = (await session.exec(sb_stmt)).all()
        for sb in sb_rows:
            try:
                sb_input = json.loads(sb.input_json or "{}")
            except (json.JSONDecodeError, TypeError):
                sb_input = {}
            same_ep = str(sb_input.get("episode_number")) == str(target_episode)
            same_idx = sb_input.get("segment_index") == target_seg.get("index")
            if same_ep and same_idx:
                try:
                    sb_out = json.loads(sb.output_json or "{}")
                except (json.JSONDecodeError, TypeError):
                    sb_out = {}
                sb_prompt = (sb_out.get("prompt") or sb_input.get("prompt") or sb.prompt or "")
                cells = sb_out.get("cells") or []
                shots = sb_out.get("shots") or []
                parts = []
                if sb_prompt:
                    parts.append(f"分镜整体描述:\n{sb_prompt[:2000]}")
                if cells:
                    parts.append("逐格(grid 模式):\n" + "\n".join(
                        f"  - {c.get('row','?')}行{c.get('col','?')}列 [{c.get('shot_type','')}]: "
                        f"{c.get('content','')}"
                        + (f" 台词「{c.get('dialogue')}」" if c.get('dialogue') else "")
                        for c in cells
                    ))
                if shots:
                    parts.append("逐镜(shot_list 模式):\n" + "\n".join(
                        f"  - 镜 {s.get('index', i+1)} [{s.get('shot_type','')}] "
                        f"持续 {s.get('duration', '?')}s: {s.get('action','')}"
                        + (f" 台词「{s.get('dialogue')}」" if s.get('dialogue') else "")
                        for i, s in enumerate(shots)
                    ))
                if parts:
                    storyboard_block = "\n\n".join(parts)
                break

        user_prompt = (
            f"段落 ID:{segment_id}(第 {target_episode} 集第 {target_seg.get('index')} 段)\n"
            f"工作流模式:{workflow_mode}(grid=多宫格 / frames=首尾帧 / story_template=故事模板)\n"
            f"剧情:{plot}\n"
            f"段落总时长:约 {duration} 秒\n\n"
            + (f"### 分镜表(必须按这个写视频提示词)\n{storyboard_block}\n\n" if storyboard_block else "")
            + "### 输出要求(铁律)\n"
            "video_prompt 必须**按分镜逐镜**写成一段连续的文字脚本,**每镜必含**:\n"
            "  1) 景别(特写/中景/远景/全景/航拍)\n"
            "  2) 主体动作(动词主导,具体到肢体或表情)\n"
            "  3) 摄影机运动(推/拉/摇/移/跟/升降/环绕/手持/Steadicam,任何一个明确的运动)\n"
            "  4) 持续秒数(N.Ns,所有镜头加总要严格等于段落总时长)\n"
            "  5) 转场到下一镜的方式(直切/淡入淡出/匹配剪辑/划像 等)\n"
            "如果有台词必须保留;不要写画风/颜色/光影(这些已经在分镜图里),只关注**动作和镜头**。\n\n"
            "输出 JSON {\n"
            '  "prompt": "镜1 [中景] [推镜头] [3.0s] ...; 镜2 [特写] ...; 镜3 [...] ...",\n'
            '  "motion_hints": ["推镜头","横摇","跟移"],\n'
            '  "camera": "整段摄影风格说明",\n'
            '  "audio_hint": "环境音/配乐/SFX",\n'
            '  "shots": [{"index":1,"shot_type":"中景","action":"...","camera":"推镜头","duration":3.0,"transition":"直切"}, ...]\n'
            "}\n仅 JSON,不要任何额外文字。"
        )
        svc = LLMService(session)
        result = await svc.generate(
            task_type="video_prompt_generation",
            messages=[{"role": "user", "content": user_prompt}],
            system=await resolve_prompt(
                "drama.generate_segment_video_prompt", project_id, node_id,
                ctx=WorkerContext(
                    project_id=project_id, node_id=node_id,
                    episode_number=int(target_episode) if target_episode else None,
                    segment_index=target_seg.get("index"),
                    workflow_mode=workflow_mode,
                    grid=target_seg.get("grid"),
                    duration_seconds=duration,
                    extras={"segment_id": segment_id},
                ),
            ),
            project_id=project_id,
        )
        data = _extract_json(result["content"], default={"prompt": result["content"]})
        if not isinstance(data, dict):
            data = {"prompt": str(data)}

        target_seg["video_prompt"] = data.get("prompt")
        target_seg["video_prompt_meta"] = data
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()

        return {
            "segment_id": segment_id,
            "episode_number": int(target_episode) if target_episode else None,
            "workflow_mode": workflow_mode,
            **data,
        }

