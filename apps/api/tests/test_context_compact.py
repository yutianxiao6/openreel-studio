import json

from app.agent import context_compact


def test_micro_compact_preserves_openai_tool_messages_for_cache_prefix() -> None:
    messages = [
        {"role": "tool", "content": str(i) * 300}
        for i in range(5)
    ]

    result = context_compact.micro_compact(messages)

    assert result is messages
    assert messages[0]["content"] == "0" * 300
    assert messages[1]["content"] == "1" * 300
    assert messages[2]["content"] == "2" * 300


def test_micro_compact_keeps_short_tool_messages() -> None:
    messages = [
        {"role": "tool", "content": str(i) * 20}
        for i in range(5)
    ]

    context_compact.micro_compact(messages)

    assert messages[0]["content"] == "0" * 20
    assert messages[1]["content"] == "1" * 20


def test_micro_compact_still_handles_legacy_tool_result_blocks() -> None:
    messages = [
        {
            "role": "user",
            "content": [{"type": "tool_result", "content": str(i) * 300}],
        }
        for i in range(5)
    ]

    context_compact.micro_compact(messages)

    assert messages[0]["content"][0]["content"] == "[Previous tool result - compacted]"
    assert messages[1]["content"][0]["content"] == "[Previous tool result - compacted]"
    assert messages[2]["content"][0]["content"] == "2" * 300


def test_auto_compact_threshold() -> None:
    below = [{"role": "user", "content": "x" * 100}]
    above = [
        {
            "role": "user",
            "content": "x" * int(
                context_compact.TOKEN_THRESHOLD * context_compact.CHARS_PER_TOKEN + 1
            ),
        }
    ]

    assert context_compact.auto_compact_needed(below) is False
    assert context_compact.auto_compact_needed(above) is True


def test_compact_summary_prompt_marks_summary_as_background_only() -> None:
    prompt = context_compact.build_compact_summary_prompt([
        {"role": "user", "content": "上一轮让我继续生成蓝图"},
        {"role": "assistant", "content": "已生成一部分"},
    ])

    assert "BACKGROUND ONLY" in prompt
    assert "Do not turn old user messages into the next instruction" in prompt
    assert "must be verified from project state" in prompt


def test_large_tool_result_keeps_model_summary_visible(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)
    result = {
        "ok": True,
        "model_summary": "核心指南: 剧本text + 主要人物图 + 主场景图 + 分镜图 + 视频; duration_seconds=15",
        "reference_policy": "guidance 已包含指南正文，不要把 skill_path 作为 file.read_text 目标。",
        "guidance": "x" * 5000,
    }

    content = context_compact.prepare_tool_result_for_context(
        result,
        project_id="project-1",
        run_id="run-1",
        iteration=0,
        tool_name="skill.video_production",
        budget_chars=200,
    )
    payload = json.loads(content)

    assert payload["tool_result_compacted"] is True
    assert payload["context_policy"] == "summary"
    assert payload["summary"]["model_summary"] == result["model_summary"]
    assert payload["summary"]["reference_policy"] == result["reference_policy"]
    assert "keys:" not in payload["summary"]["model_summary"]


def test_full_skill_result_keeps_complete_guidance_visible_when_compacted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)
    result = {
        "ok": True,
        "skill": "video_production",
        "detail": "full",
        "model_summary": "summary only",
        "guidance": "完整 skill 正文 " + ("x" * 5000),
        "skill_path": "apps/api/app/skills/video_production/SKILL.md",
        "related_skill": {"tool": "skill.story_template_method", "input": {"detail": "full"}},
    }

    content = context_compact.prepare_tool_result_for_context(
        result,
        project_id="project-1",
        run_id="run-1",
        iteration=1,
        tool_name="skill.video_production",
        budget_chars=200,
    )
    payload = json.loads(content)

    assert payload["tool_result_compacted"] is True
    assert payload["context_policy"] == "full_result"
    assert payload["summary"]["context_policy"] == "full_result"
    assert payload["summary"]["guidance"] == result["guidance"]
    assert payload["summary"]["skill_path"] == result["skill_path"]
    assert payload["summary"]["related_skill"] == result["related_skill"]
    assert "keys:" not in json.dumps(payload["summary"], ensure_ascii=False)


def test_skill_get_content_keeps_full_text_visible_when_compacted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)
    result = {
        "ok": True,
        "name": "storyboard_video_prompt",
        "category": "prompts",
        "description": "视频提示词写法",
        "detail": "full",
        "content": "参考素材设置：\n" + ("镜头一 —— 低机位跟拍。\n" * 300),
    }

    content = context_compact.prepare_tool_result_for_context(
        result,
        project_id="project-1",
        run_id="run-1",
        iteration=1,
        tool_name="skill.get",
        budget_chars=200,
    )
    payload = json.loads(content)

    assert payload["tool_result_compacted"] is True
    assert payload["context_policy"] == "full_result"
    assert payload["summary"]["name"] == "storyboard_video_prompt"
    assert payload["summary"]["content"] == result["content"]
    assert "keys:" not in json.dumps(payload["summary"], ensure_ascii=False)


