from agent_plan_contract_helpers import *  # noqa: F401,F403

import base64

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api import routes_assets
from app.db import session as db_session
from app.db.models import Asset, Project, WorkflowNode
from app.mcp_tools import asset_library_tools
from app.mcp_tools import canvas_tools
from app.mcp_tools import media_tools
from app.mcp_tools import reference_tools
from app.mcp_tools.file_tools import write_image_base64_cache
from app.services import llm_service as llm_service_module


async def _setup_reference_db(monkeypatch, tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'reference-assets.db'}"
    engine = create_async_engine(database_url, echo=False, future=True, connect_args={"timeout": 30})
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "AsyncSessionLocal", session_local)
    monkeypatch.setattr(reference_tools.settings, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(reference_tools.settings, "STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setattr(reference_tools.settings, "STORAGE_PATH", str(tmp_path / "storage"))
    await db_session.init_db()
    async with db_session.session_scope() as session:
        project = Project(id="project-1", title="参考图测试", state_json="{}")
        session.add(project)
        await session.commit()
    upload_dir = tmp_path / "storage" / "project-1" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / "style.png").write_bytes(
        base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")
    )
    return engine


@pytest.mark.asyncio
async def test_reference_manage_ingests_and_resolves_uploaded_image(monkeypatch, tmp_path) -> None:
    await _setup_reference_db(monkeypatch, tmp_path)

    result = await reference_tools.reference_manage(
        project_id="project-1",
        action="ingest_attachments",
        attachments=[{
            "kind": "image",
            "rel_path": "uploads/style.png",
            "filename": "style.png",
            "mention": "@水墨",
            "mime_type": "image/png",
        }],
    )

    assert result["ok"] is True
    assert result["assets"][0]["mention"] == "@水墨"

    resolved = await reference_tools.reference_manage(
        project_id="project-1",
        action="resolve",
        mention="@水墨",
    )
    assert resolved["ok"] is True
    assert resolved["asset"]["rel_path"] == "uploads/style.png"


@pytest.mark.asyncio
async def test_reference_analyze_persists_visual_analysis(monkeypatch, tmp_path) -> None:
    await _setup_reference_db(monkeypatch, tmp_path)
    await reference_tools.reference_manage(
        project_id="project-1",
        action="register",
        rel_path="uploads/style.png",
        mention="@图1",
    )

    async def fake_generate(**kwargs):
        return {
            "model": "vision-test",
            "usage": {"total_tokens": 123},
            "content": json.dumps({
                "summary": "水墨国风人物海报参考",
                "subject": "持剑人物",
                "style_name": "水墨国风",
                "style_tags": ["水墨", "国风"],
                "color_palette": ["墨黑", "宣纸白"],
                "lighting": "柔和侧光",
                "composition": "留白构图",
                "camera_language": "中景",
                "texture": "宣纸颗粒",
                "mood": "冷峻",
                "usable_roles": ["style_reference"],
                "prompt_fragment": "水墨国风，宣纸颗粒，墨色晕染，留白构图",
                "negative_constraints": ["不要霓虹"],
            }, ensure_ascii=False),
        }

    monkeypatch.setattr(llm_service_module.llm_service, "generate", fake_generate, raising=False)

    result = await reference_tools.reference_manage(
        project_id="project-1",
        action="analyze",
        mention="@图1",
        include_analysis=True,
    )

    assert result["ok"] is True
    assert result["asset"]["status"] == "analyzed"
    assert result["asset"]["analysis"]["style_tags"] == ["水墨", "国风"]
    assert "宣纸颗粒" in result["asset"]["analysis"]["prompt_fragment"]


@pytest.mark.asyncio
async def test_reference_analyze_uses_uploaded_base64_cache(monkeypatch, tmp_path) -> None:
    await _setup_reference_db(monkeypatch, tmp_path)
    source_path = tmp_path / "storage" / "project-1" / "uploads" / "style.png"
    cache = write_image_base64_cache(
        "project-1",
        "uploads/style.png",
        source_path=source_path,
        mime_type="image/png",
    )
    await reference_tools.reference_manage(
        project_id="project-1",
        action="ingest_attachments",
        attachments=[{
            "kind": "image",
            "rel_path": "uploads/style.png",
            "filename": "style.png",
            "mention": "@缓存图",
            "mime_type": "image/png",
            "base64_rel_path": cache["base64_rel_path"],
        }],
    )

    captured: dict[str, object] = {}

    async def fake_generate(**kwargs):
        captured["messages"] = kwargs["messages"]
        return {
            "model": "vision-test",
            "usage": {"total_tokens": 10},
            "content": json.dumps({
                "summary": "base64 缓存图",
                "style_tags": ["缓存"],
                "prompt_fragment": "使用 base64 缓存图的视觉特征",
            }, ensure_ascii=False),
        }

    monkeypatch.setattr(llm_service_module.llm_service, "generate", fake_generate, raising=False)

    result = await reference_tools.reference_manage(
        project_id="project-1",
        action="analyze",
        mention="@缓存图",
        include_analysis=True,
    )

    assert result["ok"] is True
    assert result["asset"]["base64_rel_path"] == cache["base64_rel_path"]
    content = captured["messages"][0]["content"]  # type: ignore[index]
    image_part = next(part for part in content if part.get("type") == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_media_describe_image_accepts_generated_media_url(monkeypatch, tmp_path) -> None:
    await _setup_reference_db(monkeypatch, tmp_path)
    generated_dir = tmp_path / "storage" / "project-1" / "generated_images"
    generated_dir.mkdir(parents=True, exist_ok=True)
    (generated_dir / "gen.png").write_bytes(
        base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")
    )

    async def fake_generate(**kwargs):
        return {
            "model": "vision-test",
            "usage": {"total_tokens": 10},
            "content": json.dumps({
                "summary": "生成图可识别",
                "style_tags": ["生成图"],
                "prompt_fragment": "生成图的视觉特征",
            }, ensure_ascii=False),
        }

    monkeypatch.setattr(llm_service_module.llm_service, "generate", fake_generate, raising=False)

    result = await media_tools.describe_image(
        project_id="project-1",
        rel_path="/api/media/project-1/gen.png",
    )

    assert result["ok"] is True
    assert result["path"] == "generated_images/gen.png"
    assert result["description"] == "生成图可识别"
    assert result["asset"]["reference_input"] == "generated_images/gen.png"


@pytest.mark.asyncio
async def test_reference_bind_updates_active_blueprint_file(monkeypatch, tmp_path) -> None:
    await _setup_reference_db(monkeypatch, tmp_path)
    project_dir = tmp_path / "data" / "projects" / "project-1"
    project_dir.mkdir(parents=True, exist_ok=True)
    blueprint_doc = {
        "id": "bp-1",
        "version": 1,
        "status": "active",
        "theme": {"title": "桥头", "duration_seconds": 15},
        "production": {"video_mode": "grid", "episode_count": 1, "segment_seconds": 15},
        "story": {"global_outline": "雨夜桥头。", "episodes": []},
        "characters": [],
        "scenes": [],
    }
    (project_dir / "blueprint.json").write_text(json.dumps(blueprint_doc, ensure_ascii=False), encoding="utf-8")
    async with db_session.session_scope() as session:
        project = await session.get(Project, "project-1")
        state = {
            "project_blueprint": {
                "id": "bp-1",
                "status": "active",
                "file_json": "data/projects/project-1/blueprint.json",
                "file_markdown": "data/projects/project-1/blueprint.md",
                "file_view_model": "data/projects/project-1/blueprint_view_model.json",
                "checksum": "old",
            }
        }
        project.state_json = json.dumps(state, ensure_ascii=False)
        session.add(project)
        await session.commit()

    await reference_tools.reference_manage(
        project_id="project-1",
        action="register",
        rel_path="uploads/style.png",
        mention="@图1",
    )
    result = await reference_tools.reference_manage(
        project_id="project-1",
        action="bind_to_blueprint",
        mention="@图1",
        role="style_reference",
        apply_to=["text", "image", "video"],
    )

    assert result["ok"] is True
    updated = json.loads((project_dir / "blueprint.json").read_text(encoding="utf-8"))
    assert updated["reference_images"][0]["mention"] == "@图1"
    assert updated["reference_bindings"][0]["role"] == "style_reference"
    assert (project_dir / "blueprint_view_model.json").exists()


@pytest.mark.asyncio
async def test_reference_manage_registers_generated_asset_record(monkeypatch, tmp_path) -> None:
    await _setup_reference_db(monkeypatch, tmp_path)
    generated_dir = tmp_path / "storage" / "project-1" / "generated_images"
    generated_dir.mkdir(parents=True, exist_ok=True)
    generated_path = generated_dir / "gen.png"
    generated_path.write_bytes(
        base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")
    )
    async with db_session.session_scope() as session:
        asset = Asset(
            id="asset-generated-1",
            project_id="project-1",
            type="scene_image",
            name="生成场景图",
            path=str(generated_path),
            url="/api/media/project-1/gen.png",
            metadata_json=json.dumps({"status": "completed", "local_path": str(generated_path)}, ensure_ascii=False),
        )
        session.add(asset)
        await session.commit()

    result = await reference_tools.reference_manage(
        project_id="project-1",
        action="register_asset",
        asset_id="asset-generated-1",
        mention="@生成图",
    )

    assert result["ok"] is True
    assert result["asset"]["mention"] == "@生成图"
    assert result["asset"]["asset_id"] == "asset-generated-1"
    assert result["asset"]["reference_input"] == "generated_images/gen.png"


@pytest.mark.asyncio
async def test_reference_manage_registers_generated_node_public_id(monkeypatch, tmp_path) -> None:
    await _setup_reference_db(monkeypatch, tmp_path)
    generated_dir = tmp_path / "storage" / "project-1" / "generated_images"
    generated_dir.mkdir(parents=True, exist_ok=True)
    generated_path = generated_dir / "node-gen.png"
    generated_path.write_bytes(
        base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")
    )
    internal_node_id = "8e6b1b8a-c4e1-4f8d-8e60-ec57c3300012"
    async with db_session.session_scope() as session:
        session.add(WorkflowNode(
            id=internal_node_id,
            project_id="project-1",
            display_id=12,
            type="image",
            title="节点编号测试图",
            status="completed",
            position_x=0,
            position_y=0,
            input_json=json.dumps({"title": "节点编号测试图"}, ensure_ascii=False),
            output_json=json.dumps({
                "type": "fusion",
                "stages": [{
                    "name": "图片",
                    "status": "completed",
                    "url": "/api/media/project-1/node-gen.png",
                    "local_url": "/api/media/project-1/node-gen.png",
                }],
            }, ensure_ascii=False),
            model_config_json="{}",
            prompt="",
            version=1,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        ))
        await session.commit()

    result = await reference_tools.reference_manage(
        project_id="project-1",
        action="register",
        node_id="12",
        mention="@节点12",
    )

    assert result["ok"] is True
    assert result["asset"]["mention"] == "@节点12"
    assert result["asset"]["node_id"] == "12"
    assert result["asset"]["rel_path"] == "generated_images/node-gen.png"


@pytest.mark.asyncio
async def test_reference_manage_registers_asset_library_file(monkeypatch, tmp_path) -> None:
    await _setup_reference_db(monkeypatch, tmp_path)
    library_dir = tmp_path / "asset-library"
    library_dir.mkdir(parents=True, exist_ok=True)
    library_path = library_dir / "ink-style.png"
    library_path.write_bytes(
        base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")
    )

    result = await reference_tools.reference_manage(
        project_id="project-1",
        action="register_file",
        source_path=str(library_path),
        mention="@资产库水墨",
    )

    assert result["ok"] is True
    assert result["asset"]["mention"] == "@资产库水墨"
    assert result["asset"]["source"] == "file"
    assert result["asset"]["source_path"] == str(library_path)
    assert result["asset"]["reference_input"] == str(library_path)


@pytest.mark.asyncio
async def test_asset_library_save_accepts_generated_asset_reference(monkeypatch, tmp_path) -> None:
    await _setup_reference_db(monkeypatch, tmp_path)
    generated_dir = tmp_path / "storage" / "project-1" / "generated_images"
    generated_dir.mkdir(parents=True, exist_ok=True)
    generated_path = generated_dir / "gen.png"
    generated_path.write_bytes(
        base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")
    )
    project_root = tmp_path / "project-library"
    async with db_session.session_scope() as session:
        project = await session.get(Project, "project-1")
        project.state_json = json.dumps({
            "metadata": {"title": "测试短剧"},
            "asset_library": {"project_root": str(project_root)},
        }, ensure_ascii=False)
        session.add(Asset(
            id="asset-generated-2",
            project_id="project-1",
            type="scene_image",
            name="生成图",
            path=str(generated_path),
            url="/api/media/project-1/gen.png",
            metadata_json=json.dumps({"status": "completed", "local_path": str(generated_path)}, ensure_ascii=False),
        ))
        session.add(project)
        await session.commit()

    result = await asset_library_tools.assets_save_to_project(
        project_id="project-1",
        episode=1,
        kind="scene",
        source="asset:asset-generated-2",
        name="桥头场景",
    )

    assert result["ok"] is True
    saved_path = Path(result["path"])
    assert saved_path.exists()
    assert saved_path.name == "桥头场景.png"


@pytest.mark.asyncio
async def test_asset_library_save_accepts_generated_node_public_id(monkeypatch, tmp_path) -> None:
    await _setup_reference_db(monkeypatch, tmp_path)
    generated_dir = tmp_path / "storage" / "project-1" / "generated_images"
    generated_dir.mkdir(parents=True, exist_ok=True)
    generated_path = generated_dir / "node-gen.png"
    generated_path.write_bytes(
        base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")
    )
    project_root = tmp_path / "project-library"
    async with db_session.session_scope() as session:
        project = await session.get(Project, "project-1")
        project.state_json = json.dumps({
            "metadata": {"title": "测试短剧"},
            "asset_library": {"project_root": str(project_root)},
        }, ensure_ascii=False)
        session.add(WorkflowNode(
            id="8e6b1b8a-c4e1-4f8d-8e60-ec57c3300012",
            project_id="project-1",
            display_id=12,
            type="image",
            title="节点编号测试图",
            status="completed",
            position_x=0,
            position_y=0,
            input_json=json.dumps({"title": "节点编号测试图"}, ensure_ascii=False),
            output_json=json.dumps({
                "type": "fusion",
                "stages": [{
                    "name": "图片",
                    "status": "completed",
                    "url": "/api/media/project-1/node-gen.png",
                    "local_url": "/api/media/project-1/node-gen.png",
                }],
            }, ensure_ascii=False),
            model_config_json="{}",
            prompt="",
            version=1,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        ))
        session.add(project)
        await session.commit()

    result = await asset_library_tools.assets_save_to_project(
        project_id="project-1",
        episode=1,
        kind="scene",
        source="node:12",
        name="节点编号场景",
    )

    assert result["ok"] is True
    saved_path = Path(result["path"])
    assert saved_path.exists()
    assert saved_path.name == "节点编号场景.png"


@pytest.mark.asyncio
async def test_canvas_delete_accepts_public_node_ids(monkeypatch, tmp_path) -> None:
    await _setup_reference_db(monkeypatch, tmp_path)
    internal_node_id = "8e6b1b8a-c4e1-4f8d-8e60-ec57c3300012"
    async with db_session.session_scope() as session:
        session.add(WorkflowNode(
            id=internal_node_id,
            project_id="project-1",
            display_id=12,
            type="image",
            title="待删除编号节点",
            status="idle",
            position_x=0,
            position_y=0,
            input_json=json.dumps({"title": "待删除编号节点"}, ensure_ascii=False),
            output_json="{}",
            model_config_json="{}",
            prompt="",
            version=1,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        ))
        await session.commit()

    result = await canvas_tools.delete_canvas(
        project_id="project-1",
        scope="selected",
        node_ids=["12"],
    )

    assert result["ok"] is True
    assert result["deleted_node_ids"] == ["12"]
    assert result["_canvas_deleted_node_ids"] == [internal_node_id]
    async with db_session.session_scope() as session:
        assert await session.get(WorkflowNode, internal_node_id) is None


@pytest.mark.asyncio
async def test_asset_library_preview_route_is_scoped_to_configured_roots(monkeypatch, tmp_path) -> None:
    await _setup_reference_db(monkeypatch, tmp_path)
    library_dir = tmp_path / "asset-library"
    library_dir.mkdir(parents=True, exist_ok=True)
    library_path = library_dir / "ink-style.png"
    library_path.write_bytes(
        base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")
    )
    outside_path = tmp_path / "outside.png"
    outside_path.write_bytes(library_path.read_bytes())
    async with db_session.session_scope() as session:
        project = await session.get(Project, "project-1")
        project.state_json = json.dumps({
            "asset_library": {"project_root": str(library_dir)},
        }, ensure_ascii=False)
        session.add(project)
        await session.commit()

    async with db_session.session_scope() as session:
        response = await routes_assets.preview_asset_library_file(
            project_id="project-1",
            path=str(library_path),
            db=session,
        )
        assert response.media_type == "image/png"
        with pytest.raises(HTTPException) as exc_info:
            await routes_assets.preview_asset_library_file(
                project_id="project-1",
                path=str(outside_path),
                db=session,
            )
        assert exc_info.value.status_code == 403
