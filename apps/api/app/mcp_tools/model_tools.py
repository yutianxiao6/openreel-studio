"""Model configuration MCP tools."""
from __future__ import annotations

from sqlmodel import select

from app.db.models import ModelConfig
from app.db.session import session_scope


async def list_model_configs() -> list[dict]:
    async with session_scope() as session:
        result = await session.exec(select(ModelConfig))
        return [c.model_dump() for c in result.all()]


async def get_model_config(task_type: str) -> dict | None:
    async with session_scope() as session:
        result = await session.exec(
            select(ModelConfig).where(
                ModelConfig.task_type == task_type,
                ModelConfig.enabled == True,  # noqa: E712
            )
        )
        config = result.first()
        return config.model_dump() if config else None


async def set_model_config(
    task_type: str, provider: str, model_name: str, **kwargs
) -> dict:
    async with session_scope() as session:
        result = await session.exec(
            select(ModelConfig).where(ModelConfig.task_type == task_type)
        )
        config = result.first()
        if config:
            config.provider = provider
            config.model_name = model_name
            for k, v in kwargs.items():
                if hasattr(config, k):
                    setattr(config, k, v)
        else:
            config = ModelConfig(
                task_type=task_type,
                provider=provider,
                model_name=model_name,
                **kwargs,
            )
            session.add(config)
        await session.commit()
        await session.refresh(config)
        return config.model_dump()
