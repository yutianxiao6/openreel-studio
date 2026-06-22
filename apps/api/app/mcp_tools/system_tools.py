"""System introspection tools — let the Agent know its own real configuration."""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.mcp_tools.registry import register, registry


@register("system.status", description="Get real system status: models, tools, MCP servers, capabilities", tags=["system", "read"])
async def system_status() -> dict[str, Any]:
    """Returns the actual system configuration so the Agent never has to guess."""
    from app.config_store import get_store
    from app.services.llm_service import _TASK_DEFAULTS, _default_model_for

    # Model configuration per task
    model_map = {}
    for task_type in _TASK_DEFAULTS:
        model_map[task_type] = _default_model_for(task_type)

    # Tool stats
    all_tools = registry.list_tools()
    namespaces = registry.namespaces()
    tools_by_ns = {}
    for ns in namespaces:
        tools_by_ns[ns] = len([t for t in all_tools if t.namespace == ns])

    # MCP servers
    mcp_servers = []
    try:
        from app.mcp_client import mcp_client_manager
        for name, conn in mcp_client_manager._servers.items():
            mcp_servers.append({
                "name": name,
                "status": "connected" if conn.session else "disconnected",
                "tools": len(conn.tools),
            })
    except Exception:
        pass

    # API keys configured
    providers = {
        "deepseek": bool(settings.DEEPSEEK_API_KEY),
        "openai": bool(settings.OPENAI_API_KEY),
        "anthropic": bool(settings.ANTHROPIC_API_KEY),
        "dashscope": bool(settings.DASHSCOPE_API_KEY),
        "gemini": bool(settings.GEMINI_API_KEY),
    }
    try:
        runtime_config = await get_store().get_runtime()
        agent_loop_max_iterations = int(runtime_config.app_settings.get("agent.max_iterations", 200))
    except Exception:
        agent_loop_max_iterations = 200

    return {
        "models": model_map,
        "default_fast_model": settings.DEFAULT_FAST_MODEL,
        "default_text_model": settings.DEFAULT_TEXT_MODEL,
        "default_script_model": settings.DEFAULT_SCRIPT_MODEL,
        "default_review_model": settings.DEFAULT_REVIEW_MODEL,
        "providers_configured": providers,
        "tools_total": len(all_tools),
        "namespaces": len(namespaces),
        "tools_by_namespace": tools_by_ns,
        "mcp_servers": mcp_servers,
        "agent_loop_max_iterations": agent_loop_max_iterations,
    }


@register("system.models", description="List all task types and their current model assignments", tags=["system", "read"])
async def system_models() -> dict[str, Any]:
    """Focused view: which model is used for each task type."""
    from app.services.llm_service import _TASK_DEFAULTS, _default_model_for

    models = {}
    for task_type in _TASK_DEFAULTS:
        models[task_type] = _default_model_for(task_type)

    return {
        "task_models": models,
        "note": "Use the settings panel or config APIs to change model assignments.",
    }
