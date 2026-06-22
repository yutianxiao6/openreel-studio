from app.agent import planner
from app.agent.prompts import flow_paths, task_loop


def test_planner_prompt_uses_generic_node_surface():
    text = planner.PLANNER_PROMPT

    assert "text, image, video, and audio" in text
    assert "segment_video_prompt" not in text
    assert "episode_script" not in text
    assert "selected_video_mode" not in text


def test_runtime_prompt_guides_generic_video_workflow():
    combined = "\n".join([flow_paths.PROMPT, task_loop.PROMPT])

    assert "text" in combined
    assert "image" in combined
    assert "video" in combined
    assert "skill.search" in combined
    assert "skill.get" in combined
    assert "node" in combined
    assert "blueprint.start_tree_draft" not in combined
    assert "blueprint.append_tree_node" not in combined
    assert "blueprint.finalize_tree_draft" not in combined
    assert "segment_video_clip" not in combined
    assert "selected_mode" not in combined
