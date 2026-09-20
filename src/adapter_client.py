"""QQ 适配器调用辅助模块。"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api import adapter_api
from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("snowluma_extension")

_ONEBOT_ADAPTER_PLUGIN = "onebot_adapter"


def get_qq_adapter_signature() -> str | None:
    """获取当前可用的 QQ 适配器签名。

    Returns:
        str | None: 已启动的 QQ 适配器签名；没有可用适配器时返回 None。
    """

    adapters = adapter_api.get_all_adapters()
    candidates = [
        signature
        for signature, adapter in adapters.items()
        if adapter.platform == "qq"
    ]
    if not candidates:
        return None

    preferred_signature = next(
        (
            signature
            for signature in candidates
            if signature.split(":", 1)[0] == _ONEBOT_ADAPTER_PLUGIN
        ),
        None,
    )
    if preferred_signature is not None:
        if len(candidates) > 1:
            logger.warning(
                "检测到多个 QQ 适配器，SnowLuma 扩展将优先使用 OneBot 适配器："
                f"{preferred_signature}"
            )
        return preferred_signature

    if len(candidates) > 1:
        logger.warning(
            "检测到多个 QQ 适配器，SnowLuma 扩展将使用首个已启动适配器："
            f"{candidates[0]}"
        )
    return candidates[0]


async def call_qq_adapter_api(
    action_name: str,
    params: dict[str, Any],
    timeout: float = 30.0,
) -> dict[str, Any]:
    """通过框架公共接口调用 QQ 适配器命令。

    Args:
        action_name: OneBot 兼容的 API 动作名称。
        params: API 请求参数。
        timeout: 请求超时时间（秒）。

    Returns:
        dict[str, Any]: 适配器返回的响应字典。
    """

    adapter_signature = get_qq_adapter_signature()
    if adapter_signature is None:
        return {
            "status": "error",
            "message": "未找到已启动的 QQ 适配器，请先启用 OneBot 或其他兼容适配器。",
            "data": None,
        }

    try:
        return await adapter_api.send_adapter_command(
            adapter_sign=adapter_signature,
            command_name=action_name,
            command_data=params,
            timeout=timeout,
        )
    except Exception as exc:
        logger.error(
            f"QQ 适配器命令调用异常: action={action_name}, params={params}, error={exc}"
        )
        return {
            "status": "error",
            "message": f"QQ 适配器命令调用异常：{exc}",
            "data": None,
        }


__all__ = ["call_qq_adapter_api", "get_qq_adapter_signature"]