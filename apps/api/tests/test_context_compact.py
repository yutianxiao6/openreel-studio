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
        tool_name="skill.get",
        budget_chars=200,
    )
    payload = json.loads(content)

    assert payload["tool_result_compacted"] is True
    assert payload["context_policy"] == "summary"
    assert payload["summary"]["model_summary"] == result["model_summary"]
    assert payload["summary"]["reference_policy"] == result["reference_policy"]
    assert "keys:" not in payload["summary"]["model_summary"]


def test_large_tool_search_result_keeps_candidate_names_visible(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)
    result = {
        "query": "template save reusable workflow",
        "category": "workflow",
        "mode": "keyword",
        "total": 2,
        "returned": 2,
        "tools": [
            {
                "name": "workflow.spec.apply_patch",
                "category": "workflow",
                "description": "创建、替换或修订 workflow spec，并保存为 artifact 或用户模板。" + ("x" * 800),
                "usage_hints": ["create 传 workflow；update 传 base 和 operations。"],
            },
            {
                "name": "workflow.materialize_artifact",
                "category": "workflow",
                "description": "按 workflow spec artifact_ref 物化画布 draft 节点和依赖边。",
                "usage_hints": ["/workflow 写入返回 artifact_ref 后使用。"],
            },
        ],
        "padding": "x" * 5000,
    }

    content = context_compact.prepare_tool_result_for_context(
        result,
        project_id="project-1",
        run_id="run-1",
        iteration=0,
        tool_name="tool.search",
        budget_chars=200,
    )
    payload = json.loads(content)
    summary = payload["summary"]

    assert payload["tool_result_compacted"] is True
    assert [item["name"] for item in summary["tools"]] == [
        "workflow.spec.apply_patch",
        "workflow.materialize_artifact",
    ]
    assert "tool.execute" in summary["next_action"]
    assert "keys:" not in json.dumps(summary, ensure_ascii=False)


def test_large_skill_search_result_keeps_workflow_template_inputs_visible(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)
    result = {
        "ok": True,
        "mode": "multi_query",
        "total": 1,
        "skills": [
            {
                "name": "general_short_drama_workflow",
                "category": "workflow",
                "description": "通用视频制作工作流" + ("x" * 500),
                "scope": "builtin",
                "usage": "命中同名或来源绑定的 workflow 模板",
                "direct_template": {
                    "template_id": "general_short_drama_workflow",
                    "name": "通用视频制作工作流",
                    "scope": "builtin",
                    "required_inputs": ["plot", "durationSeconds"],
                    "missing_inputs": ["plot", "durationSeconds"],
                    "input_fields": [
                        {"id": "plot", "label": "故事主题或剧情", "type": "string", "required": True, "missing": True},
                        {"id": "durationSeconds", "label": "总时长", "type": "integer", "required": True, "missing": True},
                    ],
                    "input_questions": [
                        {"id": "plot", "header": "剧情主题", "question": "请填写剧情主题。"},
                        {"id": "durationSeconds", "header": "总时长", "question": "请填写总时长。"},
                    ],
                },
            }
        ],
        "groups": [],
        "padding": "x" * 6000,
    }

    content = context_compact.prepare_tool_result_for_context(
        result,
        project_id="project-1",
        run_id="run-1",
        iteration=0,
        tool_name="skill.search",
        budget_chars=200,
    )
    payload = json.loads(content)
    summary = payload["summary"]

    assert payload["tool_result_compacted"] is True
    assert summary["skills"][0]["name"] == "general_short_drama_workflow"
    direct = summary["skills"][0]["direct_template"]
    assert direct["template_id"] == "general_short_drama_workflow"
    assert direct["missing_inputs"] == ["plot", "durationSeconds"]
    assert [field["label"] for field in direct["input_fields"]] == ["故事主题或剧情", "总时长"]
    assert [question["id"] for question in direct["input_questions"]] == ["plot", "durationSeconds"]
    assert "keys:" not in json.dumps(summary, ensure_ascii=False)


