from app.agent import planner
from app.agent.prompts import core_rules, task_loop, working_loop


def test_planner_prompt_uses_generic_node_surface():
    text = planner.PLANNER_PROMPT

    assert "text, image, video, and audio" in text
    assert "selected_video_mode" not in text


def test_runtime_prompt_guides_generic_video_workflow():
    combined = "\n".join([working_loop.PROMPT, core_rules.PROMPT, task_loop.PROMPT])

    assert "text" in combined
    assert "image" in combined
    assert "video" in combined
    assert "existing workflow templates" in combined
    assert "Workflow Build Mode" not in combined
    assert "node" in combined
    assert "workflow_spec returns" not in combined
    assert "workflow.run" not in combined
    assert "blueprint.start_tree_draft" not in combined
    assert "blueprint.append_tree_node" not in combined
    assert "blueprint.finalize_tree_draft" not in combined
    assert "selected_mode" not in combined
