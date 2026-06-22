import asyncio

import pytest

from app.agent.run_broker import ProjectRunBroker


@pytest.mark.asyncio
async def test_run_broker_keeps_subscription_open_after_error_until_done() -> None:
    async def source():
        yield {"type": "error", "message": "provider timeout"}
        yield {"type": "done", "status": "failed"}

    broker = ProjectRunBroker()
    run = await broker.start("project-error-done", source)

    events = []
    async for event in run.subscribe(replay=True):
        events.append(event)

    assert [event["type"] for event in events] == ["error", "done"]
    assert events[-1]["status"] == "failed"
    assert run.done


@pytest.mark.asyncio
async def test_run_broker_source_exception_publishes_error_and_done() -> None:
    async def source():
        raise RuntimeError("source crashed")
        yield {"type": "unreachable"}

    broker = ProjectRunBroker()
    run = await broker.start("project-source-exception", source)

    events = []
    async for event in run.subscribe(replay=True):
        events.append(event)

    assert [event["type"] for event in events] == ["error", "done"]
    assert "source crashed" in events[0]["message"]
    assert events[-1]["status"] == "failed"
    assert run.done


@pytest.mark.asyncio
async def test_run_broker_cancel_stops_active_source() -> None:
    started = asyncio.Event()

    async def source():
        started.set()
        yield {"type": "agent_round", "round": 1, "content": "running", "tools": []}
        await asyncio.sleep(60)
        yield {"type": "done", "status": "completed"}

    broker = ProjectRunBroker()
    run = await broker.start("project-cancel", source)
    await started.wait()

    result = await broker.cancel("project-cancel", "用户点击停止")
    assert result["cancelled"] is True

    events = []
    async for event in run.subscribe(replay=True):
        events.append(event)

    assert [event["type"] for event in events] == ["agent_round", "cancelled"]
    assert "用户点击停止" in events[-1]["message"]
    assert run.done