def test_large_tool_describe_result_keeps_schema_names_visible(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)
    result = {
        "tools": [
            {
                "name": "workflow.spec.apply_patch",
                "category": "workflow",
                "description": "创建、替换或修订 workflow spec。" + ("x" * 800),
                "usage_hints": ["一次调用内完成校验、audit 和保存。"],
                "example": "tool.execute(name='workflow.spec.apply_patch', input={...})",
                "input_schema": {
                    "type": "object",
                    "required": ["operation"],
                    "properties": {
                        "project_id": {"type": "string"},
                        "operation": {"type": "string"},
                        "workflow": {"type": "object"},
                        "sample_inputs": {"type": "object"},
                    },
                },
            }
        ],
        "not_found": [],
        "padding": "x" * 5000,
    }

    content = context_compact.prepare_tool_result_for_context(
        result,
        project_id="project-1",
        run_id="run-1",
        iteration=0,
        tool_name="tool.describe",
        budget_chars=200,
    )
    payload = json.loads(content)
    tool = payload["summary"]["tools"][0]

    assert tool["name"] == "workflow.spec.apply_patch"
    assert tool["required"] == ["operation"]
    assert "workflow" in tool["properties"]
    assert "tool.execute" in payload["summary"]["next_action"]


def test_large_workflow_execute_result_keeps_actionable_summary(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)
    result = {
        "_deferred_tool": "workflow.instantiate",
        "ok": True,
        "template_id": "segment_character",
        "template_name": "分段人物图",
        "instance_id": "wf_1",
        "created_count": 3,
        "edges_count": 0,
        "nodes": [
            {"id": f"node-{index}", "type": "image", "title": f"第{index}段人物图", "status": "idle", "input": {"large": "x" * 2000}}
            for index in range(1, 4)
        ],
        "runtime": {"instance_id": "wf_1", "template_id": "segment_character", "progress": {"ready": 3}, "steps": ["x" * 2000]},
        "next_action": "已创建画布节点；返回的 nodes 和 runtime 可作为验收依据。",
        "padding": "x" * 6000,
    }

    content = context_compact.prepare_tool_result_for_context(
        result,
        project_id="project-1",
        run_id="run-1",
        iteration=2,
        tool_name="tool.execute",
        budget_chars=400,
    )
    payload = json.loads(content)
    summary = payload["summary"]

    assert payload["tool_result_compacted"] is True
    assert summary["_deferred_tool"] == "workflow.instantiate"
    assert summary["created_count"] == 3
    assert summary["nodes"][0]["title"] == "第1段人物图"
    assert "已创建画布节点" in summary["next_action"]
    assert "keys:" not in json.dumps(summary, ensure_ascii=False)


def test_project_state_summary_keeps_authorized_workflow_ref_visible() -> None:
    state = {
        "title": "30秒视频测试",
        "_workflow_spec_authorized_refs": [
            {
                "template_id": "old_workflow",
                "artifact_ref": "",
                "authorized_by": "workflow_spec",
                "authorized_at": "2026-07-04T08:00:00+00:00",
                "task_hash": "hidden",
            },
            {
                "template_id": "general_short_drama_workflow",
                "artifact_ref": "",
                "authorized_by": "workflow_spec",
                "authorized_at": "2026-07-04T08:43:46+00:00",
                "task_hash": "hidden",
            },
            {
                "template_id": "general_short_drama_workflow",
                "artifact_ref": "",
                "decision": "reuse_existing",
                "version_id": "1",
                "input_fields": [
                    {"id": "plot", "label": "故事主题", "type": "string", "required": True},
                    {"id": "durationSeconds", "label": "目标时长", "type": "integer", "required": True, "default": 30},
                ],
                "authorized_by": "workflow_spec",
                "authorized_at": "2026-07-04T08:46:52+00:00",
                "task_hash": "hidden",
            },
        ],
        "workflow_input_values": {
            "by_workflow": {
                "general_short_drama_workflow": {
                    "workflow_id": "general_short_drama_workflow",
                    "updated_at": "2026-07-04T08:52:42Z",
                    "values": {
                        "plot": "未来城市外卖员发现时间裂缝。",
                        "durationSeconds": 30,
                        "segments": [{"segment_index": 1}, {"segment_index": 2}],
                    },
                }
            }
        },
    }

    summary = context_compact.summarize_tool_result_for_context("project.get_state", state)

    latest = summary["latest_authorized_workflow_ref"]
    assert latest["template_id"] == "general_short_drama_workflow"
    assert latest["decision"] == "reuse_existing"
    assert latest["authorized_by"] == "workflow_spec"
    assert [field["id"] for field in latest["input_fields"]] == ["plot", "durationSeconds"]
    assert "task_hash" not in json.dumps(summary, ensure_ascii=False)
    assert [item["template_id"] for item in summary["authorized_workflow_refs"]] == [
        "general_short_drama_workflow",
        "old_workflow",
    ]
    workflow_inputs = summary["workflow_input_values"]["by_workflow"][0]
    assert workflow_inputs["workflow_id"] == "general_short_drama_workflow"
    assert workflow_inputs["values_preview"]["durationSeconds"] == 30
    assert workflow_inputs["values_preview"]["segments"] == "<list:2>"


