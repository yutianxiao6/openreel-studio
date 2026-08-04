"""Drama generation MCP-style tools."""
from __future__ import annotations

import json
import re
from pathlib import Path

from sqlmodel import select

from app.config import settings
from app.db.models import Episode, Message, Project, WorkflowEdge, WorkflowNode
from app.db.session import session_scope
from app.prompts import resolve_prompt
from app.prompts._section import WorkerContext
from app.services.llm_service import LLMService
from app.services.node_public_ids import public_node_id_from_model


UNTITLED_PROJECT_TITLE = "未命名项目"


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
    "_last_agent_review",
    "project_mode",
    "project_sub_mode",
    "selected_video_mode",
    "pending_video_mode_choice",
    "pending_video_brief",
    "pending_video_request",
    "_pending_reset_confirm",
    "_pending_tool_confirm",
    "agent_token_usage",
    "reference_assets",
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

        pages: list[str] = []
        next_offset: int | None = 0
        while next_offset is not None and sum(len(page) for page in pages) < 12_000:
            extracted = await extract_text_from_upload(
                project_id,
                upload_rel_path,
                offset=next_offset,
                limit=min(8_000, 12_000 - sum(len(page) for page in pages)),
            )
            if extracted.get("error"):
                return {"error": f"无法读取附件 {upload_rel_path}: {extracted['error']}"}
            content_page = extracted.get("content_page")
            if not isinstance(content_page, dict):
                break
            pages.append(str(content_page.get("content") or ""))
            raw_next = content_page.get("next_offset")
            next_offset = int(raw_next) if raw_next is not None else None
        text = "".join(pages)
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
    import hashlib
    import hmac
    import time

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
                    or has_resettable_context
                    or project.title != UNTITLED_PROJECT_TITLE
                )
                if not has_content:
                    # Also check for actual DB rows
                    ep_count = (await session.exec(
                        select(Episode).where(Episode.project_id == project_id)
                    )).first()
                    node_count = (await session.exec(
                        select(WorkflowNode).where(WorkflowNode.project_id == project_id)
                    )).first()
                    if not ep_count and not node_count:
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
            public_ids_by_internal: dict[str, str] = {}
            for n in candidates:
                if n.output_json and n.output_json.strip() not in ("", "null", "{}"):
                    continue
                target_ids.add(n.id)
                public_ids_by_internal[n.id] = public_node_id_from_model(n)

            edge_stmt = select(WorkflowEdge).where(
                (WorkflowEdge.source_node_id.in_(target_ids))
                | (WorkflowEdge.target_node_id.in_(target_ids))
            ) if target_ids else None
            edges = (await session.exec(edge_stmt)).all() if edge_stmt is not None else []
            for e in edges:
                await session.delete(e)

            deleted_ids = await _delete_nodes_by_ids(session, target_ids)
            public_deleted_ids = [public_ids_by_internal.get(node_id, node_id) for node_id in deleted_ids]
            await session.commit()
            return {
                "ok": True,
                "scope": "failed",
                "deleted_node_ids": public_deleted_ids,
                "_canvas_deleted_node_ids": deleted_ids,
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

        # 顶级列同样要清，否则项目状态摘要里仍会残留 genre。
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

        for model_cls in (Episode,):
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
        public_deleted_ids = [public_node_id_from_model(n) for n in nodes]
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
        return {
            "ok": True,
            "scope": "full",
            "deleted_node_ids": public_deleted_ids,
            "_canvas_deleted_node_ids": deleted_ids,
            "deleted_edges": len(edges),
            "cleared_all": True,
            "state_keys_cleared": cleared_keys,
            "new_theme_applied": applied_theme,
            "archived_messages": archived_messages,
            "title": applied_theme.get("title") or UNTITLED_PROJECT_TITLE,
        }
