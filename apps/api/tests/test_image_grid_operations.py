from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import session as db_session
from app.db.models import Project
from app.mcp_tools import canvas_tools, node_universal, tool_meta_tools
from app.mcp_tools.registry import registry
from app.services import image_operations


def test_storage_root_resolves_relative_path_against_project_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(image_operations.settings, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(image_operations.settings, "STORAGE_PATH", "./storage")

    assert image_operations._storage_root() == (tmp_path / "storage").resolve()


@pytest.mark.asyncio
async def test_resolve_image_path_maps_absolute_local_media_url(monkeypatch, tmp_path) -> None:
    await _setup_db(monkeypatch, tmp_path)
    _write_source(tmp_path)

    path = await image_operations.resolve_image_path(
        "project-grid",
        "https://yutianxiaoliu.top/studio/api/media/project-grid/source.png",
    )

    assert path == (tmp_path / "storage" / "project-grid" / "generated_images" / "source.png").resolve()


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
    assert registry.tool_exposure("image.edit") == "hidden"
    assert registry.tool_exposure("image.segment") == "hidden"
    assert registry.tool_exposure("image.grid_split") == "hidden"
    assert registry.tool_exposure("image.grid_combine") == "hidden"
    assert registry.tool_exposure("image.extract_grid_cell") == "hidden"
    assert registry.tool_exposure("image.place_grid_cell") == "hidden"
    assert registry.tool_exposure("image.inpaint_region") == "hidden"
    assert "image.edit" not in registry.agent_visible_tool_names()
    assert "image.segment" not in registry.agent_visible_tool_names()
    assert "image.grid_split" not in registry.agent_visible_tool_names()
    assert "image.place_grid_cell" not in registry.agent_visible_tool_names()


def test_mask_operation_can_isolate_rounded_icon_shape() -> None:
    image = Image.new("RGBA", (40, 40), (10, 80, 220, 255))

    edited = image_operations.apply_image_edit_operations(
        image,
        [
            {
                "type": "mask",
                "mode": "shape",
                "shape": "rounded_rect",
                "effect": "keep",
                "unit": "pixel",
                "rect": {"x": 0, "y": 0, "width": 40, "height": 40},
                "radius": 12,
            }
        ],
    )

    assert edited.getpixel((0, 0))[3] == 0
    assert edited.getpixel((39, 0))[3] == 0
    assert edited.getpixel((20, 20))[3] == 255


def test_segment_image_removes_flat_edge_background() -> None:
    image = Image.new("RGBA", (80, 80), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((22, 16, 58, 64), radius=10, fill=(30, 80, 220, 255))

    result = image_operations.segment_image(
        image,
        method="auto",
        background_tolerance=4,
        feather=0,
        smooth=0,
    )
    cutout = result["image"]

    assert result["engine"] == "background_flood"
    assert result["bbox"] == (22, 16, 59, 65)
    assert cutout.getpixel((0, 0))[3] == 0
    assert cutout.getpixel((40, 40))[3] == 255


@pytest.mark.asyncio
async def test_image_segment_node_returns_cutout_mask_and_bbox(monkeypatch, tmp_path) -> None:
    await _setup_db(monkeypatch, tmp_path)
    _write_source(tmp_path)
    root = tmp_path / "storage" / "project-grid" / "generated_images"
    source = Image.new("RGBA", (96, 64), (255, 255, 255, 255))
    draw = ImageDraw.Draw(source)
    draw.rectangle((28, 12, 68, 52), fill=(20, 120, 220, 255))
    source.save(root / "segment-source.png")

    node = await canvas_tools.create_node(
        project_id="project-grid",
        node_type="image",
        title="待抠图",
        input_data={"title": "待抠图"},
        model_config={"surface": "draft_canvas"},
    )
    await canvas_tools.update_node(
        node["id"],
        {
            "status": "completed",
            "output_data": {"type": "image", "local_url": "/api/media/project-grid/segment-source.png"},
        },
    )

    result = await image_operations.segment_image_node(
        "project-grid",
        "#0",
        method="auto",
        background_tolerance=4,
        feather=0,
        smooth=0,
    )

    assert result["ok"] is True
    assert result["action"] == "segment"
    assert result["node_id"] == "0"
    assert result["cutout_ref"].startswith("/api/media/project-grid/image_ops/segment-cutout")
    assert result["mask_ref"].startswith("/api/media/project-grid/image_ops/segment-mask")
    assert result["bbox"] == {"x": 28, "y": 12, "width": 41, "height": 41, "left": 28, "top": 12, "right": 69, "bottom": 53}
    cutout = Image.open(result["cutout"]["local_path"]).convert("RGBA")
    assert cutout.getpixel((0, 0))[3] == 0
    assert cutout.getpixel((48, 32))[3] == 255


def test_mask_operation_can_clear_edge_background() -> None:
    image = Image.new("RGBA", (60, 40), (255, 255, 255, 255))
    for x in range(20, 40):
        for y in range(10, 30):
            image.putpixel((x, y), (20, 20, 20, 255))

    edited = image_operations.apply_image_edit_operations(
        image,
        [
            {
                "type": "mask",
                "mode": "background",
                "effect": "transparent",
                "tolerance": 8,
                "seed": "edges",
                "seed_step": 1,
            }
        ],
    )

    assert edited.getpixel((0, 0))[3] == 0
    assert edited.getpixel((59, 39))[3] == 0
    assert edited.getpixel((30, 20))[3] == 255


def test_mask_operation_can_clear_color_range() -> None:
    image = Image.new("RGBA", (20, 20), (255, 0, 0, 255))
    for x in range(6, 14):
        for y in range(6, 14):
            image.putpixel((x, y), (0, 255, 0, 255))

    edited = image_operations.apply_image_edit_operations(
        image,
        [
            {
                "type": "mask",
                "mode": "color",
                "effect": "transparent",
                "color": "#ff0000",
                "tolerance": 0,
            }
        ],
    )

    assert edited.getpixel((0, 0))[3] == 0
    assert edited.getpixel((10, 10))[3] == 255


def test_transparent_mask_fades_visible_pixels() -> None:
    image = Image.new("RGBA", (20, 20), (220, 120, 60, 255))

    edited = image_operations.apply_image_edit_operations(
        image,
        [
            {
                "type": "mask",
                "mode": "shape",
                "shape": "rect",
                "effect": "transparent",
                "unit": "pixel",
                "rect": {"x": 0, "y": 0, "width": 10, "height": 20},
                "alpha": 0.16,
            }
        ],
    )

    changed = edited.getpixel((5, 10))
    untouched = edited.getpixel((15, 10))
    assert changed[3] == 41
    assert changed[0] < 220
    assert changed[1] < 120
    assert untouched == (220, 120, 60, 255)


def test_wireframe_fill_draws_image_driven_surface_lines() -> None:
    image = Image.new("RGBA", (80, 80), (28, 34, 42, 255))
    draw = ImageDraw.Draw(image)
    draw.ellipse((18, 10, 62, 70), fill=(180, 150, 120, 255))
    draw.ellipse((30, 28, 37, 34), fill=(40, 45, 55, 255))
    draw.ellipse((43, 28, 50, 34), fill=(40, 45, 55, 255))
    draw.arc((30, 36, 50, 56), 20, 160, fill=(70, 50, 45, 255), width=2)

    edited = image_operations.apply_image_edit_operations(
        image,
        [
            {
                "type": "fill",
                "unit": "pixel",
                "shape": "ellipse",
                "rect": {"x": 18, "y": 10, "width": 44, "height": 60},
                "style": {
                    "type": "wireframe",
                    "color": "#22d3ee",
                    "opacity": 0.9,
                    "spacing": 14,
                    "line_width": 1,
                    "strength": 0.8,
                },
            }
        ],
    )

    changed_inside = sum(
        1
        for y in range(10, 70)
        for x in range(18, 62)
        if edited.getpixel((x, y)) != image.getpixel((x, y))
    )
    assert changed_inside > 300
    assert edited.getpixel((2, 2)) == image.getpixel((2, 2))


def test_opencv_curve_image_generates_dense_full_image_curves() -> None:
    image = Image.new("RGBA", (120, 90), (24, 28, 36, 255))
    draw = ImageDraw.Draw(image)
    draw.ellipse((18, 8, 86, 76), fill=(190, 145, 110, 255))
    draw.rectangle((72, 18, 112, 84), fill=(70, 115, 170, 255))
    draw.arc((32, 36, 72, 62), 10, 170, fill=(40, 38, 38, 255), width=3)

    curve = image_operations._opencv_curve_image(
        image,
        color="#22d3ee",
        detail=0.85,
        line_strength=0.95,
        base_visibility=0.1,
    )

    assert curve.size == image.size
    changed = sum(
        1
        for y in range(curve.height)
        for x in range(curve.width)
        if curve.getpixel((x, y)) != image.getpixel((x, y))
    )
    cyan_pixels = sum(
        1
        for y in range(curve.height)
        for x in range(curve.width)
        if curve.getpixel((x, y))[1] > curve.getpixel((x, y))[0] + 20
        and curve.getpixel((x, y))[2] > curve.getpixel((x, y))[0] + 20
    )
    assert changed > 9000
    assert cyan_pixels > 200


@pytest.mark.asyncio
async def test_curve_preview_returns_candidate_without_new_agent_tool(monkeypatch, tmp_path) -> None:
    await _setup_db(monkeypatch, tmp_path)
    _write_source(tmp_path)

    node = await canvas_tools.create_node(
        project_id="project-grid",
        node_type="image",
        title="待曲线化图",
        input_data={"title": "待曲线化图"},
        model_config={"surface": "draft_canvas"},
    )
    await canvas_tools.update_node(
        node["id"],
        {
            "status": "completed",
            "output_data": {"type": "image", "local_url": "/api/media/project-grid/source.png"},
        },
    )

    result = await image_operations.preview_curve_image_node("project-grid", node["id"])

    assert result["ok"] is True
    assert result["action"] == "curve_preview"
    assert result["candidate_ref"].startswith("/api/media/project-grid/image_ops/curve-preview")
    assert result["curve"]["engine"] == "opencv"
    assert "image.curve" not in registry.registered_tool_names()


@pytest.mark.asyncio
async def test_image_edit_preview_returns_candidate_without_mutating_node(monkeypatch, tmp_path) -> None:
    await _setup_db(monkeypatch, tmp_path)
    _write_source(tmp_path)

    node = await canvas_tools.create_node(
        project_id="project-grid",
        node_type="image",
        title="待编辑图",
        input_data={"title": "待编辑图"},
        model_config={"surface": "draft_canvas"},
    )
    await canvas_tools.update_node(
        node["id"],
        {
            "status": "completed",
            "output_data": {"type": "image", "local_url": "/api/media/project-grid/source.png"},
        },
    )

    result = await image_operations.edit_image_node(
        "project-grid",
        node["id"],
        [{"type": "crop", "unit": "pixel", "rect": {"x": 0, "y": 0, "width": 60, "height": 40}}],
        action="preview",
    )
    current = await canvas_tools.get_node(node["id"])

    assert result["ok"] is True
    assert result["action"] == "preview"
    assert result["candidate_ref"].startswith("/api/media/project-grid/image_ops/edit-preview")
    assert result["image"]["width"] == 60
    assert result["image"]["height"] == 40
    assert result["_model_content_type"] == "image_edit_result"
    assert result["_model_content_refs"] == [result["candidate_ref"]]
    assert result["_model_content"][0]["type"] == "text"
    assert result["_model_content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert current["output"]["local_url"] == "/api/media/project-grid/source.png"


@pytest.mark.asyncio
async def test_image_edit_source_ref_accepts_public_node_id(monkeypatch, tmp_path) -> None:
    await _setup_db(monkeypatch, tmp_path)
    _write_source(tmp_path)

    node = await canvas_tools.create_node(
        project_id="project-grid",
        node_type="image",
        title="待编辑图",
        input_data={"title": "待编辑图"},
        model_config={"surface": "draft_canvas"},
    )
    await canvas_tools.update_node(
        node["id"],
        {
            "status": "completed",
            "output_data": {"type": "image", "local_url": "/api/media/project-grid/source.png"},
        },
    )

    result = await image_operations.edit_image_node(
        "project-grid",
        "0",
        [{"type": "crop", "unit": "pixel", "rect": {"x": 0, "y": 0, "width": 60, "height": 40}}],
        action="preview",
        source_ref="0",
    )

    assert result["ok"] is True
    assert result["action"] == "preview"
    assert result["node_id"] == "0"
    assert result["source_ref"] == "0"
    assert result["image"]["width"] == 60
    assert result["image"]["height"] == 40


@pytest.mark.asyncio
async def test_image_edit_commit_updates_node_and_archives_previous_output(monkeypatch, tmp_path) -> None:
    await _setup_db(monkeypatch, tmp_path)
    _write_source(tmp_path)

    node = await canvas_tools.create_node(
        project_id="project-grid",
        node_type="image",
        title="待编辑图",
        input_data={"title": "待编辑图"},
        model_config={"surface": "draft_canvas"},
    )
    await canvas_tools.update_node(
        node["id"],
        {
            "status": "completed",
            "output_data": {"type": "image", "local_url": "/api/media/project-grid/source.png", "width": 120, "height": 80},
        },
    )
    preview = await image_operations.edit_image_node(
        "project-grid",
        "0",
        [{"type": "fill", "unit": "normalized", "shape": "rect", "rect": {"x": 0, "y": 0, "width": 0.5, "height": 0.5}, "style": {"type": "solid", "color": "#0000ff", "opacity": 1}}],
        action="preview",
    )
    preview_path = Path(preview["image"]["local_path"])
    assert preview_path.exists()
    curve_preview = await image_operations.preview_curve_image_node(
        "project-grid",
        "0",
        source_ref=preview["candidate_ref"],
    )
    curve_preview_path = Path(curve_preview["image"]["local_path"])
    assert curve_preview_path.exists()

    committed = await image_operations.edit_image_node(
        "project-grid",
        "#0",
        action="commit",
        candidate_ref=curve_preview["candidate_ref"],
    )
    updated = await canvas_tools.get_node(node["id"])
    final_path = Path(committed["image"]["local_path"])

    assert committed["ok"] is True
    assert committed["action"] == "commit"
    assert committed["node_id"] == "0"
    assert updated["output"]["operation"] == "image_edit"
    assert updated["output"]["local_url"].startswith("/api/media/project-grid/image_ops/edit-final")
    assert updated["output"]["history"][0]["output"]["local_url"] == "/api/media/project-grid/source.png"
    assert final_path.exists()
    assert not preview_path.exists()
    assert not curve_preview_path.exists()
    assert str(preview_path) in committed["cleaned_temp_files"]
    assert str(curve_preview_path) in committed["cleaned_temp_files"]


@pytest.mark.asyncio
async def test_image_edit_cleanup_removes_panel_preview_without_commit(monkeypatch, tmp_path) -> None:
    await _setup_db(monkeypatch, tmp_path)
    _write_source(tmp_path)

    node = await canvas_tools.create_node(
        project_id="project-grid",
        node_type="image",
        title="待编辑图",
        input_data={"title": "待编辑图"},
        model_config={"surface": "draft_canvas"},
    )
    await canvas_tools.update_node(
        node["id"],
        {
            "status": "completed",
            "output_data": {"type": "image", "local_url": "/api/media/project-grid/source.png", "width": 120, "height": 80},
        },
    )
    preview = await image_operations.preview_curve_image_node("project-grid", "0")
    preview_path = Path(preview["image"]["local_path"])
    assert preview_path.exists()

    cleanup = await image_operations.cleanup_image_edit_temps("project-grid", "0")

    assert cleanup["ok"] is True
    assert str(preview_path) in cleanup["deleted_temp_files"]
    assert not preview_path.exists()


@pytest.mark.asyncio
async def test_image_edit_is_hidden_behind_image_editor_subagent() -> None:
    assert registry.tool_exposure("image.edit") == "hidden"

    search = await tool_meta_tools.tool_search(query="裁剪 涂鸦 图片编辑", category="image")
    cutout_search = await tool_meta_tools.tool_search(query="抠图 透明背景 图标", category="image")
    names = {item["name"] for item in search["tools"]}
    cutout_names = {item["name"] for item in cutout_search["tools"]}
    described = await tool_meta_tools.tool_describe(["image.edit", "image.segment", "agent.run"])

    assert names == {"agent.run"}
    assert cutout_names == {"agent.run"}
    assert described["not_found"] == ["image.edit (hidden)", "image.segment (hidden)"]
    assert [tool["name"] for tool in described["tools"]] == ["agent.run"]
