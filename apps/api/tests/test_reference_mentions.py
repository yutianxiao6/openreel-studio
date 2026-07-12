import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api import routes_projects
from app.db import session as db_session
from app.db.models import Project, WorkflowNode
from app.mcp_tools import canvas_tools, node_universal
from app.services.reference_mentions import (
    build_reference_mention_candidates,
    parse_reference_mentions,
)


async def _setup_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'reference-mentions.db'}"
    engine = create_async_engine(database_url, echo=False, future=True, connect_args={"timeout": 30})
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "AsyncSessionLocal", session_local)
    await db_session.init_db()
    async with db_session.session_scope() as session:
        session.add(Project(id="project-mentions", title="Mention Test", state_json="{}"))
        await session.commit()


def test_reference_mention_parser_requires_an_exact_candidate_token() -> None:
    candidates = build_reference_mention_candidates([
        {"ref": "node:character", "label": "凌澈 · 人物参考图", "source": "node"},
        {"ref": "node:storyboard", "label": "宫格分镜图", "source": "node"},
    ])

    assert [item["mention"] for item in candidates] == ["@凌澈人物参考图", "@宫格分镜图"]
    matched, unknown, missing = parse_reference_mentions(
        "镜头人物沿用@凌澈人物参考图，构图沿用@宫格分镜图。",
        candidates,
    )
    assert [(item["mention"], item["ref"]) for item in matched] == [
        ("@凌澈人物参考图", "node:character"),
        ("@宫格分镜图", "node:storyboard"),
    ]
    assert unknown == []
    assert missing == []

    matched, unknown, missing = parse_reference_mentions("连续中文也会绑定@凌澈人物参考图保持一致。", candidates)
    assert [item["ref"] for item in matched] == ["node:character"]
    assert unknown == []
    assert missing == ["@宫格分镜图"]

    matched, unknown, missing = parse_reference_mentions("不要误认@不存在的图片。", candidates)
    assert matched == []
    assert unknown == ["@不存在的图片"]
    assert missing == ["@凌澈人物参考图", "@宫格分镜图"]

    matched, unknown, _missing = parse_reference_mentions("@宫格分镜图2不是已有标签。", candidates)
    assert matched == []
    assert unknown == ["@宫格分镜图2不是已有标签"]


@pytest.mark.asyncio
async def test_prompt_mentions_bind_to_image_node_ids_across_reference_reordering(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _setup_db(monkeypatch, tmp_path)
    character = await canvas_tools.create_node(
        project_id="project-mentions",
        node_type="image",
        title="凌澈人物参考图",
        input_data={"title": "凌澈人物参考图"},
    )
    storyboard = await canvas_tools.create_node(
        project_id="project-mentions",
        node_type="image",
        title="宫格分镜图",
        input_data={"title": "宫格分镜图"},
    )
    video = await canvas_tools.create_node(
        project_id="project-mentions",
        node_type="video",
        title="成片",
        prompt="先按@凌澈人物参考图保持人物，再按@宫格分镜图组织镜头。",
        input_data={
            "references": [
                {"ref": f"node:{character['id']}", "role": "visual_reference"},
                {"ref": f"node:{storyboard['id']}", "role": "visual_reference"},
            ],
        },
    )

    before = await canvas_tools.get_node(video["id"])
    before_mentions = before["input"]["reference_image_mentions"]
    assert [(item["mention"], item["ref"], item["index"]) for item in before_mentions] == [
        ("@凌澈人物参考图", f"node:{character['id']}", 1),
        ("@宫格分镜图", f"node:{storyboard['id']}", 2),
    ]

    await canvas_tools.update_node(video["id"], {
        "input_data": {
            **before["input"],
            "references": [
                {"ref": f"node:{storyboard['id']}", "role": "visual_reference"},
                {"ref": f"node:{character['id']}", "role": "visual_reference"},
            ],
        },
    })
    after = await canvas_tools.get_node(video["id"])
    after_by_mention = {
        item["mention"]: (item["ref"], item["index"])
        for item in after["input"]["reference_image_mentions"]
    }
    assert after_by_mention == {
        "@凌澈人物参考图": (f"node:{character['id']}", 2),
        "@宫格分镜图": (f"node:{storyboard['id']}", 1),
    }


@pytest.mark.asyncio
async def test_user_node_detail_save_uses_the_same_backend_mention_parser(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _setup_db(monkeypatch, tmp_path)
    image = await canvas_tools.create_node(
        project_id="project-mentions",
        node_type="image",
        title="雨夜场景图",
        input_data={"title": "雨夜场景图"},
    )
    video = await canvas_tools.create_node(
        project_id="project-mentions",
        node_type="video",
        title="成片",
        input_data={},
    )

    async with db_session.session_scope() as session:
        await routes_projects.update_project_canvas_node_detail(
            project_id="project-mentions",
            node_id=video["id"],
            req=routes_projects.CanvasNodeUpdateRequest(
                prompt="画面沿用@雨夜场景图。",
                input={
                    "references": [
                        {"ref": f"node:{image['id']}", "role": "visual_reference"},
                    ],
                },
            ),
            db=session,
        )

    async with db_session.session_scope() as session:
        stored = await session.get(WorkflowNode, video["id"])
        assert stored is not None
        input_data = json.loads(stored.input_json or "{}")
    assert input_data["reference_image_mentions"] == [{
        "mention": "@雨夜场景图",
        "label": "雨夜场景图",
        "ref": f"node:{image['id']}",
        "source": "node",
        "index": 1,
    }]


@pytest.mark.asyncio
async def test_workflow_llm_interleaves_each_reference_label_with_its_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_session_scope():
        yield object()

    class FakeLLMService:
        def __init__(self, _session: object) -> None:
            pass

        async def generate(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"content": "ok"}

    monkeypatch.setattr(node_universal, "session_scope", fake_session_scope)
    monkeypatch.setattr(node_universal, "LLMService", FakeLLMService)

    await node_universal._call_workflow_text_llm(
        task_type="workflow_text_generation",
        system="system",
        message="message",
        project_id="project-mentions",
        image_urls=["data:image/png;base64,first", "data:image/png;base64,second"],
        image_labels=[
            {"mention": "@人物图", "label": "人物图"},
            {"mention": "@分镜图", "label": "分镜图"},
        ],
    )

    content = captured["messages"][0]["content"]
    assert content == [
        {"type": "text", "text": "message"},
        {"type": "text", "text": "参考图片标签：@人物图（人物图）"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,first"}},
        {"type": "text", "text": "参考图片标签：@分镜图（分镜图）"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,second"}},
    ]
