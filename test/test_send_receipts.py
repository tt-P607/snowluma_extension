"""SnowLuma 扩展的适配器命令回执测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from plugins.snowluma_extension.src import actions, adapter_client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_success", "expected_text"),
    [
        ({"status": "ok", "retcode": 0}, True, "ok"),
        ({"status": "ok", "retcode": False}, False, "retcode=False"),
        ({"status": "ok", "retcode": True}, False, "retcode=True"),
        ({"status": "ok"}, False, "未知错误"),
        ({"status": "timeout", "message": "timed out"}, False, "不要自动重试"),
        ({"status": "async", "retcode": 1}, False, "异步受理"),
    ],
)
async def test_public_callers_require_explicit_sync_success(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object],
    expected_success: bool,
    expected_text: str,
) -> None:
    """两个公开调用器均严格检查成功回执并保留未知状态原因。"""
    monkeypatch.setattr(actions, "call_qq_adapter_api", AsyncMock(return_value=response))

    result, message = await actions._call_qq_adapter_api(
        action_name="set_group_name", params={},
    )
    data_result, data_message, data = await actions._call_qq_adapter_api_with_data(
        action_name="set_group_name", params={},
    )

    assert result is expected_success
    assert data_result is expected_success
    assert message == data_message
    assert data is None
    if expected_text != "ok":
        assert expected_text in message


@pytest.mark.asyncio
async def test_adapter_timeout_exception_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """调用超时保留为不确定结果，不能提示为确定失败。"""
    monkeypatch.setattr(adapter_client, "get_qq_adapter_signature", lambda: "onebot:adapter:qq")
    monkeypatch.setattr(
        adapter_client.adapter_api,
        "send_adapter_command",
        AsyncMock(side_effect=TimeoutError("deadline")),
    )

    response = await adapter_client.call_qq_adapter_api("send_group_msg", {})

    assert response["status"] == "timeout"
    assert "deadline" in str(response["message"])
    assert "可能已执行" in actions._format_adapter_failure("send_group_msg", response)


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id", [None, False, 0, "", "invalid", 42, "-42"])
async def test_message_actions_require_message_id(
    monkeypatch: pytest.MonkeyPatch, message_id: object,
) -> None:
    """发送类回执不能仅凭 ok 声称送达，管理动作不受此约束。"""
    response = {"status": "ok", "retcode": 0, "data": {"message_id": message_id}}
    monkeypatch.setattr(actions, "call_qq_adapter_api", AsyncMock(return_value=response))
    success, detail = await actions._call_qq_adapter_api(action_name="send_msg", params={})
    expected = type(message_id) is int and message_id == 42 or message_id == "-42"
    assert success is expected
    if not expected:
        assert "未提供有效消息 ID" in detail
        assert "不要自动重试" in detail
    assert (await actions._call_qq_adapter_api(action_name="set_group_name", params={}))[0]