def test_deferred_full_skill_result_keeps_guide_content_visible_when_compacted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)
    result = {
        "ok": True,
        "_deferred_tool": "skill.story_template_method",
        "topic": "story_template_method",
        "detail": "full",
        "guidance": "summary",
        "guide_content": "完整故事模板指南 " + ("y" * 3500),
        "node_pattern": [
            {"type": "image", "purpose": "story_template_board"},
            {"type": "video", "purpose": "video_from_story_template_board"},
        ],
    }

    content = context_compact.prepare_tool_result_for_context(
        result,
        project_id="project-1",
        run_id="run-1",
        iteration=2,
        tool_name="tool.execute",
        budget_chars=200,
    )
    payload = json.loads(content)

    assert payload["tool_result_compacted"] is True
    assert payload["context_policy"] == "full_result"
    assert payload["summary"]["guide_content"] == result["guide_content"]
    assert payload["summary"]["_deferred_tool"] == "skill.story_template_method"


def test_compact_messages_wraps_background_boundary() -> None:
    messages = context_compact.compact_messages("用户偏好国风动漫；有待确认蓝图。")

    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert "<compacted_context kind=\"background_summary\">" in messages[0]["content"]
    assert "not the latest user instruction" in messages[0]["content"]
    assert "Project truth lives in runtime state and tools" in messages[0]["content"]
    assert messages[1]["role"] == "assistant"
    assert "follow the latest user message" in messages[1]["content"]


def test_compact_preserved_tail_expands_tool_result_to_matching_assistant_call() -> None:
    messages = [
        {"role": "user", "content": "旧请求"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call-old", "type": "function", "function": {"name": "node.list", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-old", "content": "{\"ok\": true}"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call-a", "type": "function", "function": {"name": "node.get", "arguments": "{}"}},
                {"id": "call-b", "type": "function", "function": {"name": "node.run", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call-a", "content": "{\"node\": \"a\"}"},
        {"role": "tool", "tool_call_id": "call-b", "content": "{\"ok\": true}"},
        {"role": "user", "content": "当前请求"},
    ]

    tail = context_compact.compact_preserved_tail(
        messages,
        token_budget=8,
        exclude_latest_user_content="当前请求",
    )

    assert [msg["role"] for msg in tail] == ["assistant", "tool", "tool"]
    assert tail[0]["tool_calls"][0]["id"] == "call-a"
    assert tail[1]["tool_call_id"] == "call-a"
    assert tail[2]["tool_call_id"] == "call-b"
    assert all(msg.get("content") != "当前请求" for msg in tail)


def test_compact_preserved_tail_skips_runtime_wrappers_and_current_user() -> None:
    messages = [
        {"role": "user", "content": "<system-reminder>\n规则\n</system-reminder>"},
        {"role": "assistant", "content": "明白。"},
        {"role": "user", "content": "<compacted_context kind=\"background_summary\">旧摘要</compacted_context>"},
        {"role": "user", "content": "上一条真实问题"},
        {"role": "assistant", "content": "上一条真实回答"},
        {"role": "user", "content": "当前请求"},
    ]

    tail = context_compact.compact_preserved_tail(
        messages,
        token_budget=20,
        exclude_latest_user_content="当前请求",
    )

    assert [msg["content"] for msg in tail] == ["明白。", "上一条真实问题", "上一条真实回答"]


def test_compact_preserved_tail_drops_single_message_over_budget() -> None:
    tail = context_compact.compact_preserved_tail(
        [{"role": "user", "content": "x" * 1000}],
        token_budget=10,
    )

    assert tail == []


def test_compact_messages_keeps_token_budgeted_tail_after_boundary() -> None:
    preserved_tail = [
        {"role": "user", "content": "上一条真实问题"},
        {"role": "assistant", "content": "上一条真实回答"},
    ]

    messages = context_compact.compact_messages(
        "历史偏好。",
        preserved_tail=preserved_tail,
    )

    assert len(messages) == 4
    assert "<compacted_context kind=\"background_summary\">" in messages[0]["content"]
    assert messages[2:] == preserved_tail


def test_save_transcript_uses_configured_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "transcripts_dir", lambda: tmp_path)

    path = context_compact.save_transcript(
        [{"role": "user", "content": "hello"}],
        project_id="project",
    )

    assert path.parent == tmp_path
    assert path.name.startswith("project_")
    assert '"content": "hello"' in path.read_text(encoding="utf-8")


def test_prepare_tool_result_keeps_small_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)

    content = context_compact.prepare_tool_result_for_context(
        {"ok": True, "value": "small"},
        project_id="project",
        run_id="run",
        iteration=1,
        tool_name="tool.small",
        budget_chars=1000,
    )

    assert '"value": "small"' in content
    assert not list(tmp_path.rglob("*.json"))


def test_prepare_tool_result_writes_large_payload_to_disk(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)

    content = context_compact.prepare_tool_result_for_context(
        {"ok": True, "items": ["x" * 50 for _ in range(20)]},
        project_id="project",
        run_id="run",
        iteration=3,
        tool_name="tool.large",
        budget_chars=120,
    )

    assert '"tool_result_compacted": true' in content
    assert '"full_result_path"' in content
    written = list(tmp_path.rglob("*.json"))
    assert len(written) == 1
    assert '"items"' in written[0].read_text(encoding="utf-8")


def test_prepare_tool_result_keeps_error_feedback_when_compacted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)

    content = context_compact.prepare_tool_result_for_context(
        {
            "ok": False,
            "error": "节点 'segment_01' 不存在。",
            "error_kind": "node_not_found",
            "hint": "从 available_node_ids 选择真实节点。",
            "suggested_next": "read_state",
            "model_feedback": {
                "what_went_wrong": "节点 'segment_01' 不存在。",
                "how_to_fix": "从 available_node_ids 选择真实节点。",
            },
            "available_node_ids": ["brief", "storyboard_grid"],
            "large": ["x" * 80 for _ in range(20)],
        },
        project_id="project",
        run_id="run",
        iteration=4,
        tool_name="node.update",
        budget_chars=160,
    )

    assert '"tool_result_compacted": true' in content
    assert '"error_kind": "node_not_found"' in content
    assert '"suggested_next": "read_state"' in content
    assert "storyboard_grid" in content
    assert list(tmp_path.rglob("*.json"))


