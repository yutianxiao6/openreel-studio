from agent_plan_contract_helpers import *  # noqa: F401,F403

import base64

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api import routes_assets, routes_projects
from app.config import settings
from app.db import session as db_session
from app.db.models import Asset, Project, WorkflowNode
from app.mcp_tools import asset_library_tools, canvas_tools, node_universal


async def _setup_asset_db(monkeypatch, tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'asset-library.db'}"
    engine = create_async_engine(database_url, echo=False, future=True, connect_args={"timeout": 30})
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "AsyncSessionLocal", session_local)
    monkeypatch.setattr(settings, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setattr(settings, "STORAGE_PATH", str(tmp_path / "storage"))
    await db_session.init_db()
    async with db_session.session_scope() as session:
        project = Project(id="project-1", title="资产库测试", state_json="{}")
        session.add(project)
        await session.commit()
    upload_dir = tmp_path / "storage" / "project-1" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / "style.png").write_bytes(
        base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")
    )
    return engine


@pytest.mark.asyncio
async def test_node_references_accept_uploaded_image_rel_path() -> None:
    refs, warnings = await node_universal._normalize_reference_images_for_render(
        "project-1",
        [{"ref": "upload:uploads/style.png", "role": "visual_reference"}],
    )

    assert refs == ["uploads/style.png"]
    assert warnings == []


def test_source_image_accepts_uploaded_image_reference() -> None:
    output = node_universal._image_output_from_source_value("project-1", "upload:uploads/style.png")

    assert output == {
        "url": "/api/uploads/project-1/file/uploads/style.png",
        "local_url": "/api/uploads/project-1/file/uploads/style.png",
    }


@pytest.mark.asyncio
async def test_asset_library_save_accepts_generated_asset_reference(monkeypatch, tmp_path) -> None:
    await _setup_asset_db(monkeypatch, tmp_path)
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
    assert (saved_path.parent / f".{saved_path.name}.openreel.json").exists()

    listed = await asset_library_tools.assets_list_project(
        project_id="project-1",
        episode=1,
        kind="scene",
    )
    assert listed["count"] == 1
    assert listed["items"][0]["title"] == "桥头场景"
    assert listed["items"][0]["resolution"] == "1x1"


@pytest.mark.asyncio
async def test_asset_library_defaults_to_project_root_assets(monkeypatch, tmp_path) -> None:
    await _setup_asset_db(monkeypatch, tmp_path)
    generated_dir = tmp_path / "storage" / "project-1" / "generated_images"
    generated_dir.mkdir(parents=True, exist_ok=True)
    generated_path = generated_dir / "default-gen.png"
    generated_path.write_bytes(
        base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")
    )
    async with db_session.session_scope() as session:
        project = await session.get(Project, "project-1")
        project.state_json = json.dumps({"metadata": {"title": "默认资产库项目"}}, ensure_ascii=False)
        session.add(Asset(
            id="asset-default-library",
            project_id="project-1",
            type="scene_image",
            name="默认目录测试图",
            path=str(generated_path),
            metadata_json=json.dumps({"local_path": str(generated_path)}, ensure_ascii=False),
        ))
        session.add(project)
        await session.commit()

    path_info = await asset_library_tools.assets_get_library_path(project_id="project-1")
    assert path_info["configured"] is True
    assert path_info["using_default"] is True
    assert Path(path_info["root"]) == tmp_path / "assets"
    assert Path(path_info["project_root"]) == tmp_path / "assets"
    assert Path(path_info["shared_root"]) == tmp_path / "assets"

    result = await asset_library_tools.assets_save_to_project(
        project_id="project-1",
        episode=1,
        kind="scene",
        source="asset:asset-default-library",
        name="默认保存场景",
    )

    saved_path = Path(result["path"])
    assert result["ok"] is True
    assert saved_path.exists()
    assert saved_path.is_relative_to(tmp_path / "assets")
    assert saved_path.name == "默认保存场景.png"


@pytest.mark.asyncio
async def test_asset_library_save_accepts_generated_node_public_id(monkeypatch, tmp_path) -> None:
    await _setup_asset_db(monkeypatch, tmp_path)
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
    await _setup_asset_db(monkeypatch, tmp_path)
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
    await _setup_asset_db(monkeypatch, tmp_path)
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


