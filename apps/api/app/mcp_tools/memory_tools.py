"""Memory tools — three layers of memory for the Agent.

1. Short-term: active unarchived chat history until context compaction is needed.
     Compaction replaces old transcript with a background summary plus a
     token-budgeted concrete tail; it does not use a sliding message window.
2. Project memory: durable facts scoped to one project
     - storage: state.memory.facts (list of {id, kind, content, created_at})
     - tools:   memory.save_fact / recall / forget / summarize_conversation
3. User memory: cross-project preferences (style, naming, model, taste)
     - storage: user_memory table (id, kind, content, source_project_id?, hits, ...)
     - tools:   memory.save_user_fact / recall_user / forget_user / list_user

The agent should:
  - call save_user_fact when the user voices a *durable preference* across
    projects ("以后女主名字都用苏晚", "我习惯用 deepseek 写大纲")
  - call save_fact for project-scoped decisions (世界观、人物关系、调性)
  - rely on the orchestrator to auto-inject active short-term context
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from sqlmodel import select

from app.db.models import Message, Project, UserMemory
from app.db.session import session_scope


def _message_metadata(message: Message) -> dict:
    metadata_json = getattr(message, "metadata_json", None)
    if not metadata_json:
        return {}
    try:
        parsed = json.loads(metadata_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ── Project-scoped memory ────────────────────────────────────────────

def memory_summarization_messages(tail_messages: list[dict]) -> list[dict]:
    """Return only user-authored text eligible for durable memory extraction."""
    return [
        {"role": "user", "content": str(message.get("content") or "")}
        for message in tail_messages
        if isinstance(message, dict)
        and message.get("role") == "user"
        and str(message.get("content") or "").strip()
    ]


async def memory_save_fact(
    project_id: str,
    content: str,
    kind: str = "note",
    pinned: bool = False,
) -> dict:
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}
        state = json.loads(project.state_json or "{}")
        memory = state.setdefault("memory", {"facts": []})
        # de-dup: same kind + identical content → skip (re-pin if requested)
        for f in memory["facts"]:
            if f.get("kind") == kind and f.get("content") == content:
                if pinned and not f.get("pinned"):
                    f["pinned"] = True
                    project.state_json = json.dumps(state, ensure_ascii=False)
                    session.add(project)
                    await session.commit()
                return f
        fact = {
            "id": str(uuid.uuid4()),
            "kind": kind,
            "content": content,
            "pinned": pinned,
            "created_at": datetime.utcnow().isoformat(),
        }
        memory["facts"].append(fact)
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()
        return fact


async def memory_pin_fact(project_id: str, fact_id: str, pinned: bool = True) -> dict:
    """Pin/unpin a fact so it's never dropped during recall trimming."""
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}
        state = json.loads(project.state_json or "{}")
        facts = state.get("memory", {}).get("facts", [])
        for f in facts:
            if f.get("id") == fact_id:
                f["pinned"] = pinned
                project.state_json = json.dumps(state, ensure_ascii=False)
                session.add(project)
                await session.commit()
                return {"ok": True, "id": fact_id, "pinned": pinned}
        return {"ok": False, "error": "fact not found"}


async def memory_recall(
    project_id: str,
    kind: str | None = None,
    limit: int = 50,
) -> list[dict]:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return []
        state = json.loads(project.state_json or "{}")
        facts = state.get("memory", {}).get("facts", [])
        if kind:
            facts = [f for f in facts if f.get("kind") == kind]
        # Pinned first, then most recent. Pinned never trimmed.
        pinned = [f for f in facts if f.get("pinned")]
        unpinned = [f for f in facts if not f.get("pinned")]
        return pinned + unpinned[-max(0, limit - len(pinned)):]


async def memory_forget(project_id: str, fact_id: str) -> dict:
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if not project:
            return {"error": "Project not found"}
        state = json.loads(project.state_json or "{}")
        facts = state.get("memory", {}).get("facts", [])
        before = len(facts)
        facts = [f for f in facts if f.get("id") != fact_id]
        state.setdefault("memory", {})["facts"] = facts
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()
        return {"removed": before - len(facts)}


