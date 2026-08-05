"""Media cancellation and provider-preset helpers."""

from __future__ import annotations


async def cancel_image_generation(project_id: str, reason: str = "") -> dict:
    """Request cancellation of the active image/chat generation for a project."""
    from app.agent import message_queue as mq

    result = await mq.request_cancel(
        project_id,
        reason or "用户要求停止图片生成",
    )
    return {
        **result,
        "status": "cancel_requested",
        "message": "已请求停止当前图片生成。若外部图片服务已接收请求，系统会在下一个安全点停止后续执行和写回。",
    }


async def get_presets(
    model_name: str | None = None,
) -> dict:
    """Get image provider defaults declared by the UMA image target catalog."""
    from app.services.media_provider import match_preset, list_presets, get_preset_descriptions

    if model_name:
        preset = match_preset(model_name)
        return {
            "model_name": model_name,
            "preset": preset or {},
            "descriptions": get_preset_descriptions(),
        }

    return {
        "presets": list_presets(),
        "descriptions": get_preset_descriptions(),
    }
