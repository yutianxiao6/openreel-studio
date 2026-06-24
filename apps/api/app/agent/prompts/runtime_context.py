"""工厂 section:运行时上下文(项目状态 / 模型映射 / 长期记忆 / 项目快照 / 会话焦点)。"""
from __future__ import annotations

import json

NAME = "runtime_context"
TRIGGER = "factory"
ORDER = 900  # 永远在末尾


def _short_text(value: object, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _short_list(values: object, limit: int = 4) -> list[object]:
    if not isinstance(values, list):
        return []
    return values[:limit]


def _compact_ref(payload: dict, keys: list[str]) -> dict:
    out: dict[str, object] = {}
    for key in keys:
        value = payload.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, str):
            out[key] = _short_text(value, 240)
        elif isinstance(value, list):
            out[key] = value[:8]
            if len(value) > 8:
                out[f"{key}_total"] = len(value)
        else:
            out[key] = value
    return out


def _metadata_summary(metadata: object) -> dict:
    if not isinstance(metadata, dict):
        return {}
    title = _short_text(metadata.get("title"), 120)
    return {"title": title} if title else {}


def _progress_summary(progress: dict) -> dict:
    summary = _compact_ref(
        progress,
        [
            "status",
            "review_mode",
            "current_section",
            "next_section_index",
            "current_window_index",
            "next_window_index",
            "failed_section_index",
            "failed_window_index",
        ],
    )
    sections = progress.get("sections")
    if isinstance(sections, list):
        by_status: dict[str, int] = {}
        for section in sections:
            if not isinstance(section, dict):
                continue
            status = str(section.get("status") or "unknown")
            by_status[status] = by_status.get(status, 0) + 1
        summary["sections_total"] = len(sections)
        if by_status:
            summary["sections_by_status"] = by_status
    windows = progress.get("windows")
    if isinstance(windows, list):
        summary["windows_total"] = len(windows)
    if progress.get("failure_reason"):
        summary["has_failure_reason"] = True
    return summary


def _pending_video_request_summary(pending_request: dict) -> dict:
    summary: dict[str, object] = {
        "stage": pending_request.get("stage"),
        "last_submitted_stage": pending_request.get("last_submitted_stage"),
        "duration_seconds": pending_request.get("duration_seconds"),
        "has_raw_request": bool(pending_request.get("raw_request")),
        "has_basic_answer": bool(pending_request.get("basic_answer")),
        "has_structure_answer": bool(pending_request.get("structure_answer")),
    }
    method_hint = _legacy_video_method_hint(pending_request.get("selected_mode"))
    if method_hint:
        summary["method_hint"] = method_hint
    facts = pending_request.get("collected_facts")
    if isinstance(facts, dict) and facts:
        summary["collected_facts"] = _compact_ref(
            facts,
            [
                "topic",
                "style",
                "video_type",
                "duration_seconds",
                "aspect_ratio",
                "scene",
                "character",
                "plot_outline",
                "episode_count",
                "segment_seconds",
                "production_basis",
            ],
        )
        summary["fact_policy"] = "collected_facts 是用户已确认事实，后续直接使用这些值并围绕缺失信息提问。"
        if any(str(value or "") == "model_decide" for value in facts.values()):
            summary["delegation_policy"] = (
                "model_decide 表示用户授权模型选择；创建节点前，"
                "duration/aspect_ratio/production_basis 等字段要落成具体可执行值，并在剧本/规划 text 节点写清模型假设。"
            )
    for answer_key in ("basic_answers", "structure_answers"):
        answers = pending_request.get(answer_key)
        if not isinstance(answers, list) or not answers:
            continue
        compact_answers: list[dict[str, object]] = []
        for item in answers[:8]:
            if not isinstance(item, dict):
                continue
            compact_item = _compact_ref(item, ["id", "label", "value"])
            if compact_item:
                compact_answers.append(compact_item)
        if compact_answers:
            summary[answer_key] = compact_answers
            if len(answers) > 8:
                summary[f"{answer_key}_total"] = len(answers)
    references = pending_request.get("reference_images")
    if isinstance(references, list) and references:
        summary["reference_images"] = [
            _compact_ref(ref, ["mention", "rel_path", "filename"])
            for ref in references[:6]
            if isinstance(ref, dict)
        ]
        if len(references) > 6:
            summary["reference_images_total"] = len(references)
    return {key: value for key, value in summary.items() if value not in (None, "", [], {})}