async def memory_summarize_conversation(
    project_id: str,
    tail_messages: list[dict],
) -> dict:
    """Extract durable user-stated facts from a conversation tail."""
    from app.services.llm_service import LLMService

    if not tail_messages:
        return {"facts": []}

    user_messages = memory_summarization_messages(tail_messages)
    if not user_messages:
        return {"facts": []}

    system = (
        "你是一个长期记忆压缩器。只从用户消息中提取用户明确确认、明确要求记住、"
        "或明确表示以后复用的稳定事实。忽略助手草稿、助手推演、工具结果、失败方案、"
        "待确认蓝图、临时 intake 表单答案和普通创作请求。不要把助手生成的剧情、人物、"
        "场景、分段或提示词写成长期事实；已确认蓝图由项目蓝图保存。"
        "没有符合条件的事实就输出空数组 []。最多 6 条,输出 JSON 数组:[\"事实1\", \"事实2\"]。"
    )
    user = json.dumps(user_messages, ensure_ascii=False)

    async with session_scope() as session:
        svc = LLMService(session)
        result = await svc.generate(
            task_type="agent_loop",
            messages=[{"role": "user", "content": user}],
            system=system,
            project_id=project_id,
        )
    try:
        text = result["content"].strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        facts = json.loads(text)
        if not isinstance(facts, list):
            facts = []
    except Exception:
        facts = []

    saved = []
    for f in facts:
        if isinstance(f, str) and f.strip():
            saved.append(await memory_save_fact(project_id, f.strip(), kind="summary"))
    return {"facts": saved}


async def memory_compact_context(
    project_id: str,
    target_tail_tokens: int | None = None,
) -> dict:
    """Persist a compacted background summary and archive replaced messages."""
    from app.agent.context_compact import (
        PRESERVED_TAIL_TOKEN_BUDGET,
        auto_compact_needed,
        build_compact_summary_prompt,
        compact_preserved_tail,
        compacted_context_ack_message,
        compacted_context_message,
        estimate_tokens,
        save_transcript,
    )
    from app.services.llm_service import LLMService

    try:
        tail_token_budget = int(target_tail_tokens or PRESERVED_TAIL_TOKEN_BUDGET)
    except (TypeError, ValueError):
        tail_token_budget = PRESERVED_TAIL_TOKEN_BUDGET
    tail_token_budget = max(0, min(tail_token_budget, 50000))
    async with session_scope() as session:
        result = await session.exec(
            select(Message)
            .where(Message.project_id == project_id, Message.archived == False)  # noqa: E712
            .order_by(Message.created_at)
        )
        active = list(result.all())

    payload = [
        {
            "role": m.role,
            "content": m.content,
            "_message_id": m.id,
            "_metadata": _message_metadata(m),
        }
        for m in active
        if m.role in ("user", "assistant")
    ]
    active_tokens = estimate_tokens(payload)
    if not auto_compact_needed(payload):
        return {
            "archived": 0,
            "active": len(active),
            "active_tokens": active_tokens,
            "transcript": None,
            "facts": [],
            "summary_inserted": False,
            "reason": "below_token_threshold",
        }

    transcript = save_transcript(payload, project_id)
    facts_result = {"facts": []}
    fact_error = None
    if payload:
        try:
            facts_result = await memory_summarize_conversation(project_id, payload)
        except Exception as exc:
            fact_error = str(exc)

    summary_prompt = build_compact_summary_prompt(payload)
    async with session_scope() as session:
        svc = LLMService(session)
        summary_result = await svc.generate(
            task_type="agent_loop",
            messages=[{"role": "user", "content": summary_prompt}],
            system="You are a conversation summarizer. Be concise.",
            project_id=project_id,
        )
    summary_text = str(summary_result.get("content") or "").strip()
    if not summary_text:
        summary_text = "历史上下文已压缩；继续前请以项目状态、蓝图、任务和节点工具为准。"

    preserved_tail = compact_preserved_tail(
        payload,
        token_budget=tail_token_budget,
    )
    preserved_ids = {
        str(message.get("_message_id"))
        for message in preserved_tail
        if message.get("_message_id")
    }
    compacted_messages = [
        compacted_context_message(summary_text),
        compacted_context_ack_message(),
    ]
    for message in preserved_tail:
        compacted_messages.append({
            "role": str(message.get("role") or ""),
            "content": str(message.get("content") or ""),
            "_metadata": message.get("_metadata") if isinstance(message.get("_metadata"), dict) else {},
        })

    async with session_scope() as session:
        archived_count = 0
        for message in active:
            row = await session.get(Message, message.id)
            if row is not None:
                row.archived = True
                session.add(row)
                archived_count += 1
        for message in compacted_messages:
            role = str(message.get("role") or "")
            content = str(message.get("content") or "")
            if role not in {"user", "assistant"} or not content:
                continue
            metadata = dict(message.get("_metadata") or {}) if isinstance(message.get("_metadata"), dict) else {}
            metadata["kind"] = "compacted_context" if "<compacted_context" in content else "compacted_tail"
            metadata["source"] = "memory.compact_context"
            metadata["transcript"] = str(transcript)
            session.add(Message(
                project_id=project_id,
                role=role,
                content=content,
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            ))
        await session.commit()

    return {
        "archived": archived_count,
        "active": len(compacted_messages),
        "active_tokens_before": active_tokens,
        "summary_inserted": True,
        "preserved_tail_messages": len(preserved_tail),
        "preserved_tail_tokens": estimate_tokens(preserved_tail),
        "preserved_message_ids": sorted(preserved_ids),
        "transcript": str(transcript),
        "facts": facts_result.get("facts", []),
        "fact_error": fact_error,
        "summary_usage": summary_result.get("usage") if isinstance(summary_result, dict) else None,
    }


