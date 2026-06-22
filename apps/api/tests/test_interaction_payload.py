import json

from app.agent.interaction_payload import (
    build_interaction_agent_message,
    interaction_agent_payload,
)
from app.api.routes_chat import ChatRequest, _request_message, _request_user_metadata


def test_chat_route_keeps_interaction_message_and_metadata_separate() -> None:
    decision_inputs = {
        "kind": "interaction_input",
        "purpose": "video_blueprint_intake",
        "stage": "structure",
        "values": {"plot_outline": "少年剑客救人后反杀蒙面刺客"},
        "questions": [
            {
                "id": "plot_outline",
                "header": "剧情",
                "question": "剧情大纲按什么处理？",
                "options": [
                    {"label": "模型发挥", "description": "由模型规划"},
                    {"label": "沿用我给的大纲", "description": "以上下文作为约束"},
                ],
            }
        ],
    }
    request = ChatRequest(
        project_id="project-1",
        message="已提交：剧情结构\n- 剧情大纲：少年剑客救人后反杀蒙面刺客",
        decision_inputs=decision_inputs,
        client_user_message_id="client-msg-1",
    )

    assert _request_message(request) == request.message
    assert _request_user_metadata(request) == {
        "decisionInputs": decision_inputs,
        "clientUserMessageId": "client-msg-1",
    }


def test_interaction_agent_payload_is_typed_json_not_prompt_prose() -> None:
    decision_inputs = {
        "kind": "interaction_input",
        "target": "video_blueprint_intake",
        "purpose": "video_blueprint_intake",
        "stage": "structure",
        "title": "剧情结构",
        "values": {
            "plot_outline": "少年剑客救人后反杀蒙面刺客",
        },
        "questions": [
            {
                "id": "plot_outline",
                "header": "剧情",
                "question": "剧情大纲按什么处理？",
                "options": [
                    {"label": "模型发挥", "description": "由模型规划"},
                    {"label": "沿用我给的大纲", "description": "以上下文作为约束"},
                ],
            },
        ],
    }

    payload = interaction_agent_payload(
        decision_inputs,
        user_visible_message="已提交：剧情结构",
    )
    message = build_interaction_agent_message("已提交：剧情结构", decision_inputs)
    encoded = message.removeprefix("<interaction-input-json>\n").removesuffix("\n</interaction-input-json>")
    decoded = json.loads(encoded)

    assert decoded == payload
    assert payload["event"] == "interaction_input_submitted"
    assert payload["values"]["plot_outline"] == "少年剑客救人后反杀蒙面刺客"
    assert payload["questions"][0]["id"] == "plot_outline"
    assert "fields" not in payload
    assert "presentation" not in payload
    assert "问题：" not in message
    assert "表单用途" not in message
    assert "边界提醒" not in message


def test_interaction_agent_payload_preserves_generic_questions() -> None:
    decision_inputs = {
        "kind": "interaction_input",
        "purpose": "general",
        "title": "确认范围",
        "questions": [
            {
                "id": "scope",
                "header": "范围",
                "question": "这次先处理哪个范围？",
                "options": [
                    {"label": "只修当前问题", "description": "最快，改动最小"},
                    {"label": "顺手整理相邻代码", "description": "范围稍大"},
                ],
            }
        ],
        "values": {"scope": "只修当前问题"},
    }

    payload = interaction_agent_payload(decision_inputs, user_visible_message="已提交：确认范围")

    assert payload["questions"][0]["question"] == "这次先处理哪个范围？"
    assert payload["values"]["scope"] == "只修当前问题"
    assert "fields" not in payload