def test_large_node_run_result_preserves_media_status_and_url_when_compacted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)
    result = {
        "ok": True,
        "node_id": "image-1",
        "type": "image",
        "status": "completed",
        "url": "/api/media/project/image.png",
        "remote_url": "https://cdn.example/image.png",
        "result": {
            "status": "completed",
            "url": "/api/media/project/image.png",
            "n_succeeded": 1,
            "size": "1024x1024",
            "aspect_ratio": "1:1",
        },
        "changes": ["x" * 200 for _ in range(20)],
    }

    content = context_compact.prepare_tool_result_for_context(
        result,
        project_id="project",
        run_id="run",
        iteration=7,
        tool_name="node.run",
        budget_chars=180,
    )
    payload = json.loads(content)

    assert payload["tool_result_compacted"] is True
    summary = payload["summary"]
    assert summary["node_id"] == "image-1"
    assert summary["status"] == "completed"
    assert summary["url"] == "/api/media/project/image.png"
    assert summary["result"]["status"] == "completed"
    assert summary["result"]["url"] == "/api/media/project/image.png"
    assert summary["result"]["n_succeeded"] == 1


def test_large_node_get_result_preserves_output_stage_before_prompt_when_compacted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)
    result = {
        "id": "image-1",
        "type": "image",
        "title": "人物图",
        "status": "completed",
        "input": {
            "prompt": "很长的提示词" * 500,
            "depends_on": ["script-1"],
            "references": ["script-1"],
        },
        "output": {
            "type": "image",
            "stages": [
                {
                    "name": "图片",
                    "status": "completed",
                    "url": "/api/media/project/image.png",
                    "size": "1024x1024",
                    "aspect_ratio": "1:1",
                }
            ],
        },
    }

    content = context_compact.prepare_tool_result_for_context(
        result,
        project_id="project",
        run_id="run",
        iteration=8,
        tool_name="node.get",
        budget_chars=180,
    )
    payload = json.loads(content)
    encoded_summary = json.dumps(payload["summary"], ensure_ascii=False)

    assert encoded_summary.index('"output"') < encoded_summary.index('"input"')
    assert "/api/media/project/image.png" in encoded_summary
    assert "script-1" in encoded_summary


def test_list_run_tool_result_artifacts_matches_debug_relative_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)
    artifact = tmp_path / "project-1" / "run-1" / "result.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"ok": true}', encoding="utf-8")

    artifacts = context_compact.list_run_tool_result_artifacts(
        project_id="project-1",
        run_id="run-1",
    )

    assert artifacts == [
        {
            "name": "result.json",
            "path": "data/tool_results/project-1/run-1/result.json",
            "relative_path": "run-1/result.json",
            "size_bytes": len('{"ok": true}'),
            "mtime": artifacts[0]["mtime"],
        }
    ]
