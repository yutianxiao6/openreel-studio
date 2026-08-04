from pathlib import Path

import pytest

from app.mcp_tools import config_tools, node_universal, workflow_tools
from app.mcp_tools.registry import registry


@pytest.mark.asyncio
async def test_agent_config_views_cannot_request_unmasked_secrets(monkeypatch, tmp_path: Path) -> None:
    class FakeStore:
        file_path = tmp_path / "runtime.jsonc"

        async def get_raw_text(self) -> str:
            return '{"llm_providers":[{"api_key":"sk-raw-secret"}]}'

        async def read(self, *, mask_secrets: bool = True):
            assert mask_secrets is True
            return {"llm_providers": [{"api_key": "***"}]}

        async def validate_text(self, content: str):
            assert "sk-raw-secret" in content
            return True, []

    monkeypatch.setattr(config_tools, "get_store", lambda: FakeStore())

    structured = await config_tools.config_read_for_agent()
    raw_page = await config_tools.config_read_file_for_agent()

    assert structured["llm_providers"][0]["api_key"] == "***"
    assert "sk-raw-secret" not in raw_page["raw_text_page"]["content"]
    assert raw_page["mask_secrets"] is True
    assert "mask_secrets" not in registry.get("config.read").schema.get("properties", {})
    assert "mask_secrets" not in registry.get("config.read_file").schema.get("properties", {})


@pytest.mark.asyncio
async def test_node_get_rejects_unbounded_id_batches_before_database_reads(monkeypatch) -> None:
    async def unexpected_get_node(node_id: str):
        raise AssertionError(f"database read should not occur for oversized batch: {node_id}")

    monkeypatch.setattr(node_universal.canvas_tools, "get_node", unexpected_get_node)

    result = await node_universal.node_get(
        project_id="project-1",
        node_ids=[str(index) for index in range(21)],
    )

    assert result["ok"] is False
    assert result["error_kind"] == "too_many_node_ids"
    assert result["max_node_ids"] == 20


def test_long_source_tools_share_resumable_document_policy() -> None:
    names = {
        "node.get",
        "skills.read",
        "file.read_text",
        "file.extract_text_from_upload",
        "file.workspace_read",
        "assets.read_asset",
        "config.read_file",
        "workflow.template.read",
        "workflow.spec.read",
    }

    for name in names:
        spec = registry.get(name)
        assert spec is not None
        assert spec.output_policy.profile == "document"
        assert spec.output_policy.max_model_tokens == 10_000
    for spec in registry.list_tools():
        assert 0 < spec.output_policy.default_model_tokens <= spec.output_policy.max_model_tokens
        assert spec.output_policy.max_model_tokens <= 10_000


def test_collection_and_delegated_tools_use_executable_output_profiles() -> None:
    collections = {
        "task.list",
        "memory.recall",
        "memory.recall_user",
        "events.tail",
        "events.query",
        "file.list_dir",
        "file.workspace_list",
        "file.workspace_search",
        "node.list",
        "skills.list",
        "assets.list_project",
        "assets.list_shared",
        "workflow.runtime_status",
        "workflow.canvas.inspect",
    }
    delegated = {
        "agent.run",
        "agent.review",
        "agent.map_reduce",
        "agent.pipeline",
        "agent.hierarchical",
        "tool.execute",
    }

    assert all(registry.get(name).output_policy.profile == "collection" for name in collections)
    assert all(registry.get(name).output_policy.profile == "delegated" for name in delegated)


def test_workflow_runtime_status_pages_steps_and_summarizes_stored_inputs() -> None:
    runtime = workflow_tools._workflow_runtime_status_step_page(
        {"steps": [{"id": str(index)} for index in range(120)], "input_values": {"script": "x"}},
        offset=50,
        limit=50,
    )
    summary = workflow_tools._workflow_input_value_summary(
        {"script": "剧" * 20_000, "segments": [{"id": index} for index in range(4)]}
    )

    assert [step["id"] for step in runtime["steps"][:2]] == ["50", "51"]
    assert runtime["steps_page"]["next_offset"] == 100
    assert summary["items"][0]["chars"] == 20_000
    assert len(summary["items"][0]["preview"]) == 240
    assert "剧" * 1_000 not in str(summary)
