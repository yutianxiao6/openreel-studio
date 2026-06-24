"""Async DB session — SQLModel + aiosqlite."""
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings


engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True,
    connect_args={
        "timeout": 30,  # 30-second busy timeout for SQLite
    },
    pool_size=5,
    max_overflow=10,
)
AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yields one session per request."""
    async with AsyncSessionLocal() as session:
        yield session


# Backwards-compatible alias used in some routes
get_db = get_session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Context-manager form for use inside services / tools."""
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    """Create all SQLModel tables. Import models so they register with metadata."""
    # noqa: F401 — these imports register table metadata
    from app.db import models  # noqa: F401
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        # Enable WAL mode for better concurrent read/write performance
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA synchronous=NORMAL"))
        # Lightweight migrations for SQLite — create_all does not ALTER existing
        # tables, so we add new columns ourselves when they're missing.
        await _ensure_column(
            conn, "workflow_nodes", "supersedes_id", "VARCHAR"
        )
        await _ensure_column(
            conn, "workflow_nodes", "display_id", "INTEGER"
        )
        await _backfill_workflow_node_display_ids(conn)
        await _ensure_column(
            conn, "model_configs", "llm_provider_name", "VARCHAR"
        )
        await _ensure_column(
            conn, "messages", "archived", "BOOLEAN NOT NULL DEFAULT 0"
        )
        await _ensure_column(conn, "llm_providers", "context_window_tokens", "INTEGER")
        await _ensure_column(conn, "llm_providers", "max_input_tokens", "INTEGER")
        await _ensure_column(conn, "llm_providers", "max_output_tokens", "INTEGER")
        await _ensure_column(conn, "llm_providers", "supports_prompt_cache", "BOOLEAN")
        await _ensure_column(conn, "llm_providers", "supports_vision", "BOOLEAN")
        await _ensure_column(conn, "llm_providers", "tokenizer", "VARCHAR")
        await _ensure_column(conn, "llm_providers", "params_json", "VARCHAR")


async def _ensure_column(conn, table: str, column: str, ddl_type: str) -> None:
    from sqlalchemy import text
    rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).all()
    existing = {r[1] for r in rows}
    if column in existing:
        return
    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


async def _backfill_workflow_node_display_ids(conn) -> None:
    from sqlalchemy import text

    projects = (await conn.execute(text(
        "SELECT DISTINCT project_id FROM workflow_nodes WHERE display_id IS NULL"
    ))).all()
    for (project_id,) in projects:
        rows = (await conn.execute(
            text(
                "SELECT id FROM workflow_nodes "
                "WHERE project_id = :project_id "
                "ORDER BY created_at, id"
            ),
            {"project_id": project_id},
        )).all()
        used_rows = (await conn.execute(
            text(
                "SELECT display_id FROM workflow_nodes "
                "WHERE project_id = :project_id AND display_id IS NOT NULL"
            ),
            {"project_id": project_id},
        )).all()
        used = {int(value) for (value,) in used_rows if value is not None}
        next_display_id = 0
        for _, (node_id,) in enumerate(rows):
            current = (await conn.execute(
                text("SELECT display_id FROM workflow_nodes WHERE id = :node_id"),
                {"node_id": node_id},
            )).scalar_one_or_none()
            if current is not None:
                continue
            while next_display_id in used:
                next_display_id += 1
            await conn.execute(
                text(
                    "UPDATE workflow_nodes SET display_id = :display_id "
                    "WHERE id = :node_id AND display_id IS NULL"
                ),
                {"display_id": next_display_id, "node_id": node_id},
            )
            used.add(next_display_id)
            next_display_id += 1