def _legacy_video_method_hint(selected_mode: object) -> str:
    """Expose old intake mode values only as a readable hint, not a routing key."""
    mode = str(selected_mode or "").strip().lower()
    if mode in {"grid", "story_template", "story-template", "template"}:
        return "故事模板图"
    if mode in {"storyboard", "board", "grid_storyboard"}:
        return "宫格分镜"
    if mode in {"first_last_frame", "first-last-frame", "keyframes"}:
        return "首尾帧"
    if mode in {"text_to_video", "t2v", "direct_video"}:
        return "文生视频"
    return ""


def _project_snapshot(state: dict) -> str:
    """压缩版项目快照:已存在哪些人物/集/场景/段。让 Agent 不要重复生成。"""
    lines: list[str] = []

    chars = state.get("characters") or []
    if chars:
        items = []
        for c in chars[:30]:
            name = c.get("name", "?")
            role = c.get("role_type") or c.get("tier") or ""
            items.append(f"{name}({role})" if role else name)
        more = f" 等共 {len(chars)} 个" if len(chars) > 30 else ""
        lines.append(f"- 人物:{', '.join(items)}{more}")

    episodes = state.get("episodes") or {}
    if episodes:
        # episodes 形如 {"1": {...}, "2": {...}}
        items = []
        for k in sorted(episodes.keys(), key=lambda x: int(x) if str(x).isdigit() else 0)[:20]:
            ep = episodes[k] or {}
            title = ep.get("title") or ep.get("summary", "")[:30]
            items.append(f"第{k}集{('・' + title) if title else ''}")
        more = f" 等共 {len(episodes)} 集" if len(episodes) > 20 else ""
        lines.append(f"- 已完成剧本:{', '.join(items)}{more}")

    scenes = state.get("scenes") or []
    if scenes:
        items = []
        for s in scenes[:20]:
            name = s.get("name") or s.get("location", "?")
            items.append(name)
        more = f" 等共 {len(scenes)} 个" if len(scenes) > 20 else ""
        lines.append(f"- 场景:{', '.join(items)}{more}")

    segments = state.get("segments") or {}
    if segments:
        total = sum(len(v) if isinstance(v, list) else 0 for v in segments.values())
        lines.append(f"- 段落:已切 {len(segments)} 集 共 {total} 段")

    return "\n".join(lines) if lines else ""


def _session_focus(state: dict) -> str:
    sess = state.get("session") or {}
    if not sess:
        return ""
    parts = []
    if (e := sess.get("working_episode")) is not None:
        parts.append(f"当前集:第{e}集")
    if (s := sess.get("working_segment")) is not None:
        parts.append(f"当前段:第{s}段")
    if step := sess.get("last_step"):
        parts.append(f"上次完成:{step}")
    if node := sess.get("last_node_id"):
        parts.append(f"上一节点:{node[:12]}…")
    return " / ".join(parts) if parts else ""


def _pending_control_block(state: dict) -> str:
    pending_reset = state.get("_pending_reset_confirm")
    if isinstance(pending_reset, dict) and pending_reset.get("scope") == "full":
        return (
            "待确认控制操作:"
            + json.dumps(
                {
                    "action": "project_reset",
                    "scope": "full",
                    "reason": pending_reset.get("reason"),
                    "status": "awaiting_model_decision_from_latest_user_message",
                },
                ensure_ascii=False,
            )
        )
    pending_tool = state.get("_pending_tool_confirm")
    if isinstance(pending_tool, dict) and pending_tool.get("target"):
        return (
            "待确认控制操作:"
            + json.dumps(
                {
                    "action": "tool_confirmation",
                    "target": pending_tool.get("target"),
                    "risk": pending_tool.get("risk"),
                    "reason": pending_tool.get("reason"),
                    "source_user_message": str(pending_tool.get("source_user_message") or "")[:400],
                    "status": "awaiting_structured_confirmation_or_model_decision",
                },
                ensure_ascii=False,
            )
        )
    return ""


def _format_node_counts(counts: dict, *, limit: int = 15) -> str:
    if not counts:
        return ""
    return ", ".join(
        f"{k}={v}"
        for k, v in sorted(counts.items(), key=lambda x: (-int(x[1] or 0), str(x[0])))[:limit]
    )


def _node_type_counts(by_type: dict) -> str:
    type_zh = {
        "text": "文本",
        "image": "图片",
        "video": "视频",
    }
    type_parts = [
        f"{type_zh.get(k, k)}×{v}"
        for k, v in sorted(by_type.items(), key=lambda x: (-int(x[1] or 0), str(x[0])))[:15]
    ]
    return ", ".join(type_parts)


def _node_surface_line(label: str, data: dict) -> str:
    total = int(data.get("total") or 0)
    if total == 0:
        return f"- {label}:0 个"
    type_str = _node_type_counts(data.get("by_type") or {})
    status_str = _format_node_counts(data.get("by_status") or {})
    return (
        f"- {label}:共 {total} 个"
        + (f" — {type_str}" if type_str else "")
        + (f" | 状态:{status_str}" if status_str else "")
    )


