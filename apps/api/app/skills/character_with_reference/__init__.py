"""Internal character-with-reference helper."""
from __future__ import annotations

async def character_with_reference(
    project_id: str,
    reference_description: str,
    role_type: str | None = None,
    name: str | None = None,
    node_id: str | None = None,
) -> dict:
    from app.mcp_tools import drama_tools

    requirements = [
        f"参考图视觉特征：{reference_description}",
        "appearance / visual_prompt 字段必须明确反映上面的视觉特征",
    ]
    return await drama_tools.generate_character(
        project_id=project_id,
        name=name,
        role_type=role_type,
        requirements=requirements,
        node_id=node_id,
    )
