from app.agent.orchestrator import _StreamingUserVisibleText


def test_streaming_user_text_sanitizes_identifier_split_between_deltas() -> None:
    stream = _StreamingUserVisibleText()

    chunks = [
        stream.push("正在调用 node."),
        stream.push("create 完成。"),
        stream.finish(),
    ]

    assert "".join(chunks) == "正在调用 内部动作 完成。"
    assert all("node.create" not in chunk for chunk in chunks)