def _canvas_block(state: dict) -> str:
    """项目节点真实统计(DB 是真相源)。"""
    cs = state.get("_canvas_summary")
    if cs is None:
        return ""  # 没传 canvas_summary,不输出
    total = cs.get("total", 0)
    if total == 0:
        return "项目节点(DB 真实):0 个"

    node_refs = cs.get("node_refs") if isinstance(cs.get("node_refs"), list) else []
    by_type = cs.get("by_type") or {}
    by_status = cs.get("by_status") or {}
    if not by_type and isinstance(cs.get("surface_details"), dict):
        by_type = {}
        by_status = {}
        for data in cs["surface_details"].values():
            if not isinstance(data, dict):
                continue
            for key, value in (data.get("by_type") or {}).items():
                by_type[key] = int(by_type.get(key) or 0) + int(value or 0)
            for key, value in (data.get("by_status") or {}).items():
                by_status[key] = int(by_status.get(key) or 0) + int(value or 0)

    type_str = _node_type_counts(by_type)
    status_str = _format_node_counts(by_status)
    lines = [
        f"项目节点(DB 真实):共 {total} 个 — {type_str}"
        + (f" | 状态:{status_str}" if status_str else "")
    ]
    if node_refs:
        refs: list[dict[str, object]] = []
        for item in node_refs[:8]:
            if not isinstance(item, dict):
                continue
            ref: dict[str, object] = {}
            for key in ("id", "type", "status"):
                value = item.get(key)
                if value not in (None, "", [], {}):
                    ref[key] = value
            title = _short_text(item.get("title"), 64)
            if title:
                ref["title"] = title
            if ref:
                refs.append(ref)
        payload: dict[str, object] = {
            "available_count": len(node_refs),
            "items": refs,
            "policy": "编号目标直接 node.get(node_id)，模糊目标用 node.list(query|regex)。",
        }
        if len(node_refs) > len(refs):
            payload["omitted_count"] = len(node_refs) - len(refs)
        lines.append("节点定位索引:" + json.dumps(payload, ensure_ascii=False))
    lines.append("读取规则:用户和 Agent 共用同一画布；编号目标直接 node.get；空/草稿节点可补全。")
    return "\n".join(lines)


def _memory_index_block(title: str, facts: list[dict]) -> str:
    """Return memory references only; never inject durable memory body text."""
    if not facts:
        return ""
    refs: list[dict[str, object]] = []
    for idx, fact in enumerate(facts[:12], start=1):
        if not isinstance(fact, dict):
            continue
        refs.append({
            "index": idx,
            "id": fact.get("id"),
            "kind": fact.get("kind"),
            "pinned": bool(fact.get("pinned")),
            "source_project_id": fact.get("source_project_id"),
            "created_at": fact.get("created_at"),
        })
    if not refs:
        return ""
    payload = {
        "title": title,
        "available_count": len(facts),
        "refs": refs,
        "body_policy": "memory bodies are not auto-injected; use current state first and ask the user when a memory body is required but no memory tool is visible",
    }
    return json.dumps(payload, ensure_ascii=False)


def build(
    state: dict,
    model_configs: list[dict] | None = None,
    user_facts: list[dict] | None = None,
    project_facts: list[dict] | None = None,
    latest_user_message: str = "",
    **_: object,
) -> str:
    metadata = state.get("metadata", {})
    parts = [
        "## 运行时上下文",
        f"项目标题:{json.dumps(_metadata_summary(metadata).get('title') or '未命名项目', ensure_ascii=False)}",
    ]

    loaded_skills = state.get("_skills_loaded")
    if isinstance(loaded_skills, dict) and loaded_skills:
        cached_skills = []
        for skill, payload in sorted(loaded_skills.items()):
            if not isinstance(payload, dict):
                continue
            cached_skills.append({
                "skill": skill,
                "tool": payload.get("tool"),
                "detail": payload.get("detail"),
                "guidance_hash": payload.get("guidance_hash"),
                "guidance_chars": payload.get("guidance_chars"),
            })
        if cached_skills:
            skill_cache_limit = 8
            skill_payload: dict[str, object] = {
                "available_count": len(cached_skills),
                "items": cached_skills[:skill_cache_limit],
                "reuse_policy": "仅提示已读 skill；需要流程细节时重新调用对应 skill。",
            }
            if len(cached_skills) > skill_cache_limit:
                skill_payload["omitted_count"] = len(cached_skills) - skill_cache_limit
            parts.append(
                "\n### Skill 复用提醒\n"
                + json.dumps(skill_payload, ensure_ascii=False)
            )

    return "\n".join(parts)
