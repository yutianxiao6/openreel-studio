import json

from app.agent import context_compact


def test_auto_compact_threshold(monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "TOKEN_THRESHOLD", 10)

    assert context_compact.auto_compact_needed(
        [{"role": "user", "content": "甲" * 20}],
        threshold=10,
    ) is True
    assert context_compact.auto_compact_needed([{"role": "user", "content": "ok"}]) is False


def test_compaction_threshold_matches_codex_ninety_percent_and_explicit_cap() -> None:
    assert context_compact.compaction_threshold(context_window_tokens=100_000) == 90_000
    assert context_compact.compaction_threshold(
        context_window_tokens=100_000,
        explicit_limit=70_000,
    ) == 70_000
    assert context_compact.compaction_threshold(
        context_window_tokens=100_000,
        explicit_limit=95_000,
    ) == 90_000


def test_auto_compact_counts_system_and_tool_schema() -> None:
    assert context_compact.auto_compact_needed(
        [{"role": "user", "content": "ok"}],
        threshold=10,
        system="s" * 30,
        tools=[{"type": "function", "name": "node__list", "description": "d" * 30}],
    ) is True


def test_codex_compaction_prompt_is_checkpoint_handoff() -> None:
    assert "CONTEXT CHECKPOINT COMPACTION" in context_compact.CODEX_COMPACTION_PROMPT
    assert "Current progress and key decisions made" in context_compact.CODEX_COMPACTION_PROMPT
    assert "What remains to be done" in context_compact.CODEX_COMPACTION_PROMPT


def test_local_compaction_keeps_recent_real_users_and_one_summary_checkpoint() -> None:
    messages = [
        {"role": "user", "content": "当前真实任务"},
        {"role": "assistant", "content": "旧回答"},
        {"type": "function_call", "call_id": "c1", "name": "node__list", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "{}"},
        {
            "role": "developer",
            "content": "<skills_instructions>\nCATALOG_BODY\n</skills_instructions>",
        },
        {
            "role": "user",
            "content": "<skill>\n<name>demo</name>\nSKILL_BODY\n</skill>",
        },
    ]

    compacted = context_compact.codex_local_compacted_history(messages, "完成 A，下一步 B")

    assert compacted[0] == {"role": "user", "content": "当前真实任务"}
    assert all(item.get("role") != "assistant" for item in compacted)
    assert all(item.get("type") not in {"function_call", "function_call_output"} for item in compacted)
    assert compacted[-1]["role"] == "user"
    assert compacted[-1]["content"].startswith(context_compact.CODEX_SUMMARY_PREFIX)
    assert "完成 A，下一步 B" in compacted[-1]["content"]


def test_remote_compaction_removes_stale_wrappers_but_keeps_opaque_item() -> None:
    items = context_compact.sanitize_remote_compaction_items([
        {"role": "developer", "content": "old runtime"},
        {"role": "user", "content": "<skill>\nold skill\n</skill>"},
        {"role": "user", "content": "真实任务"},
        {"type": "function_call", "call_id": "c1", "name": "node__list", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "{}"},
        {"type": "compaction", "id": "cmp_1", "encrypted_content": "opaque"},
    ])

    assert items == [
        {"role": "user", "content": "真实任务"},
        {"type": "compaction", "id": "cmp_1", "encrypted_content": "opaque"},
    ]


def test_remove_oldest_compaction_unit_keeps_function_pair_invariant() -> None:
    reduced = context_compact.remove_oldest_compaction_unit([
        {"type": "function_call", "call_id": "c1", "name": "node__list", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "{}"},
        {"role": "user", "content": "继续"},
    ])

    assert reduced == [{"role": "user", "content": "继续"}]


def test_compaction_trims_newest_function_outputs_to_fit_model_window() -> None:
    messages = [
        {"role": "user", "content": "读取"},
        {"type": "function_call_output", "call_id": "c1", "output": "x" * 4000},
        {"type": "function_call_output", "call_id": "c2", "output": "y" * 4000},
    ]

    trimmed, rewritten, deleted_tokens = context_compact.trim_function_outputs_for_compaction(
        messages,
        max_input_tokens=500,
    )

    assert rewritten >= 1
    assert deleted_tokens > 0
    assert "truncated" in trimmed[-1]["output"]
    assert context_compact.estimate_tokens(trimmed) < context_compact.estimate_tokens(messages)


def test_preserved_tail_keeps_tool_call_and_result_together() -> None:
    messages = [
        {"role": "user", "content": "较早问题"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "function": {"name": "node__list", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": '{"ok":true}'},
        {"role": "assistant", "content": "已读取"},
    ]

    tail = context_compact.compact_preserved_tail(messages, token_budget=200)

    assert [message["role"] for message in tail] == ["user", "assistant", "tool", "assistant"]
    assert tail[2]["tool_call_id"] == "call-1"


def test_preserved_tail_keeps_responses_function_call_and_output_together() -> None:
    messages = [
        {"role": "user", "content": "读取节点"},
        {"id": "rs-1", "type": "reasoning", "encrypted_content": "opaque"},
        {
            "id": "fc-1",
            "type": "function_call",
            "call_id": "call-1",
            "name": "node__get",
            "arguments": '{"node_id":"7"}',
        },
        {"type": "function_call_output", "call_id": "call-1", "output": '{"ok":true}'},
        {
            "id": "msg-1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "已读取"}],
        },
    ]

    tail = context_compact.compact_preserved_tail(messages, token_budget=300)

    assert any(item.get("type") == "function_call" for item in tail)
    assert any(
        item.get("type") == "function_call_output" and item.get("call_id") == "call-1"
        for item in tail
    )
    assert context_compact.estimate_tokens(tail) > 0


def test_preserved_tail_skips_runtime_wrappers_and_current_user() -> None:
    messages = [
        {"role": "user", "content": "<system-reminder>runtime</system-reminder>"},
        {"role": "user", "content": "上一条真实问题"},
        {"role": "assistant", "content": "上一条真实回答"},
        {"role": "user", "content": "当前任务"},
    ]

    tail = context_compact.compact_preserved_tail(
        messages,
        token_budget=200,
        exclude_latest_user_content="当前任务",
    )

    assert tail == [
        {"role": "user", "content": "上一条真实问题"},
        {"role": "assistant", "content": "上一条真实回答"},
    ]


def test_preserved_tail_drops_message_that_exceeds_budget() -> None:
    assert context_compact.compact_preserved_tail(
        [{"role": "user", "content": "甲" * 200}],
        token_budget=10,
    ) == []


def test_estimate_tokens_counts_typed_images_without_serializing_bytes() -> None:
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }]

    assert context_compact.estimate_tokens(messages) >= context_compact.image_token_estimate()


def test_estimate_tokens_counts_responses_input_images() -> None:
    messages = [{
        "role": "user",
        "content": [
            {"type": "input_text", "text": "看图"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
        ],
    }]

    assert context_compact.estimate_tokens(messages) >= context_compact.image_token_estimate()


def test_save_transcript_uses_configured_directory_and_redacts_images(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "transcripts_dir", lambda: tmp_path)
    data_url = "data:image/png;base64,SECRET"

    path = context_compact.save_transcript(
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_url}}]}],
        project_id="project",
    )

    assert path.parent == tmp_path
    assert path.name.startswith("project_")
    rendered = path.read_text(encoding="utf-8")
    assert data_url not in rendered
    assert json.loads(rendered)["role"] == "user"