def test_large_agent_run_workflow_spec_result_keeps_template_ref_visible(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)
    result = {
        "_deferred_tool": "agent.run",
        "ok": True,
        "agent": "workflow_spec",
        "status": "completed",
        "summary": "已复用默认通用视频制作工作流。",
        "result": {
            "status": "completed",
            "decision": "reuse_existing",
            "template_id": "general_short_drama_workflow",
            "artifact_ref": "",
            "input_fields": [
                {"id": "plot", "label": "故事主题或剧情", "type": "string", "required": True},
                {"id": "durationSeconds", "label": "目标时长", "type": "integer", "required": True, "default": 30},
            ],
            "validation": {
                "ok": True,
                "workflow_id": "general_short_drama_workflow",
                "step_count": 18,
                "protocol": {"workflow_spec_version": "openreel.workflow.v1"},
            },
            "next_action": "输入齐全后使用 template_id 调用 workflow.run_all。",
        },
        "tool_log": [{"event": "large", "payload": "x" * 5000}],
    }

    content = context_compact.prepare_tool_result_for_context(
        result,
        project_id="project-1",
        run_id="run-1",
        iteration=3,
        tool_name="tool.execute",
        budget_chars=200,
    )
    payload = json.loads(content)
    summary = payload["summary"]

    assert payload["tool_result_compacted"] is True
    assert summary["template_id"] == "general_short_drama_workflow"
    assert summary["decision"] == "reuse_existing"
    assert summary["workflow_spec"]["template_id"] == "general_short_drama_workflow"
    assert [field["id"] for field in summary["workflow_spec"]["input_fields"]] == [
        "plot",
        "durationSeconds",
    ]
    assert summary["workflow_spec"]["validation"]["workflow_id"] == "general_short_drama_workflow"
    assert "keys:" not in json.dumps(summary, ensure_ascii=False)


def test_large_workflow_spec_write_result_keeps_projection_signals_visible(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)
    result = {
        "ok": True,
        "status": "completed",
        "operation": "create",
        "save_target": "template",
        "template_id": "model_authored_workflow",
        "version_id": "file",
        "preview": {
            "id": "model_authored_workflow",
            "name": "人物弧光工作流",
            "description": "x" * 4000,
            "step_count": 4,
            "deferred_group_count": 1,
            "input_ids": ["plot"],
            "required_inputs": ["plot"],
            "can_run": True,
        },
        "input_fields": [
            {"id": "plot", "label": "剧情主题", "type": "string", "required": True},
        ],
        "validation": {
            "ok": True,
            "workflow_id": "model_authored_workflow",
            "step_count": 4,
            "deferred_group_count": 1,
        },
        "audit": {
            "status": "pass",
            "ok": True,
            "can_save": True,
            "can_run": True,
            "recommended_use": "runnable",
            "summary": "Workflow audit passed.",
            "visible_output_count": 0,
            "dry_run": {
                "status": "pass",
                "ok": True,
                "step_count": 4,
                "repeat_instance_count": 0,
                "visible_output_ids": [],
                "final_output_ids": [],
                "repeat_groups": [],
                "executable_batches": [["story"]],
            },
        },
        "suggested_next": "call_workflow_canvas_inspect",
        "next_action": "Call workflow.canvas.inspect",
        "padding": "x" * 6000,
    }

    content = context_compact.prepare_tool_result_for_context(
        result,
        project_id="project-1",
        run_id="run-1",
        iteration=4,
        tool_name="workflow.spec.apply_patch",
        budget_chars=200,
    )
    payload = json.loads(content)
    summary = payload["summary"]

    assert payload["tool_result_compacted"] is True
    assert summary["template_id"] == "model_authored_workflow"
    assert summary["suggested_next"] == "call_workflow_canvas_inspect"
    assert len(summary["preview"]["description"]) < 550
    assert summary["audit"]["visible_output_count"] == 0
    assert summary["audit"]["dry_run"]["repeat_instance_count"] == 0
    assert summary["audit"]["dry_run"]["visible_output_ids"] == []
    assert summary["audit"]["dry_run"]["final_output_ids"] == []
    assert summary["audit"]["dry_run"]["repeat_groups"] == []
    assert "keys:" not in json.dumps(summary, ensure_ascii=False)


