from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.db.models import ModelConfig
from app.db.session import get_session

router = APIRouter()


@router.get("/configs")
async def list_model_configs(db: AsyncSession = Depends(get_session)):
    result = await db.exec(select(ModelConfig).order_by(ModelConfig.task_type))
    configs = list(result.all())
    return {
        "defaults": {
            "text": settings.DEFAULT_TEXT_MODEL,
            "fast": settings.DEFAULT_FAST_MODEL,
            "script": settings.DEFAULT_SCRIPT_MODEL,
            "review": settings.DEFAULT_REVIEW_MODEL,
        },
        "configs": [c.model_dump() for c in configs],
    }


@router.get("/providers")
async def list_providers():
    return {
        "openai": bool(settings.OPENAI_API_KEY),
        "anthropic": bool(settings.ANTHROPIC_API_KEY),
        "deepseek": bool(settings.DEEPSEEK_API_KEY),
        "dashscope": bool(settings.DASHSCOPE_API_KEY),
        "gemini": bool(settings.GEMINI_API_KEY),
    }
