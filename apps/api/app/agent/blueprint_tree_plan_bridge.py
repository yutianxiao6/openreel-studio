"""Bridge semantic blueprint trees to user-visible confirmation plans."""
from __future__ import annotations

from typing import Any


def build_blueprint_tree_plan_doc(
    *,
    container: dict[str, Any],
    tree_version: Any,
    tree_summary: dict[str, Any],
    tree_nodes: list[dict[str, Any]],
    replacement: bool = False,
) -> dict[str, Any]:
    plan_summary = container.get("summary") or f"语义蓝图树已生成，共 {tree_summary.get('node_count', 0)} 个节点。"
    plan_doc: dict[str, Any] = {
        "kind": "blueprint_tree",
        "title": container.get("title") or "视频蓝图",
        "summary": plan_summary,
        "source_request": container.get("source_request") or "",
        "tree_version": tree_version,
        "tree_summary": tree_summary,
        "tree_nodes": tree_nodes,
        "sections": [
            {
                "type": "tree_preview",
                "content": plan_summary,
                "items": tree_nodes,
            }
        ],
    }
    if replacement:
        replaces = container.get("replaces") if isinstance(container.get("replaces"), dict) else {}
        plan_doc.update({
            "replacement": True,
            "replacement_mode": "replace_active_blueprint",
            "replace_reason": container.get("replace_reason") or "",
            "replaces": replaces,
            "replaces_tree_version": replaces.get("tree_version"),
            "replaces_checksum": replaces.get("checksum"),
            "replacement_notice": "确认后会替换当前 active blueprint，并清理旧蓝图物化节点；确认前旧蓝图不变。",
        })
    return plan_doc
