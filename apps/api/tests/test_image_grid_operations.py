from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import session as db_session
from app.db.models import Project
from app.mcp_tools import canvas_tools, node_universal
from app.mcp_tools.registry import registry
from app.services import image_operations


async def _setup_db(monkeypatch, tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'image-grid.db'}"
    engine = create_async_engine(database_url, echo=False, future=True, connect_args={"timeout": 30})
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "AsyncSessionLocal", session_local)
    monkeypatch.setattr(image_operations.settings, "STORAGE_PATH", str(tmp_path / "storage"))
    monkeypatch.setattr(image_operations.settings, "STORAGE_DIR", str(tmp_path / "storage"))
    await db_session.init_db()
    async with db_session.session_scope() as session:
        session.add(Project(id="project-grid", title="Grid Test", state_json="{}"))
        await session.commit()


def _write_source(tmp_path: Path) -> None:
    root = tmp_path / "storage" / "project-grid" / "generated_images"
    root.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (120, 80), (255, 0, 0, 255))
    image.save(root / "source.png")
    replacement = Image.new("RGBA", (60, 40), (0, 255, 0, 255))
    replacement.save(root / "replacement.png")


@pytest.mark.asyncio
async def test_grid_split_keeps_cells_inside_current_node_until_extract(monkeypatch, tmp_path) -> None:
    await _setup_db(monkeypatch, tmp_path)
    _write_source(tmp_path)

    node = await canvas_tools.create_node(
        project_id="project-grid",
        node_type="image",
        title="分镜图",
        input_data={"title": "分镜图"},
        model_config={"surface": "draft_canvas"},
    )
    await canvas_tools.update_node(
        node["id"],
        {
            "status": "completed",
            "output_data": {"type": "image", "local_url": "/api/media/project-grid/source.png"},
        },
    )

    before = await canvas_tools.list_nodes("project-grid")
    output = await image_operations.split_grid_node("project-grid", node["id"], 2, 2)
    after_split = await canvas_tools.list_nodes("project-grid")

    assert output["type"] == "image_grid"
    assert output["grid"] == {"rows": 2, "cols": 2}
    assert len(output["cells"]) == 4
    assert len(after_split) == len(before)
    assert all(cell["title"].startswith("分镜图的第") for cell in output["cells"])

    extracted = await image_operations.extract_grid_cell_node(
        "project-grid",
        node["id"],
        output["cells"][1]["cell_id"],
    )
    after_extract = await canvas_tools.list_nodes("project-grid")

    assert extracted["ok"] is True
    assert extracted["node"]["title"] == "分镜图的第2图片"
    assert len(after_extract) == len(before) + 1


@pytest.mark.asyncio
async def test_extract_grid_cell_can_remove_cell_from_current_grid(monkeypatch, tmp_path) -> None:
    await _setup_db(monkeypatch, tmp_path)
    _write_source(tmp_path)

    node = await canvas_tools.create_node(
        project_id="project-grid",
        node_type="image",
        title="分镜图",
        input_data={"title": "分镜图"},
        model_config={"surface": "draft_canvas"},
    )
    await canvas_tools.update_node(
        node["id"],
        {
            "status": "completed",
            "output_data": {"type": "image", "local_url": "/api/media/project-grid/source.png"},
        },
    )
    output = await image_operations.split_grid_node("project-grid", node["id"], 2, 2)
    cell_id = output["cells"][0]["cell_id"]

    extracted = await image_operations.extract_grid_cell_node(
        "project-grid",
        node["id"],
        cell_id,
        remove_from_grid=True,
    )
    grid_node = await canvas_tools.get_node(node["id"])
    emptied = next(cell for cell in grid_node["output"]["cells"] if cell["cell_id"] == cell_id)

    assert extracted["ok"] is True
    assert emptied["empty"] is True
    assert "local_url" not in emptied


@pytest.mark.asyncio
async def test_place_grid_cell_fills_cell_and_can_remove_source_node(monkeypatch, tmp_path) -> None:
    await _setup_db(monkeypatch, tmp_path)
    _write_source(tmp_path)

    grid_node = await canvas_tools.create_node(
        project_id="project-grid",
        node_type="image",
        title="分镜图",
        input_data={"title": "分镜图"},
        model_config={"surface": "draft_canvas"},
    )
    await canvas_tools.update_node(
        grid_node["id"],
        {
            "status": "completed",
            "output_data": {"type": "image", "local_url": "/api/media/project-grid/source.png"},
        },
    )
    grid_output = await image_operations.split_grid_node("project-grid", grid_node["id"], 2, 2)
    cell_id = grid_output["cells"][0]["cell_id"]
    await image_operations.extract_grid_cell_node(
        "project-grid",
        grid_node["id"],
        cell_id,
        remove_from_grid=True,
    )
    source_node = await canvas_tools.create_node(
        project_id="project-grid",
        node_type="image",
        title="替换图",
        input_data={"title": "替换图"},
        model_config={"surface": "draft_canvas"},
    )
    await canvas_tools.update_node(
        source_node["id"],
        {
            "status": "completed",
            "output_data": {"type": "image", "local_url": "/api/media/project-grid/replacement.png"},
        },
    )

    placed = await image_operations.place_grid_cell_node(
        "project-grid",
        grid_node["id"],
        cell_id,
        f"node:{source_node['id']}",
        remove_source_node=True,
    )
    nodes = await canvas_tools.list_nodes("project-grid")
    grid_after = await canvas_tools.get_node(grid_node["id"])
    filled = next(cell for cell in grid_after["output"]["cells"] if cell["cell_id"] == cell_id)

    assert placed["ok"] is True
    assert filled["empty"] is False
    assert filled["local_url"]
    assert source_node["id"] not in {node["id"] for node in nodes}


@pytest.mark.asyncio
async def test_image_node_run_can_dispatch_grid_split_operation(monkeypatch, tmp_path) -> None:
    await _setup_db(monkeypatch, tmp_path)
    _write_source(tmp_path)

    node = await canvas_tools.create_node(
        project_id="project-grid",
        node_type="image",
        title="分镜图",
        input_data={
            "title": "分镜图",
            "operation": "grid_split",
            "grid": {"rows": 2, "cols": 2},
        },
        model_config={"surface": "draft_canvas"},
    )
    await canvas_tools.update_node(
        node["id"],
        {
            "status": "completed",
            "output_data": {"type": "image", "local_url": "/api/media/project-grid/source.png"},
        },
    )

    result = await node_universal.node_run("project-grid", node["id"])

    assert result["ok"] is True
    assert result["action"] == "grid_split"
    assert result["result"]["type"] == "image_grid"
    assert len(result["result"]["cells"]) == 4


def test_image_operation_tools_are_hidden_from_agent() -> None:
    assert registry.tool_exposure("image.grid_split") == "hidden"
    assert registry.tool_exposure("image.grid_combine") == "hidden"
    assert registry.tool_exposure("image.extract_grid_cell") == "hidden"
    assert registry.tool_exposure("image.place_grid_cell") == "hidden"
    assert registry.tool_exposure("image.inpaint_region") == "hidden"
    assert "image.grid_split" not in registry.agent_visible_tool_names()
    assert "image.place_grid_cell" not in registry.agent_visible_tool_names()