@pytest.mark.asyncio
async def test_asset_library_preview_route_allows_default_project_root_assets(monkeypatch, tmp_path) -> None:
    await _setup_asset_db(monkeypatch, tmp_path)
    library_dir = tmp_path / "assets" / "场景" / "city"
    library_dir.mkdir(parents=True, exist_ok=True)
    library_path = library_dir / "海的女儿.png"
    library_path.write_bytes(
        base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")
    )
    outside_path = tmp_path / "outside.png"
    outside_path.write_bytes(library_path.read_bytes())

    async with db_session.session_scope() as session:
        response = await routes_assets.preview_asset_library_file(
            project_id="project-1",
            path=str(library_path),
            db=session,
        )
        assert response.media_type == "image/png"
        content_disposition = response.headers["content-disposition"]
        assert "filename=\"asset.png\"" in content_disposition
        assert "filename*=UTF-8''%E6%B5%B7%E7%9A%84%E5%A5%B3%E5%84%BF.png" in content_disposition
        with pytest.raises(HTTPException) as exc_info:
            await routes_assets.preview_asset_library_file(
                project_id="project-1",
                path=str(outside_path),
                db=session,
            )
        assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_asset_library_categories_move_and_add_to_canvas(monkeypatch, tmp_path) -> None:
    await _setup_asset_db(monkeypatch, tmp_path)
    project_root = tmp_path / "project-library"
    shared_root = tmp_path / "shared-library"
    source_dir = shared_root / "characters" / "unsorted"
    source_dir.mkdir(parents=True, exist_ok=True)
    source_path = source_dir / "hero.png"
    source_path.write_bytes(
        base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")
    )
    async with db_session.session_scope() as session:
        project = await session.get(Project, "project-1")
        project.state_json = json.dumps({
            "metadata": {"title": "测试短剧"},
            "asset_library": {
                "project_root": str(project_root),
                "shared_root": str(shared_root),
            },
        }, ensure_ascii=False)
        session.add(project)
        await session.commit()

    shared_category = await asset_library_tools.assets_create_category(
        project_id="project-1",
        library="shared",
        kind="scene",
        category="city_night",
    )
    project_category = await asset_library_tools.assets_create_category(
        project_id="project-1",
        library="project",
        kind="storyboard",
        episode=2,
    )
    invalid_category = await asset_library_tools.assets_create_category(
        project_id="project-1",
        kind="first_frame",
        category="old_kind",
    )

    assert shared_category["ok"] is True
    assert Path(shared_category["path"]).exists()
    assert project_category["ok"] is True
    assert Path(project_category["path"]).exists()
    assert project_category["library"] == "asset"
    assert project_category["category"] == "第2集"
    assert "error" in invalid_category

    legacy_items = await asset_library_tools.assets_list_shared(
        project_id="project-1",
        kind="character",
        category="unsorted",
    )
    assert legacy_items["count"] == 1
    assert legacy_items["items"][0]["title"] == "hero"

    moved = await asset_library_tools.assets_move_asset(
        project_id="project-1",
        path=str(source_path),
        library="shared",
        kind="scene",
        category="city_night",
    )

    moved_path = Path(moved["path"])
    assert moved["ok"] is True
    assert moved_path.exists()
    assert not source_path.exists()
    assert moved_path.parent.name == "city_night"

    categories = await asset_library_tools.assets_list_categories(project_id="project-1")
    assert categories["kinds"] == ["character", "scene", "storyboard"]
    assert any(item["category"] == "city_night" and item["count"] == 1 for item in categories["shared"])
    assert categories["project"] == []

    added = await asset_library_tools.assets_add_to_canvas(
        project_id="project-1",
        source=str(moved_path),
        title="资产主角图",
        node_type="image",
    )

    assert added["ok"] is True
    assert added["node_id"] == "0"
    async with db_session.session_scope() as session:
        node = (await session.exec(select(WorkflowNode).where(WorkflowNode.project_id == "project-1"))).first()
    assert node is not None
    assert node.type == "image"
    assert node.status == "completed"
    output = json.loads(node.output_json or "{}")
    assert output["type"] == "image"
    assert output["local_url"].startswith("/api/assets/project-1/preview?")


