from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.mcp_tools import canvas_tools
from app.services import media_generation, node_recovery


@pytest.mark.asyncio
async def test_recover_interrupted_video_polls_resumes_persisted_jobs(monkeypatch):
    now = datetime.utcnow()
    nodes = [
        SimpleNamespace(
            id="running-video",
            project_id="project-1",
            type="video",
            status="running",
            prompt="running prompt",
            input_json=json.dumps({"duration_seconds": 15}),
            output_json=json.dumps(
                {
                    "type": "video",
                    "status": "running",
                    "job_id": "job-running",
                    "provider_task_id": "provider-job-running",
                    "adapter_resume_request": {"kind": "video"},
                    "adapter_resume_supported": True,
                    "provider": "provider-a",
                }
            ),
            updated_at=now,
        ),
        SimpleNamespace(
            id="transient-video",
            project_id="project-1",
            type="video",
            status="failed",
            prompt="retry prompt",
            input_json="{}",
            output_json=json.dumps(
                {
                    "type": "video",
                    "status": "processing",
                    "job_id": "job-transient",
                    "provider_task_id": "provider-job-transient",
                    "adapter_resume_request": {"kind": "video"},
                    "adapter_resume_supported": True,
                    "provider": "provider-a",
                    "error_kind": "server_error",
                }
            ),
            updated_at=now,
        ),
        SimpleNamespace(
            id="terminal-video",
            project_id="project-1",
            type="video",
            status="failed",
            prompt="failed prompt",
            input_json="{}",
            output_json=json.dumps({
                "type": "video",
                "status": "failed",
                "job_id": "job-terminal",
                "provider": "provider-a",
                "error_kind": "provider_failed",
            }),
            updated_at=now,
        ),
    ]

    class Result:
        def all(self):
            return nodes

    class Session:
        async def exec(self, _stmt):
            return Result()

    @asynccontextmanager
    async def fake_session_scope():
        yield Session()

    resumed: list[str] = []

    async def fake_resume(**kwargs):
        resumed.append(kwargs["node_id"])
        return True

    monkeypatch.setattr(node_recovery, "session_scope", fake_session_scope)
    monkeypatch.setattr(media_generation, "resume_persisted_video_poll", fake_resume)

    result = await node_recovery.recover_interrupted_video_polls(project_id="project-1")

    assert result["resumed"] == 2
    assert resumed == ["running-video", "transient-video"]


@pytest.mark.asyncio
async def test_resume_persisted_video_poll_updates_node_without_resubmitting(monkeypatch):
    updates: list[dict] = []
    scheduled: dict = {}

    async def fake_update_node(node_id: str, patch: dict):
        updates.append({"node_id": node_id, **patch})
        return {"id": node_id, **patch}

    def fake_schedule(**kwargs):
        scheduled.update(kwargs)
        return True

    monkeypatch.setattr(canvas_tools, "update_node", fake_update_node)
    monkeypatch.setattr(media_generation, "_schedule_background_video_poll", fake_schedule)

    resumed = await media_generation.resume_persisted_video_poll(
        project_id="project-1",
        node_id="video-1",
        prompt="video prompt",
        input_data={"duration_seconds": 15, "aspect_ratio": "16:9"},
        output={
            "type": "video",
            "status": "processing",
            "job_id": "existing-job",
            "provider_task_id": "provider-existing-job",
            "adapter_resume_request": {"kind": "video"},
            "adapter_resume_supported": True,
            "provider": "provider-a",
            "model": "model-a",
            "error": "temporary 502",
            "error_kind": "server_error",
        },
    )

    assert resumed is True
    assert updates[0]["status"] == "running"
    assert updates[0]["output_data"]["job_id"] == "existing-job"
    assert "error" not in updates[0]["output_data"]
    assert scheduled["queued_result"]["job_id"] == "existing-job"
    assert scheduled["queued_result"]["recovered_after_restart"] is True


@pytest.mark.asyncio
async def test_background_video_poll_scheduler_deduplicates_same_job(monkeypatch):
    release = asyncio.Event()

    async def fake_background_poll(**_kwargs):
        await release.wait()

    monkeypatch.setattr(media_generation, "_background_video_poll", fake_background_poll)
    kwargs = {
        "project_id": "project-1",
        "node_id": "video-1",
        "queued_result": {"job_id": "job-1"},
    }

    try:
        assert media_generation._schedule_background_video_poll(**kwargs) is True
        assert media_generation._schedule_background_video_poll(**kwargs) is False
        assert len(media_generation._BACKGROUND_VIDEO_TASKS) == 1
    finally:
        release.set()
        await media_generation.stop_background_media_tasks()
