from app.services.workflow_runtime_recovery import recover_interrupted_workflow_runtime_state


def test_interrupted_run_all_becomes_resumable_without_losing_completed_steps() -> None:
    state = {
        "workflow_runtime": {
            "instances": {
                "wf_1": {
                    "status": "pause_requested",
                    "pause_requested": True,
                    "run_all_active": True,
                    "steps": {
                        "script": {"status": "completed", "output": {"content": "script"}},
                        "production_plan": {"status": "running", "last_started_at": "old"},
                        "storyboard": {"status": "idle"},
                    },
                }
            }
        }
    }

    recovered_state, count = recover_interrupted_workflow_runtime_state(
        state,
        now="2026-07-15T14:00:00+00:00",
    )

    assert count == 1
    instance = recovered_state["workflow_runtime"]["instances"]["wf_1"]
    assert instance["status"] == "paused"
    assert instance["pause_requested"] is False
    assert "run_all_active" not in instance
    assert instance["pause_reason"] == "api_process_interrupted"
    assert instance["steps"]["script"] == {
        "status": "completed",
        "output": {"content": "script"},
    }
    assert instance["steps"]["production_plan"]["status"] == "idle"
    assert "last_started_at" not in instance["steps"]["production_plan"]
    assert instance["steps"]["storyboard"]["status"] == "idle"
    assert state["workflow_runtime"]["instances"]["wf_1"]["run_all_active"] is True


def test_recovery_leaves_inactive_runtime_unchanged() -> None:
    state = {
        "workflow_runtime": {
            "instances": {
                "wf_1": {
                    "status": "paused",
                    "steps": {"production_plan": {"status": "idle"}},
                }
            }
        }
    }

    recovered_state, count = recover_interrupted_workflow_runtime_state(
        state,
        now="2026-07-15T14:00:00+00:00",
    )

    assert count == 0
    assert recovered_state is state