def test_workflow_spec_error_summary_keeps_repair_ref_and_content_fields() -> None:
    result = {
        "ok": False,
        "error": "Workflow spec must describe the framework only",
        "error_kind": "workflow_framework_content_not_allowed",
        "hint": "Use authoring schema and keep prompts as templates.",
        "repair_ref": "workflow_repair:abc.json",
        "content_fields": ["script.prompt"],
    }

    summary = context_compact.summarize_tool_result_for_context("workflow.spec.apply_patch", result)

    assert summary["repair_ref"] == "workflow_repair:abc.json"
    assert summary["content_fields"] == ["script.prompt"]
    assert summary["error_kind"] == "workflow_framework_content_not_allowed"


def test_workflow_canvas_inspect_summary_keeps_dynamic_input_diagnostics() -> None:
    result = {
        "ok": True,
        "status": "pass",
        "schema_version": "workflow_canvas_projection_v1",
        "workflow": {"id": "flow", "step_count": 1, "canvas_node_count": 0},
        "dynamic_inputs": {
            "status": "waiting_for_sample_outputs",
            "missing_sample_outputs": [
                {
                    "dimension": "segments",
                    "source": "steps.segments.output.items",
                    "step_id": "segments",
                    "context_example": {
                        "segments": {"output": {"items": [{"segment_text": "segment_text_sample"}]}}
                    },
                }
            ],
        },
        "next_action": "Re-run workflow.canvas.inspect with context",
        "padding": "x" * 6000,
    }

    content = context_compact.prepare_tool_result_for_context(
        result,
        project_id="project-1",
        run_id="run-1",
        iteration=4,
        tool_name="workflow.canvas.inspect",
        budget_chars=200,
    )
    payload = json.loads(content)
    dynamic_inputs = payload["summary"]["dynamic_inputs"]

    assert payload["tool_result_compacted"] is True
    assert dynamic_inputs["status"] == "waiting_for_sample_outputs"
    assert dynamic_inputs["missing_sample_outputs"][0]["source"] == "steps.segments.output.items"
    context_example = dynamic_inputs["missing_sample_outputs"][0]["context_example"]
    assert context_example["segments"]["output"]["items"][0]["segment_text"] == "segment_text_sample"


def test_full_skill_result_keeps_complete_guidance_visible_when_compacted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(context_compact, "tool_results_dir", lambda: tmp_path)
    result = {
        "ok": True,
        "skill": "video_production",
        "detail": "full",
        "model_summary": "summary only",
        "content": "完整 skill 正文 " + ("x" * 5000),
        "skill_path": "apps/api/app/skills/video_production/SKILL.md",
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
    assert payload["summary"]["context_policy"] == "full_result"
    assert payload["summary"]["content"] == result["content"]
    assert payload["summary"]["skill_path"] == result["skill_path"]
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


def test_node_get_summary_reads_text_content_from_nested_fields() -> None:
    result = {
        "id": "0",
        "type": "text",
        "title": "30秒短剧本",
        "status": "idle",
        "input": {
            "surface": "draft_canvas",
            "fields": {
                "content": "这是写在 fields.content 里的完整剧本正文。",
                "purpose": "剧本构思",
                "duration_seconds": 30,
            },
            "title": "30秒短剧本",
        },
        "output": None,
    }

    summary = context_compact.summarize_tool_result_for_context("node.get", result)

    assert summary["input"]["content_preview"] == "这是写在 fields.content 里的完整剧本正文。"
    assert summary["input"]["content_chars"] == len("这是写在 fields.content 里的完整剧本正文。")
    assert summary["input"]["purpose"] == "剧本构思"
    assert summary["input"]["duration_seconds"] == 30


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
