import json

from app.agent import context_compact


def test_auto_compact_threshold(monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "TOKEN_THRESHOLD", 10)

    assert context_compact.auto_compact_needed([{"role": "user", "content": "甲" * 20}]) is True
    assert context_compact.auto_compact_needed([{"role": "user", "content": "ok"}]) is False


def test_compact_summary_prompt_marks_summary_as_background_only() -> None:
    prompt = context_compact.build_compact_summary_prompt([
        {"role": "user", "content": "旧任务"},
        {"role": "assistant", "content": "旧结果"},
    ])

    assert "BACKGROUND ONLY" in prompt
    assert "next instruction" in prompt
    assert "旧任务" in prompt


def test_compact_summary_prompt_uses_token_budget_for_cjk_history() -> None:
    prompt = context_compact.build_compact_summary_prompt([
        {"role": "user", "content": "长" * 100_000},
        {"role": "assistant", "content": "答" * 100_000},
    ])

    assert context_compact.estimate_text_tokens([{"role": "user", "content": prompt}]) <= 3_000
    assert "tokens omitted" in prompt


def test_compact_summary_omits_codex_skill_context_fragments() -> None:
    prompt = context_compact.build_compact_summary_prompt([
        {"role": "user", "content": "当前真实任务"},
        {
            "role": "developer",
            "content": "<skills_instructions>\nCATALOG_BODY\n</skills_instructions>",
        },
        {
            "role": "user",
            "content": "<skill>\n<name>demo</name>\nSKILL_BODY\n</skill>",
        },
    ])

    assert "当前真实任务" in prompt
    assert "CATALOG_BODY" not in prompt
    assert "SKILL_BODY" not in prompt


def test_compact_messages_wraps_background_boundary() -> None:
    messages = context_compact.compact_messages(
        "稳定背景",
        preserved_tail=[{"role": "user", "content": "最近问题"}],
    )

    assert messages[0]["role"] == "user"
    assert '<compacted_context kind="background_summary">' in messages[0]["content"]
    assert messages[1]["role"] == "assistant"
    assert messages[2] == {"role": "user", "content": "最近问题"}


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
