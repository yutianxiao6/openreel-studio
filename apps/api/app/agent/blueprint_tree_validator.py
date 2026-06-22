"""Validation checks for semantic blueprint trees."""
from __future__ import annotations

import re
from typing import Any

from app.agent.blueprint_tree_normalizer import (
    _VIDEO_ASPECT_RATIOS,
    _aspect_ratio_conflict,
    _has_explicit_text_to_video_path,
    _is_segment_node,
    _node_materializes,
    _node_text,
    _normalize_prompt_evidence_for_nodes,
    _normalize_references,
    _prompt_evidence_error,
    _required_video_guide_topics,
    _walk_nodes,
    _walk_nodes_with_parent,
)


_VIDEO_OUTPUT_MARKERS = (
    "视频",
    "短片",
    "成片",
    "出片",
    "生产视频",
    "生成视频",
    "video",
    "clip",
)
_VIDEO_PREPRODUCTION_ONLY_MARKERS = (
    "不要生成视频",
    "不生成视频",
    "不要创建视频",
    "不要运行视频",
    "只做分镜",
    "只出分镜",
    "只做图片",
    "只生成图片",
    "视觉预制作",
    "preproduction only",
)
_STORYBOARD_MARKERS = ("分镜", "宫格", "storyboard", "grid")
_SHOT_IMAGE_MARKERS = ("单张分镜", "首帧", "尾帧", "keyframe", "first frame", "last frame")
_STORY_TEMPLATE_MARKERS = ("故事模板", "story_template", "story template")
_TEXT_TO_VIDEO_MARKERS = ("文生视频", "文本生成视频", "text-to-video", "text_to_video", "t2v")
_INITIAL_BLUEPRINT_FACT_FIELDS = ("episode_count", "segment_seconds", "production_basis")


def _state_request_text(state: dict[str, Any]) -> str:
    pending = state.get("pending_video_blueprint_request")
    if not isinstance(pending, dict):
        return ""
    collected = pending.get("collected_facts") if isinstance(pending.get("collected_facts"), dict) else {}
    parts: list[str] = []
    for key in (
        "raw_request",
        "basic_answer",
        "structure_answer",
        "source_request",
        "summary",
    ):
        value = pending.get(key)
        if value not in (None, "", [], {}):
            parts.append(str(value))
    for key in (
        "topic",
        "production_basis",
        "generation_basis",
        "video_basis",
        "reference_basis",
        "aspect_ratio",
        "segment_seconds",
    ):
        value = collected.get(key)
        if value not in (None, "", [], {}):
            parts.append(str(value))
    return "\n".join(parts)


def _video_output_requested(*, source_request: str = "", summary: str = "", state: dict[str, Any] | None = None) -> bool:
    text = "\n".join(
        item for item in (
            str(source_request or ""),
            str(summary or ""),
            _state_request_text(state or {}),
        )
        if item
    ).lower()
    if not text:
        return False
    if any(marker.lower() in text for marker in _VIDEO_PREPRODUCTION_ONLY_MARKERS):
        return False
    return any(marker.lower() in text for marker in _VIDEO_OUTPUT_MARKERS)


def _loaded_full_guide_topics(state: dict[str, Any]) -> set[str]:
    cache = state.get("_mentor_guides_loaded")
    if not isinstance(cache, dict):
        return set()
    topics: set[str] = set()
    for topic, payload in cache.items():
        if not isinstance(payload, dict):
            continue
        if str(payload.get("detail") or "").strip().lower() == "full":
            topics.add(str(payload.get("topic") or topic).strip().lower())
    return {topic for topic in topics if topic}


def _positive_int_field(value: Any) -> int | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    parsed = int(digits)
    return parsed if parsed > 0 else None


def _normalize_production_basis(value: Any) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if not text:
        return ""
    if any(marker in lowered for marker in ("text_to_video", "text-to-video", "t2v", "文生", "文本生成视频")):
        return "text_to_video"
    if any(marker in lowered for marker in ("image_to_video", "image-to-video", "i2v", "图生", "参考图", "分镜图", "首帧", "尾帧")):
        return "image_to_video"
    if any(marker in lowered for marker in ("model_decide", "model decide", "模型判断", "模型决定", "模型规划", "模型发挥", "由模型")):
        return "model_decide"
    return text


