from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.mcp_tools import canvas_tools
from app.services import media_generation, media_provider, node_recovery


@pytest.mark.asyncio
async def test_video_http_poll_retries_502_and_reads_nested_protocol_result(monkeypatch):
    protocol = {
        "version": "openreel.video_provider.v1",
        "id": "test_video",
        "display_name": "Test Video",
        "poll": {
            "method": "GET",
            "path": "/videos/tasks/{task_id}",
            "status_path": "provider_response.status",
            "progress_path": "provider_response.progress",
            "succeeded": ["completed"],
            "failed": ["failed"],
            "running": ["processing"],
            "interval_seconds": 1,
            "max_retry_interval_seconds": 4,
            "timeout_seconds": 30,
        },
        "result": {
            "video_url_path": "provider_response.result.videos.0.video_url",
        },
    }
    provider = SimpleNamespace(
        name="test-video",
        model_name="video-model",
        base_url="https://video.example/v1",
        api_key="secret",
        params_json="{}",
    )
    responses = [
        (502, {"error": {"code": "UPSTREAM_UNREACHABLE", "message": "fetch failed"}}),
        (200, {
            "provider_response": {
                "status": "completed",
                "progress": 100,
                "result": {"videos": [{"video_url": "https://cdn.example/video.mp4"}]},
            },
        }),
    ]
    progress: list[dict] = []

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict):
            self.status_code = status_code
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, endpoint: str, headers: dict):
            assert endpoint == "https://video.example/v1/videos/tasks/job-1"
            status_code, payload = responses.pop(0)
            return FakeResponse(status_code, payload)

    async def no_sleep(_seconds: float) -> None:
        return None

    async def capture(update: dict) -> None:
        progress.append(update)

    monkeypatch.setattr(media_provider, "_video_http_v1_protocol", lambda *_args: (protocol, None))
    monkeypatch.setattr(media_provider.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(media_provider.asyncio, "sleep", no_sleep)

    result = await media_provider._poll_video_http_v1_task(
        provider=provider,
        project_id="project-1",
        task_id="job-1",
        extra_override={},
        save_locally=False,
        progress_callback=capture,
    )

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["remote_url"] == "https://cdn.example/video.mp4"
    assert result["polls"][0]["http_code"] == 502
    assert result["polls"][0]["retrying"] is True
    assert result["polls"][0]["retry_count"] == 1
    assert progress[0]["retry_in_seconds"] == 1


def test_video_protocol_declares_terminal_error_paths():
    protocol = {
        "poll": {"status_path": "provider_response.status", "failed": ["failed"]},
        "error": {
            "message_path": "provider_response.error",
            "code_path": "provider_response.error_code",
        },
    }
    response = {
        "provider_response": {
            "status": "failed",
            "error": {"message": "content rejected"},
            "error_code": "CONTENT_REJECTED",
        },
    }

    message, code = media_provider._video_http_v1_provider_error(protocol, response)

    assert media_provider._video_http_v1_status(protocol, response) == "failed"
    assert message == "content rejected"
    assert code == "CONTENT_REJECTED"


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
            output_json=json.dumps({
                "type": "video",
                "status": "running",
                "job_id": "job-running",
                "provider": "provider-a",
            }),
            updated_at=now,
        ),
        SimpleNamespace(
            id="transient-video",
            project_id="project-1",
            type="video",
            status="failed",
            prompt="retry prompt",
            input_json="{}",
            output_json=json.dumps({
                "type": "video",
                "status": "processing",
                "job_id": "job-transient",
                "provider": "provider-a",
                "error_kind": "server_error",
            }),
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
