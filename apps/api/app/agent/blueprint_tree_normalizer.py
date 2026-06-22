"""Pure normalization helpers for semantic blueprint trees."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any


_SEMANTIC_NODE_TYPES = {"text", "image", "video", "audio"}
_VIDEO_ASPECT_RATIOS = {"16:9", "9:16"}
_MEDIA_PROMPT_NODE_TYPES = {"image", "video"}
_PROMPT_EVIDENCE_FIELDS = {"prompt_source"}
_MAX_APPEND_NODES = 12
_ALLOWED_PROMPT_SOURCES = {
    "skill",
    "skill_or_model_written",
    "model_written",
    "model_written_after_skill",
    "model_written_after_prompt_guide",
    "freeform_after_prompt_guide",
}
_SEGMENT_HINT_RE = re.compile(
    r"segment|(?:^|[\s_\-/])seg(?:ment)?[_\-\s]?\d+\b|"
    r"ep(?:isode)?\s*\d+\s*[_\-\s]?\s*seg(?:ment)?\s*\d+\b|"
    r"分段|单段|第\s*\d+\s*段|15\s*秒|\b\d+\s*[-–—]\s*\d+\s*s\b",
    re.IGNORECASE,
)
_DRAFT_PATCH_FIELD_ALIASES_TO_FIELDS = {
    "action_beats",
    "camera",
    "character_consistency",
    "color_palette",
    "composition",
    "design_notes",
    "duration_seconds",
    "fps",
    "image_role",
    "lighting",
    "mood",
    "motion",
    "prompt_notes",
    "prompt_style",
    "purpose",
    "reference_notes",
    "role",
    "shot_beats",
    "source_image_ref",
    "source_image_refs",
    "source_node_id",
    "storyboard_cells",
    "style_tags",
    "template_key",
    "visual_style",
}
_VALID_TEXT_FIELDS = {"id", "type", "title", "content"}
_VALID_IMAGE_FIELDS = {"id", "type", "title", "description", "resolution", "quality", "references", "depends_on"}
_VALID_VIDEO_FIELDS = {
    "id",
    "type",
    "title",
    "description",
    "duration",
    "resolution",
    "aspect_ratio",
    "production_path",
    "prompt",
    "references",
    "depends_on",
}
_VALID_AUDIO_FIELDS = {
    "id",
    "type",
    "title",
    "description",
    "prompt",
    "format",
    "duration_seconds",
    "references",
    "depends_on",
}


def _is_segment_hint_text(value: Any) -> bool:
    return _SEGMENT_HINT_RE.search(str(value or "")) is not None


def _segment_identity_text(node: dict[str, Any]) -> str:
    return f"{node.get('id') or ''}\n{node.get('title') or ''}\n{node.get('type') or ''}"


def _parse_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def _slug(value: Any, fallback: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_\-\u4e00-\u9fff]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_-")
    return text[:48] or fallback


def _normalize_node_type(raw: Any) -> str:
    node_type = str(raw or "").strip().lower()
    node_type = node_type.replace("-", "_")
    return node_type


def _is_materialized_type(node_type: str) -> bool:
    return node_type in _SEMANTIC_NODE_TYPES


def _normalize_references(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    refs: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in refs:
            refs.append(text)
    return refs


def _normalize_known_reference(ref: str, known_ids: set[str]) -> str:
    text = str(ref or "").strip()
    if not text:
        return ""
    if text.startswith("@"):
        return text
    if text in known_ids:
        return f"@{text}"
    return text


def _normalize_node_links(node: dict[str, Any], known_ids: set[str]) -> None:
    fields = node.get("fields") if isinstance(node.get("fields"), dict) else {}
    if not isinstance(node.get("fields"), dict):
        node["fields"] = fields
    for key in ("references", "depends_on"):
        refs = _normalize_references(node.get(key) or fields.get(key))
        if not refs:
            continue
        normalized: list[str] = []
        for ref in refs:
            value = _normalize_known_reference(ref, known_ids)
            if value and value not in normalized:
                normalized.append(value)
        node[key] = normalized
        fields[key] = normalized
    for child in node.get("children") or []:
        if isinstance(child, dict):
            _normalize_node_links(child, known_ids)


def _normalize_all_links(children: list[dict[str, Any]], known_ids: set[str] | None = None) -> None:
    ids = set(known_ids or set()) | _collect_node_ids(children)
    for child in children:
        if isinstance(child, dict):
            _normalize_node_links(child, ids)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _normalize_semantic_node(
    raw: Any,
    *,
    index_path: str,
    seen_ids: set[str],
    errors: list[str],
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        errors.append(f"{index_path}: node 必须是对象")
        return None

    node_type = _normalize_node_type(raw.get("type") or raw.get("role"))
    if not node_type:
        errors.append(f"{index_path}: 缺少 type")
        return None
    if node_type not in _SEMANTIC_NODE_TYPES:
        errors.append(
            f"{index_path}: 不支持的 type={node_type!r}；允许: "
            + ", ".join(sorted(_SEMANTIC_NODE_TYPES))
        )
        return None

    raw_id = str(raw.get("id") or "").strip()
    fallback_id = f"{node_type}_{len(seen_ids) + 1}"
    node_id = _slug(raw_id or raw.get("title") or fallback_id, fallback_id)
    base_id = node_id
    suffix = 2
    while node_id in seen_ids:
        node_id = f"{base_id}_{suffix}"
        suffix += 1
    seen_ids.add(node_id)

    fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
    node: dict[str, Any] = {
        "id": node_id,
        "type": node_type,
        "title": str(raw.get("title") or fields.get("title") or node_id),
        "status": str(raw.get("status") or "pending"),
        "materialize": _as_bool(raw.get("materialize"), _is_materialized_type(node_type)),
        "fields": dict(fields),
        "children": [],
        "created_at": raw.get("created_at") or datetime.now(timezone.utc).isoformat(),
        "updated_at": raw.get("updated_at") or datetime.now(timezone.utc).isoformat(),
    }

    for key in (
        "content",
        "description",
        "prompt",
        "negative_prompt",
        "resolution",
        "quality",
        "duration",
        "aspect_ratio",
        "production_path",
        "episode_index",
        "segment_index",
        "episode_number",
        "segment_id",
        "shot_id",
        "source_path",
        *_PROMPT_EVIDENCE_FIELDS,
    ):
        if raw.get(key) not in (None, "", [], {}):
            node[key] = raw.get(key)
            node["fields"].setdefault(key, raw.get(key))

    references = _normalize_references(raw.get("references") or fields.get("references"))
    if references:
        node["references"] = references
        node["fields"].setdefault("references", references)
    depends_on = _normalize_references(raw.get("depends_on") or fields.get("depends_on"))
    if depends_on:
        node["depends_on"] = depends_on
        node["fields"].setdefault("depends_on", depends_on)

    _normalize_prompt_evidence_fields(node)

    children = raw.get("children") or []
    if not isinstance(children, list):
        errors.append(f"{index_path}: children 必须是数组")
        children = []
    for idx, child in enumerate(children, 1):
        normalized_child = _normalize_semantic_node(
            child,
            index_path=f"{index_path}.{idx}",
            seen_ids=seen_ids,
            errors=errors,
        )
        if normalized_child is not None:
            node["children"].append(normalized_child)
    return node


def _tree_summary(root: dict[str, Any]) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    materialized_count = 0
    total = 0

    def walk(node: dict[str, Any]) -> None:
        nonlocal materialized_count, total
        total += 1
        node_type = str(node.get("type") or "unknown")
        by_type[node_type] = by_type.get(node_type, 0) + 1
        if node.get("materialize"):
            materialized_count += 1
        for child in node.get("children") or []:
            if isinstance(child, dict):
                walk(child)

    for child in root.get("children") or []:
        if isinstance(child, dict):
            walk(child)
    return {
        "node_count": total,
        "materialized_count": materialized_count,
        "by_type": by_type,
        "top_level": [
            {"id": c.get("id"), "type": c.get("type"), "title": c.get("title")}
            for c in (root.get("children") or [])[:12]
            if isinstance(c, dict)
        ],
    }


def _root_child_order_bucket(node: dict[str, Any]) -> int:
    text = f"{node.get('id') or ''}\n{node.get('title') or ''}\n{node.get('type') or ''}".lower()
    children_text = "\n".join(
        f"{child.get('id') or ''}\n{child.get('title') or ''}\n{child.get('type') or ''}".lower()
        for child in (node.get("children") or [])
        if isinstance(child, dict)
    )
    if re.search(r"brief|story|project_story|项目故事|故事|设定", text):
        return 0
    if re.search(r"assets|asset|视觉资产|参考图集", text) or re.search(r"character|char_ref|人物|角色", text + "\n" + children_text):
        return 1
    if re.search(r"episode|episodes|分集|第\s*\d+\s*集", text):
        return 2
    if _is_segment_hint_text(text):
        return 3
    return 4


def _normalize_root_child_order(root: dict[str, Any]) -> None:
    children = root.get("children")
    if not isinstance(children, list) or len(children) < 2:
        return
    indexed = [
        (index, child)
        for index, child in enumerate(children)
        if isinstance(child, dict)
    ]
    if len(indexed) != len(children):
        return
    indexed.sort(key=lambda item: (_root_child_order_bucket(item[1]), item[0]))
    root["children"] = [child for _, child in indexed]


def _preview_tree_nodes(root: dict[str, Any], *, limit: int = 80) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def walk(node: dict[str, Any], level: int) -> None:
        if len(rows) >= limit:
            return
        fields = node.get("fields") if isinstance(node.get("fields"), dict) else {}
        if node.get("id") != "root":
            rows.append({
                "id": node.get("id"),
                "type": node.get("type"),
                "title": node.get("title"),
                "level": level,
                "content": node.get("content") or fields.get("content"),
                "description": node.get("description") or fields.get("description"),
                "prompt": node.get("prompt") or fields.get("prompt"),
                "resolution": node.get("resolution") or fields.get("resolution"),
                "quality": node.get("quality") or fields.get("quality"),
                "duration": node.get("duration") or fields.get("duration") or fields.get("duration_seconds"),
                "aspect_ratio": node.get("aspect_ratio") or fields.get("aspect_ratio"),
            })
        child_level = level if node.get("id") == "root" else level + 1
        for child in node.get("children") or []:
            if isinstance(child, dict):
                walk(child, child_level)

    walk(root, 0)
    return rows


def _coerce_tree_children(tree: Any, nodes: Any) -> tuple[list[Any], dict[str, Any]]:
    metadata: dict[str, Any] = {}
    tree = _parse_jsonish(tree)
    nodes = _parse_jsonish(nodes)
    if isinstance(tree, dict):
        metadata = {
            key: tree.get(key)
            for key in (
                "title",
                "summary",
                "status",
                "skill",
            )
            if tree.get(key) not in (None, "")
        }
        root = tree.get("root") if isinstance(tree.get("root"), dict) else tree
        children = root.get("children") if isinstance(root, dict) else None
        if isinstance(children, list):
            return children, metadata
    if isinstance(nodes, list):
        return nodes, metadata
    return [], metadata


def _aspect_ratio_conflict(expected: str, children: list[dict[str, Any]]) -> str:
    expected = str(expected or "").strip()
    if expected not in _VIDEO_ASPECT_RATIOS:
        return ""
    text = json.dumps(children, ensure_ascii=False)
    conflict_terms = {
        "16:9": ("9:16", "竖屏"),
        "9:16": ("16:9", "横屏"),
    }[expected]
    for term in conflict_terms:
        if term in text:
            return term
    return ""


def _walk_nodes(children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        nodes.append(node)
        for child in node.get("children") or []:
            if isinstance(child, dict):
                walk(child)

    for child in children:
        if isinstance(child, dict):
            walk(child)
    return nodes


def _walk_nodes_with_parent(children: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    nodes: list[tuple[dict[str, Any], str]] = []

    def walk(node: dict[str, Any], parent_id: str) -> None:
        nodes.append((node, parent_id))
        node_id = str(node.get("id") or "")
        for child in node.get("children") or []:
            if isinstance(child, dict):
                walk(child, node_id or parent_id)

    for child in children:
        if isinstance(child, dict):
            walk(child, "root")
    return nodes


def _collect_node_ids(children: list[dict[str, Any]]) -> set[str]:
    return {
        str(node.get("id"))
        for node in _walk_nodes(children)
        if isinstance(node.get("id"), str) and node.get("id")
    }


def _available_node_refs(root: dict[str, Any], *, limit: int = 24) -> list[dict[str, Any]]:
    children = root.get("children") if isinstance(root.get("children"), list) else []
    refs: list[dict[str, Any]] = []
    for node, parent_id in _walk_nodes_with_parent(children)[:limit]:
        ref = _node_summary(node)
        ref["parent_id"] = parent_id
        refs.append(ref)
    return refs


def _node_text(node: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("id", "type", "title", "content", "description", "prompt"):
        value = node.get(key)
        if value not in (None, "", [], {}):
            parts.append(str(value))
    fields = node.get("fields") if isinstance(node.get("fields"), dict) else {}
    for key in ("production_path", "method", "mode", "duration", "aspect_ratio", "resolution"):
        value = fields.get(key)
        if value not in (None, "", [], {}):
            parts.append(str(value))
    return "\n".join(parts)


def _has_explicit_text_to_video_path(video_node: dict[str, Any], all_nodes: list[dict[str, Any]]) -> bool:
    fields = video_node.get("fields") if isinstance(video_node.get("fields"), dict) else {}
    explicit = str(
        fields.get("production_path")
        or fields.get("method")
        or fields.get("mode")
        or ""
    ).strip().lower()
    if explicit in {"text_to_video", "t2v", "direct_text_to_video", "direct_t2v"}:
        return True
    haystack = "\n".join(_node_text(node) for node in [video_node, *all_nodes]).lower()
    markers = ("文生视频", "文本生成视频", "text_to_video", "text-to-video", "direct t2v", "直接文生")
    return any(marker in haystack for marker in markers)


def _node_fields(node: dict[str, Any]) -> dict[str, Any]:
    fields = node.get("fields")
    return fields if isinstance(fields, dict) else {}


def _normalize_prompt_evidence_fields(
    node: dict[str, Any],
    state: dict[str, Any] | None = None,
    *,
    default_missing: bool = False,
) -> None:
    fields = node.get("fields") if isinstance(node.get("fields"), dict) else {}
    if not isinstance(node.get("fields"), dict):
        node["fields"] = fields
    raw_source = _clean_prompt_source(fields.get("prompt_source") or node.get("prompt_source"))
    raw_source_lc = raw_source.lower()
    if raw_source_lc in _ALLOWED_PROMPT_SOURCES:
        node["prompt_source"] = raw_source_lc
        fields["prompt_source"] = raw_source_lc
    for key in _PROMPT_EVIDENCE_FIELDS:
        if node.get(key) not in (None, "", [], {}):
            fields.setdefault(key, node.get(key))
    prompt_source = str(fields.get("prompt_source") or node.get("prompt_source") or "").strip()
    if not prompt_source and _prompt_text(node):
        if default_missing:
            fields["prompt_source"] = "model_written"
            node["prompt_source"] = "model_written"
    if fields.get("prompt_source") not in (None, "", [], {}):
        node["prompt_source"] = fields.get("prompt_source")


def _normalize_prompt_evidence_for_nodes(
    children: list[dict[str, Any]],
    state: dict[str, Any] | None = None,
    *,
    default_missing: bool = False,
) -> None:
    for node in _walk_nodes(children):
        if node.get("type") in _MEDIA_PROMPT_NODE_TYPES and _node_materializes(node):
            _normalize_prompt_evidence_fields(node, state=state, default_missing=default_missing)


def _clean_prompt_source(value: Any) -> str:
    return str(value or "").strip().strip("`'\"“”‘’ ")


def _prompt_text(node: dict[str, Any]) -> str:
    fields = _node_fields(node)
    return str(node.get("prompt") or fields.get("prompt") or "").strip()


def _field_text(node: dict[str, Any], key: str) -> str:
    fields = _node_fields(node)
    return str(fields.get(key) or node.get(key) or "").strip()


def _node_materializes(node: dict[str, Any]) -> bool:
    node_type = _normalize_node_type(node.get("type"))
    return _as_bool(node.get("materialize"), _is_materialized_type(node_type))


def _prompt_evidence_error(children: list[dict[str, Any]]) -> dict[str, Any] | None:
    missing_nodes: list[dict[str, Any]] = []
    for node in _walk_nodes(children):
        node_type = str(node.get("type") or "")
        if node_type not in _MEDIA_PROMPT_NODE_TYPES or not _node_materializes(node):
            continue
        if not _prompt_text(node):
            continue
        label = node.get("title") or node.get("id") or node_type
        prompt_source = _field_text(node, "prompt_source")
        missing_fields: list[str] = []
        error_kind = ""
        if not prompt_source:
            missing_fields.append("prompt_source")
            error_kind = "missing_prompt_source"
        elif prompt_source not in _ALLOWED_PROMPT_SOURCES:
            return {
                "ok": False,
                "error": f"{node_type} 节点 {label} 的 fields.prompt_source={prompt_source!r} 不在允许值内。",
                "error_kind": "unsupported_prompt_source",
                "node_id": node.get("id"),
                "allowed_prompt_sources": sorted(_ALLOWED_PROMPT_SOURCES),
            }
        if missing_fields:
            missing_nodes.append(
                {
                    "node_id": node.get("id"),
                    "type": node_type,
                    "title": label,
                    "missing_fields": missing_fields,
                    "error_kind": error_kind,
                }
            )
    if missing_nodes:
        first = missing_nodes[0]
        summary = "; ".join(
            f"{item['node_id']} 缺 {', '.join(item['missing_fields'])}"
            for item in missing_nodes[:8]
        )
        return {
            "ok": False,
            "error": (
                f"{len(missing_nodes)} 个媒体节点已写 prompt 但提示词证据不完整：{summary}。"
                "请一次性修补这些节点后再 finalize。"
            ),
            "error_kind": first["error_kind"],
            "node_id": first["node_id"],
            "missing_nodes": missing_nodes,
            "hint": (
                "prompt 写法来自 skill 或模型按当前节点目标撰写。"
                "修补草稿可直接调用 blueprint.update_tree_node(node_id=..., patch={"
                "'prompt_source': 'skill_or_model_written'})."
            ),
        }
    return None


def _required_video_guide_topics(nodes: list[dict[str, Any]]) -> set[str]:
    video_nodes = [node for node in nodes if node.get("type") == "video" and _node_materializes(node)]
    if not video_nodes:
        return set()
    topics = {"video_workflow"}
    image_nodes = [node for node in nodes if node.get("type") == "image" and _node_materializes(node)]
    joined_image_text = "\n".join(_node_text(node) for node in image_nodes).lower()
    joined_video_text = "\n".join(_node_text(node) for node in video_nodes).lower()
    if any(_has_explicit_text_to_video_path(node, nodes) for node in video_nodes):
        topics.add("video_workflow_t2v")
    if "story_template" in joined_image_text or "故事模板" in joined_image_text:
        topics.add("video_workflow_story_template")
    if "storyboard" in joined_image_text or "分镜" in joined_image_text or "宫格" in joined_image_text:
        topics.add("video_workflow_storyboard")
    if "shot_images" in joined_image_text or "single_shot" in joined_image_text or "单张分镜" in joined_image_text:
        topics.add("video_workflow_shot_images")
    if image_nodes and len(topics) == 1 and "image_to_video" in joined_video_text:
        topics.add("video_workflow_storyboard")
    return topics


def _is_segment_node(node: dict[str, Any]) -> bool:
    return node.get("type") == "text" and _is_segment_hint_text(_segment_identity_text(node))


def _segment_child_order_bucket(node: dict[str, Any]) -> int:
    text = f"{node.get('id') or ''}\n{node.get('title') or ''}\n{node.get('type') or ''}".lower()
    if node.get("type") == "image" and re.search(r"scene|场景", text):
        return 0
    if node.get("type") == "image" and re.search(
        r"storyboard|分镜|宫格|keyframe|首帧|尾帧|story_template|故事模板",
        text,
    ):
        return 1
    if node.get("type") == "video":
        return 2
    return 3


def _normalize_segment_child_order(root: dict[str, Any]) -> None:
    def walk(node: dict[str, Any]) -> None:
        children = node.get("children")
        if isinstance(children, list):
            if len(children) > 1 and _is_segment_node(node):
                indexed = [
                    (index, child)
                    for index, child in enumerate(children)
                    if isinstance(child, dict)
                ]
                if len(indexed) == len(children):
                    indexed.sort(key=lambda item: (_segment_child_order_bucket(item[1]), item[0]))
                    node["children"] = [child for _, child in indexed]
                    children = node["children"]
            for child in children:
                if isinstance(child, dict):
                    walk(child)

    walk(root)


def _node_summary(node: dict[str, Any]) -> dict[str, Any]:
    keys = {"id", "type", "title", "status"}
    return {k: node.get(k) for k in keys if k in node}