def _production_path_for_video(node: dict[str, Any]) -> str:
    fields = node.get("fields") if isinstance(node.get("fields"), dict) else {}
    return str(
        node.get("production_path")
        or fields.get("production_path")
        or fields.get("method")
        or fields.get("mode")
        or ""
    ).strip().lower()


def _normalized_ref_ids(node: dict[str, Any]) -> set[str]:
    fields = node.get("fields") if isinstance(node.get("fields"), dict) else {}
    refs = _normalize_references(node.get("references") or fields.get("references"))
    deps = _normalize_references(node.get("depends_on") or fields.get("depends_on"))
    return {str(item).lstrip("@") for item in [*refs, *deps] if item}


def _production_basis_mismatch_error(
    *,
    production_basis: str,
    video_nodes: list[dict[str, Any]],
    image_nodes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not video_nodes or production_basis not in {"text_to_video", "image_to_video"}:
        return None
    image_ids = {str(node.get("id") or "").lstrip("@") for node in image_nodes if node.get("id")}
    mismatches: list[dict[str, str]] = []
    for node in video_nodes:
        node_id = str(node.get("id") or node.get("title") or "video")
        path = _production_path_for_video(node)
        if production_basis == "text_to_video":
            if path not in {"text_to_video", "t2v", "direct_text_to_video", "direct_t2v"}:
                mismatches.append({"node_id": node_id, "expected": "text_to_video", "actual": path or "missing"})
        elif production_basis == "image_to_video":
            refs = _normalized_ref_ids(node)
            has_image_dependency = bool(image_ids & refs)
            if path != "image_to_video" or not has_image_dependency:
                mismatches.append({
                    "node_id": node_id,
                    "expected": "image_to_video_with_image_reference",
                    "actual": path or "missing",
                })
    if not mismatches:
        return None
    return {
        "ok": False,
        "error": "视频节点制作路径与蓝图 production_basis 不一致。",
        "error_kind": "production_basis_mismatch",
        "production_basis": production_basis,
        "mismatches": mismatches,
        "hint": (
            "按 blueprint.fields.production_basis 修正 video.fields.production_path；"
            "图生视频还必须让 video 的 references/depends_on 指向上游 image 节点。"
        ),
    }


def _request_specific_video_topics(text: str) -> set[str]:
    lowered = text.lower()
    topics: set[str] = set()
    if any(marker.lower() in lowered for marker in _TEXT_TO_VIDEO_MARKERS):
        topics.add("video_workflow_t2v")
    if any(marker.lower() in lowered for marker in _STORY_TEMPLATE_MARKERS):
        topics.add("video_workflow_story_template")
    if any(marker.lower() in lowered for marker in _SHOT_IMAGE_MARKERS):
        topics.add("video_workflow_shot_images")
    if any(marker.lower() in lowered for marker in _STORYBOARD_MARKERS):
        topics.add("video_workflow_storyboard")
    return topics


def _video_output_readiness_error(
    state: dict[str, Any],
    children: list[dict[str, Any]],
    *,
    source_request: str = "",
    summary: str = "",
    blueprint_fields: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    nodes = _walk_nodes(children)
    video_nodes = [node for node in nodes if node.get("type") == "video" and _node_materializes(node)]
    blueprint_fields = blueprint_fields if isinstance(blueprint_fields, dict) else {}
    request_text = "\n".join(
        item for item in (
            str(source_request or ""),
            str(summary or ""),
            _state_request_text(state),
            "\n".join(_node_text(node) for node in nodes),
        )
        if item
    )
    video_requested = _video_output_requested(source_request=source_request, summary=summary, state=state)
    if not video_requested and not video_nodes:
        return None

    missing_initial: list[str] = []
    if _positive_int_field(blueprint_fields.get("episode_count")) is None:
        missing_initial.append("episode_count")
    if _positive_int_field(blueprint_fields.get("segment_seconds")) is None:
        missing_initial.append("segment_seconds")
    production_basis = _normalize_production_basis(blueprint_fields.get("production_basis"))
    if not production_basis:
        missing_initial.append("production_basis")
    if missing_initial:
        return {
            "ok": False,
            "error": "蓝图缺少首要制作事实，无法审核建树是否符合用户确认的规模和制作方法。",
            "error_kind": "missing_initial_blueprint_fields",
            "missing_fields": missing_initial,
            "required_fields": list(_INITIAL_BLUEPRINT_FACT_FIELDS),
            "hint": (
                "在 blueprint.fields 写 episode_count、segment_seconds、production_basis。"
                "能从时长推断的规模事实直接填；15秒及以内默认 1 集单段。"
                "只有无法从用户消息、collected_facts 或项目状态推断时才补问。"
            ),
        }

    image_nodes = [node for node in nodes if node.get("type") == "image" and _node_materializes(node)]
    mismatch_error = _production_basis_mismatch_error(
        production_basis=production_basis,
        video_nodes=video_nodes,
        image_nodes=image_nodes,
    )
    if mismatch_error:
        return mismatch_error

    required_topics = {"video_workflow", "blueprint_tree_guide"}
    required_topics.update(_required_video_guide_topics(nodes))
    specific_topics = _request_specific_video_topics(request_text)
    if production_basis == "text_to_video":
        specific_topics.add("video_workflow_t2v")
    elif production_basis == "image_to_video" and not specific_topics.intersection(
        {"video_workflow_storyboard", "video_workflow_shot_images", "video_workflow_story_template"}
    ):
        specific_topics.add("video_workflow_storyboard")
    required_topics.update(specific_topics)
    loaded_topics = _loaded_full_guide_topics(state)
    missing_topics = sorted(topic for topic in required_topics if topic not in loaded_topics)
    if missing_topics:
        topic_calls = [
            {"name": "skill.project_mentor", "input": {"topic": topic, "detail": "full"}}
            for topic in missing_topics
        ]
        tool_flow = [
            {"name": "tool.search", "input": {"query": topic, "category": "guide"}}
            for topic in missing_topics
        ]
        tool_flow.append({"name": "tool.describe", "input": {"names": ["skill.project_mentor"]}})
        tool_flow.extend(
            {"name": "tool.execute", "input": {"name": "skill.project_mentor", "input": {"topic": topic, "detail": "full"}}}
            for topic in missing_topics
        )
        return {
            "ok": False,
            "error": "视频蓝图提交前缺少完整制作指南读取记录。",
            "error_kind": "guide_not_loaded",
            "missing_guide_topics": missing_topics,
            "required_tool_calls": topic_calls,
            "required_tool_flow": tool_flow,
            "hint": (
                "先用 tool.search(category='guide') 找到 skill.project_mentor，"
                "再用 tool.execute 逐个读取 missing_guide_topics 的 detail='full'。"
                "读完后根据指南修订树，再调用 blueprint.finalize_tree_draft。"
            ),
        }

    if video_requested and not video_nodes:
        return {
            "ok": False,
            "error": "用户请求的是视频输出，但当前蓝图只有 text/image 节点，没有 video 目标节点。",
            "error_kind": "missing_video_node_for_video_request",
            "hint": (
                "保留分镜/参考图 image 节点，同时添加一个 video 节点作为最终成片目标；"
                "video.fields.production_path 写 text_to_video 或 image_to_video，"
                "references/depends_on 指向上游设定、分镜图或参考图。确认前不会运行视频。"
            ),
            "fix_example": (
                "blueprint.append_tree_node(parent_id='segment_01', node={"
                "'id':'segment_01_video','type':'video','title':'15秒成片',"
                "'fields':{'production_path':'image_to_video','duration_seconds':15,'aspect_ratio':'16:9'},"
                "'references':['@brief','@segment_01_storyboard'],"
                "'depends_on':['@segment_01_storyboard']})"
            ),
        }
    return None


def _runtime_evidence_error(
    state: dict[str, Any],
    children: list[dict[str, Any]],
    *,
    current_tree_version: Any = None,
) -> dict[str, Any] | None:
    nodes = _walk_nodes(children)
    video_nodes = [node for node in nodes if node.get("type") == "video" and _node_materializes(node)]
    if video_nodes:
        review = state.get("_last_agent_review")
        review_status = str((review or {}).get("status") or "").strip().lower() if isinstance(review, dict) else ""
        review_passed = (
            isinstance(review, dict)
            and ((review or {}).get("passed") is True or review_status in {"pass", "passed", "ok"})
        )
        review_subject = review.get("review_subject") if isinstance(review, dict) and isinstance(review.get("review_subject"), dict) else {}
        reviewed_tree_version = review_subject.get("tree_version")
        if not isinstance(review, dict):
            return {
                "ok": True,
                "status": "agent_review_required",
                "finalized": False,
                "needs_review": True,
                "message": "视频蓝图提交前需要一次隔离只读 review；本次未提交待确认蓝图。",
                "hint": (
                    "调用 agent.review，传 review_goal、user_request、work_summary、evidence/focus。"
                    "review 只按用户需求、蓝图事实、依赖关系和执行条件找有证据的问题；"
                    "不要让偏好型建议改写正确方案。"
                ),
            }
        review_parse_status = str(review.get("parse_status") or "").strip().lower()
        review_session_status = str(review.get("session_status") or "").strip().lower()
        review_timed_out = bool(review.get("timed_out"))
        if (
            review_status in {"", "unknown"}
            or review_timed_out
            or (review_session_status and review_session_status != "completed")
            or (review_parse_status and review_parse_status not in {"parsed", "repaired"})
        ):
            return {
                "ok": True,
                "status": "agent_review_required",
                "finalized": False,
                "needs_review": True,
                "message": "最近一次隔离 review 未产出可信结构化结论；本次未提交待确认蓝图。",
                "review_status": review.get("status"),
                "review_parse_status": review.get("parse_status"),
                "review_session_status": review.get("session_status"),
                "review_timed_out": review.get("timed_out"),
                "hint": "重新调用 agent.review。若上次 parse/session/timeout 失败，减少 evidence 范围或补齐必要证据后重试。",
            }
        if current_tree_version not in (None, "") and reviewed_tree_version not in (None, ""):
            if str(reviewed_tree_version) != str(current_tree_version):
                return {
                    "ok": True,
                    "status": "agent_review_required",
                    "finalized": False,
                    "needs_review": True,
                    "message": "草稿已在上次 review 后变化，需要重新做一次隔离只读 review。",
                    "reviewed_tree_version": reviewed_tree_version,
                    "current_tree_version": current_tree_version,
                    "hint": "对当前草稿重新调用 agent.review；只根据有证据的违反项决定是否修改。",
                }
        grounded_findings = int(review.get("grounded_findings_count") or 0)
        blocking_findings = int(review.get("blocking_findings_count") or 0)
        should_block = (
            review_status in {"blocked", "failed"}
            or blocking_findings > 0
            or (review.get("safe_to_submit") is False and grounded_findings > 0 and not review_passed)
        )
        if should_block:
            return {
                "ok": True,
                "status": "agent_review_revise_required",
                "finalized": False,
                "needs_revision": True,
                "message": "最近一次隔离检查要求修改草稿；本次未提交待确认蓝图。",
                "review_status": review.get("status"),
                "grounded_findings_count": grounded_findings,
                "blocking_findings_count": blocking_findings,
                "hint": "只修正 review 中有具体 evidence/violated_requirement 的问题；偏好型 low severity 建议不应带偏正确方案。修正后重新 review，再 finalize。",
            }
    return _prompt_evidence_error(children)


def _semantic_quality_error(children: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Reject trees that are valid JSON but too implicit to execute reliably."""
    nodes = _walk_nodes(children)
    nodes_with_parent = _walk_nodes_with_parent(children)
    image_nodes = [node for node in nodes if node.get("type") == "image" and _node_materializes(node)]
    video_nodes = [node for node in nodes if node.get("type") == "video" and _node_materializes(node)]
    if not video_nodes:
        return None

    parent_by_id = {
        str(node.get("id") or ""): parent_id
        for node, parent_id in nodes_with_parent
        if node.get("id")
    }
    if image_nodes:
        root_video_nodes = [
            node for node in video_nodes
            if parent_by_id.get(str(node.get("id") or "")) == "root"
        ]
        if root_video_nodes:
            segment_candidates = [node for node in nodes if _is_segment_node(node)]
            target_segment_id = str((segment_candidates[0] if segment_candidates else {}).get("id") or "segment_01")
            root_segment_media = [
                node for node in image_nodes
                if parent_by_id.get(str(node.get("id") or "")) == "root"
                and re.search(
                    r"scene|场景|storyboard|分镜|宫格|keyframe|首帧|尾帧|story_template|故事模板",
                    f"{node.get('id') or ''}\n{node.get('title') or ''}",
                    re.IGNORECASE,
                )
            ]
            suggested_moves = [
                {"node_id": node.get("id"), "parent_id": target_segment_id}
                for node in [*root_segment_media, *root_video_nodes]
                if node.get("id")
            ]
            return {
                "ok": False,
                "error": (
                    "蓝图树把 video 节点直接挂在 root 下，但当前项目包含 image 视觉准备节点。"
                    "请用 text 节点建立 episode/segment 层级，把 storyboard/keyframe/story-template image 和 video 作为同一 segment 的子节点。"
                ),
                "error_kind": "flat_video_tree_requires_segment_parent",
                "hint": (
                    "推荐结构：root -> brief；root -> assets -> character image；"
                    "root -> episode_01(optional) -> segment_01 -> scene image -> storyboard image -> video。"
                    "跨分支依赖继续写 references/depends_on。"
                ),
                "suggested_moves": suggested_moves,
                "fix_example": f"blueprint.update_tree_node(node_id='video_final', patch={{'parent_id':'{target_segment_id}'}})",
            }

    for node in video_nodes:
        fields = node.get("fields") if isinstance(node.get("fields"), dict) else {}
        ratio = str(node.get("aspect_ratio") or fields.get("aspect_ratio") or "").strip()
        if ratio and ratio not in _VIDEO_ASPECT_RATIOS:
            return {
                "ok": False,
                "error": f"视频节点 {node.get('title') or node.get('id')} 的画幅 {ratio!r} 不受支持；视频节点只能使用 16:9 或 9:16。",
                "error_kind": "unsupported_video_aspect_ratio",
                "supported_aspect_ratios": sorted(_VIDEO_ASPECT_RATIOS),
            }
        refs = _normalize_references(node.get("references") or fields.get("references"))
        deps = _normalize_references(node.get("depends_on") or fields.get("depends_on"))
        if not refs and not deps:
            return {
                "ok": False,
                "error": f"视频节点 {node.get('title') or node.get('id')} 缺少 references 或 depends_on，无法表达它依赖哪些设定、图片或分镜。",
                "error_kind": "implicit_video_dependency",
                "hint": "视频节点应引用上游 text/image 节点；如果是直接文生视频，也要引用故事设定或分镜文本节点。",
            }

    if not image_nodes:
        for node in video_nodes:
            if _has_explicit_text_to_video_path(node, nodes):
                return None
        return {
            "ok": False,
            "error": "蓝图树包含视频节点但没有图片节点，也没有明确声明直接文生视频制作路径。",
            "error_kind": "implicit_video_production_path",
            "hint": "如果选择图生视频/参考图路径，请添加 image 视觉准备节点并让 video 引用它；如果选择文生视频，请在 video.fields.production_path 写 text_to_video 并提供完整视频 prompt 或写作计划。",
        }
    return None


async def validate_children_for_review(
    project_id: str,
    children: list[dict[str, Any]],
    *,
    get_project_state,
    get_pending_aspect_ratio,
    require_runtime_evidence: bool = False,
    current_tree_version: Any = None,
    source_request: str = "",
    summary: str = "",
    blueprint_fields: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    state: dict[str, Any] = {}
    if require_runtime_evidence:
        state = await get_project_state(project_id)
    _normalize_prompt_evidence_for_nodes(children, state=state, default_missing=True)

    expected_aspect_ratio = await get_pending_aspect_ratio(project_id)
    conflict = _aspect_ratio_conflict(expected_aspect_ratio, children)
    if conflict:
        return {
            "ok": False,
            "error": (
                f"蓝图树与用户画幅要求冲突：用户选择 {expected_aspect_ratio}，"
                f"但树内容包含 {conflict}。请按用户画幅修订语义树。"
            ),
            "error_kind": "aspect_ratio_conflict",
            "expected_aspect_ratio": expected_aspect_ratio,
            "conflicting_value": conflict,
        }
    semantic_error = _semantic_quality_error(children)
    if semantic_error:
        return semantic_error
    if require_runtime_evidence:
        readiness_error = _video_output_readiness_error(
            state,
            children,
            source_request=source_request,
            summary=summary,
            blueprint_fields=blueprint_fields,
        )
        if readiness_error:
            return readiness_error
        return _runtime_evidence_error(state, children, current_tree_version=current_tree_version)
    return None