# ── User-scoped (cross-project) memory ───────────────────────────────

async def memory_save_user_fact(
    content: str,
    kind: str = "preference",
    source_project_id: str | None = None,
) -> dict:
    """Save a fact that should apply to *all* projects (preference/style/naming/model)."""
    async with session_scope() as session:
        # de-dup: same kind + identical content → merge (bump hits)
        result = await session.exec(
            select(UserMemory).where(
                UserMemory.kind == kind, UserMemory.content == content
            )
        )
        existing = result.first()
        if existing:
            existing.hits += 1
            existing.last_used_at = datetime.utcnow()
            session.add(existing)
            await session.commit()
            return existing.model_dump()

        item = UserMemory(
            kind=kind,
            content=content,
            source_project_id=source_project_id,
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return item.model_dump()


async def memory_recall_user(
    kind: str | None = None,
    limit: int = 30,
) -> list[dict]:
    """List user-level memories, most-recently-used first."""
    async with session_scope() as session:
        stmt = select(UserMemory)
        if kind:
            stmt = stmt.where(UserMemory.kind == kind)
        stmt = stmt.order_by(UserMemory.last_used_at.desc().nullslast(),
                              UserMemory.created_at.desc()).limit(limit)
        result = await session.exec(stmt)
        items = list(result.all())
        return [i.model_dump() for i in items]


async def memory_forget_user(memory_id: str) -> dict:
    async with session_scope() as session:
        item = await session.get(UserMemory, memory_id)
        if not item:
            return {"removed": 0}
        await session.delete(item)
        await session.commit()
        return {"removed": 1, "id": memory_id}


async def memory_record_user_hit(memory_id: str) -> dict:
    """Mark a user memory as recently used; increments hits.
    Called by the orchestrator when a memory is included in the system prompt."""
    async with session_scope() as session:
        item = await session.get(UserMemory, memory_id)
        if not item:
            return {"ok": False}
        item.hits += 1
        item.last_used_at = datetime.utcnow()
        session.add(item)
        await session.commit()
        return {"ok": True, "hits": item.hits}
