from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes_assets import router as assets_router
from app.api.routes_agent_debug import router as agent_debug_router
from app.api.routes_chat import router as chat_router
from app.api.routes_media import router as media_router
from app.api.routes_models import router as models_router
from app.api.routes_nodes import router as nodes_router
from app.api.routes_projects import router as projects_router
from app.api.routes_tools import router as tools_router
from app.api.routes_uploads import router as uploads_router
from app.api.routes_workflow import router as workflow_router
from app.config import settings
from app.db.session import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    from app.services.node_recovery import cleanup_interrupted_media_nodes
    try:
        await cleanup_interrupted_media_nodes(
            stale_after_seconds=None,
            reason="api_startup_interrupted_media",
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Interrupted media node recovery failed")
    # ConfigStore：bootstrap (首启自动 seed .env keys 到 runtime.jsonc) + 启动 watcher
    from app.config_store import get_store
    store = get_store()
    env_keys = {
        "OPENAI_API_KEY": settings.OPENAI_API_KEY,
        "ANTHROPIC_API_KEY": settings.ANTHROPIC_API_KEY,
        "DEEPSEEK_API_KEY": settings.DEEPSEEK_API_KEY,
        "DASHSCOPE_API_KEY": settings.DASHSCOPE_API_KEY,
        "GEMINI_API_KEY": settings.GEMINI_API_KEY,
    }
    ok, errs = await store.bootstrap(env_keys)
    if not ok:
        import logging
        logging.getLogger(__name__).error("ConfigStore bootstrap failed: %s", errs)
    await store.start_watcher()
    # Connect to external MCP servers (non-fatal if any fail)
    from app.mcp_client import mcp_client_manager
    try:
        connected = await mcp_client_manager.connect_all()
        if connected:
            import logging
            logging.getLogger(__name__).info("MCP external servers: %s", connected)
    except Exception:
        pass
    yield
    # Cleanup
    try:
        await store.stop_watcher()
    except Exception:
        pass
    try:
        await mcp_client_manager.disconnect_all()
    except Exception:
        pass


app = FastAPI(
    title="OpenReel Studio API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router, prefix="/api/chat", tags=["chat"])
app.include_router(agent_debug_router, prefix="/api/agent", tags=["agent-debug"])
app.include_router(projects_router, prefix="/api/projects", tags=["projects"])
app.include_router(nodes_router, prefix="/api/nodes", tags=["nodes"])
app.include_router(assets_router, prefix="/api/assets", tags=["assets"])
app.include_router(media_router, prefix="/api/media", tags=["media"])
app.include_router(models_router, prefix="/api/models", tags=["models"])
app.include_router(tools_router, prefix="/api/tools", tags=["tools"])
app.include_router(uploads_router, prefix="/api/uploads", tags=["uploads"])
app.include_router(workflow_router, prefix="/api/workflow", tags=["workflow"])


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.1.0", "app": "openreel-studio"}