@pytest.mark.asyncio
async def test_project_media_history_lists_only_explicit_node_media(monkeypatch, tmp_path) -> None:
    await _setup_asset_db(monkeypatch, tmp_path)
    generated_dir = tmp_path / "storage" / "project-1" / "generated_images"
    generated_dir.mkdir(parents=True, exist_ok=True)
    generated_path = generated_dir / "history-image.png"
    generated_path.write_bytes(
        base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")
    )
    orphan_path = generated_dir / "orphan-image.png"
    orphan_path.write_bytes(generated_path.read_bytes())
    image_ops_dir = generated_dir / "image_ops"
    image_ops_dir.mkdir(parents=True, exist_ok=True)
    edit_preview_path = image_ops_dir / "edit-preview-source-node.png"
    edit_preview_path.write_bytes(generated_path.read_bytes())

    async with db_session.session_scope() as session:
        session.add(WorkflowNode(
            id="source-node",
            project_id="project-1",
            type="image",
            title="历史角色图",
            status="completed",
            position_x=0,
            position_y=0,
            prompt="红衣角色，电影光",
            input_json=json.dumps({"prompt": "红衣角色，电影光"}, ensure_ascii=False),
            output_json=json.dumps({
                "type": "image",
                "status": "completed",
                "local_url": "/api/media/project-1/generated_images/history-image.png",
            }, ensure_ascii=False),
        ))
        await session.commit()

        items = await routes_projects._list_project_media_history_items("project-1", session)
        filenames = {entry["filename"] for entry in items}
        assert "orphan-image.png" not in filenames
        assert "edit-preview-source-node.png" not in filenames
        item = next(entry for entry in items if entry["filename"] == "history-image.png")

        assert item["kind"] == "image"
        assert item["prompt"] == "红衣角色，电影光"
        assert item["source_node_id"] == "source-node"

        restored = await routes_projects.restore_project_media_history_item(
            "project-1",
            item["id"],
            routes_projects.ProjectMediaHistoryRestoreRequest(x=10, y=20),
            session,
        )
        restored_node = restored["node"]
        assert restored_node["type"] == "image"
        assert restored_node["status"] == "completed"
        assert restored_node["prompt"] == "红衣角色，电影光"

        deleted = await routes_projects.delete_project_media_history_item("project-1", item["id"], session)

    assert deleted["deleted"] is True
    assert not generated_path.exists()
    assert orphan_path.exists()
    assert edit_preview_path.exists()


@pytest.mark.asyncio
async def test_project_media_history_keeps_node_media_after_canvas_delete(monkeypatch, tmp_path) -> None:
    await _setup_asset_db(monkeypatch, tmp_path)
    generated_dir = tmp_path / "storage" / "project-1" / "generated_images"
    generated_dir.mkdir(parents=True, exist_ok=True)
    generated_path = generated_dir / "deleted-node-image.png"
    generated_path.write_bytes(
        base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")
    )

    async with db_session.session_scope() as session:
        session.add(WorkflowNode(
            id="delete-source-node",
            project_id="project-1",
            display_id=9,
            type="image",
            title="删除后可恢复",
            status="completed",
            position_x=0,
            position_y=0,
            prompt="蓝色场景",
            input_json=json.dumps({"prompt": "蓝色场景"}, ensure_ascii=False),
            output_json=json.dumps({
                "type": "image",
                "status": "completed",
                "local_url": "/api/media/project-1/generated_images/deleted-node-image.png",
            }, ensure_ascii=False),
        ))
        await session.commit()

    result = await canvas_tools.delete_canvas(
        project_id="project-1",
        scope="selected",
        node_ids=["9"],
    )

    assert result["ok"] is True
    assert result["deleted_files"] == []
    assert generated_path.exists()

    async with db_session.session_scope() as session:
        items = await routes_projects._list_project_media_history_items("project-1", session)
        item = next(entry for entry in items if entry["filename"] == "deleted-node-image.png")
        restored = await routes_projects.restore_project_media_history_item(
            "project-1",
            item["id"],
            routes_projects.ProjectMediaHistoryRestoreRequest(x=10, y=20),
            session,
        )

    assert restored["node"]["type"] == "image"
    assert restored["node"]["prompt"] == "蓝色场景"
