"""snowluma_extension Actions。

每个 Action 都通过 `snowluma_adapter` 的 `send_snowluma_api(action, params)` 或者
向 core 发送含有 CommandType 的 MessageEnvelope 来调用 SnowLuma 功能。
并通过 go_activate() 读取配置开关决定是否向 LLM 暴露。
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime
from typing import Annotated, Any, cast

import orjson

from src.app.plugin_system.api import adapter_api, plugin_api, storage_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseAction
from src.app.plugin_system.types import ChatType
from src.kernel.concurrency import get_task_manager

from ..config import SnowLumaExtensionConfig
from .qq_faces import QQ_FACE

logger = get_logger("snowluma_extension")

_SNOWLUMA_ADAPTER_SIGNATURE = "snowluma_adapter:adapter:snowluma_adapter"


def _coerce_int_if_digit(value: Any) -> Any:
    """将纯数字字符串转换为 int，其他保持原样。"""

    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            try:
                return int(s)
            except Exception:
                return value
    return value


def _get_group_id_from_context(action: BaseAction) -> Any:
    """从当前上下文消息中提取 group_id。"""

    context = action.chat_stream.context

    # 优先使用 BaseAction 的目标消息选择逻辑（会回落到最后一条上下文消息）
    msg = None
    try:
        msg = action._get_context_message_for_target()  # type: ignore[attr-defined]
    except Exception:
        msg = None

    if msg is not None:
        group_id = msg.extra.get("group_id") or msg.extra.get("target_group_id")
        if group_id is not None:
            return group_id

    # 兜底：在上下文候选消息中回溯查找（避免 current_message 为空时误判）
    candidates = []
    candidates.extend(context.unread_messages)
    candidates.extend(context.history_messages)
    candidates.extend(list(context.message_cache))
    candidates.append(context.current_message)

    for m in reversed([c for c in candidates if c is not None]):
        group_id = m.extra.get("group_id") or m.extra.get("target_group_id")
        if group_id is not None:
            return group_id

    return None


def _is_group_allowed(
    group_id: Any,
    list_type: str,
    group_list: list[str | int],
) -> bool:
    """判定指定群号是否在黑/白名单允许范围内。

    Args:
        group_id: 待判定的群号
        list_type: 名单类型 ('white' / 'black')
        group_list: 群号列表

    Returns:
        bool: 是否允许
    """
    if not group_id:
        return False
    str_group_id = str(group_id).strip()
    str_group_set = {str(gid).strip() for gid in group_list if str(gid).strip()}

    normalized_type = (list_type or "white").strip().lower()
    if normalized_type == "white":
        return str_group_id in str_group_set
    if normalized_type == "black":
        return str_group_id not in str_group_set
    return True


def _format_snowluma_failure(action: str, resp: dict[str, Any], error_hint: str = "") -> str:
    """将 SnowLuma 响应格式化为更易懂的失败文本。

    Args:
        action: API 动作名称
        resp: SnowLuma 响应字典
        error_hint: 附加给 LLM 的提示词（来自插件配置），指导 bot 如何向用户反馈错误
    """

    retcode = resp.get("retcode")
    message = str(resp.get("message") or "").strip()
    wording = str(resp.get("wording") or "").strip()
    detail = wording or message

    if not detail:
        detail = f"retcode={retcode}" if retcode is not None else "未知错误"

    # 常见权限/失败原因提炼
    lowered = detail.lower()
    if "权限" in detail or "permission" in lowered or "not admin" in lowered:
        result = (
            f"{action} 失败：权限不足。\n"
            "- 需要机器人为群主/管理员\n"
            "- 目标用户权限必须低于机器人\n"
            f"- 原始信息：{detail}"
        )
    elif "不存在" in detail or "not found" in lowered:
        result = (
            f"{action} 失败：目标不存在或已失效。\n"
            f"- 原始信息：{detail}"
        )
    elif "超时" in detail or "timeout" in lowered:
        result = (
            f"{action} 失败：请求超时。\n"
            "- 请检查 snowluma_adapter 是否已连接 SnowLuma\n"
            "- 请检查 SnowLuma 服务是否正常\n"
            f"- 原始信息：{detail}"
        )
    else:
        result = f"{action} 失败：{detail}"

    # 追加用户配置的提示词
    if error_hint:
        result += f"\n\n[提示] {error_hint}"

    return result


def _get_error_hint() -> str:
    """获取插件配置中的错误提示词。"""

    try:
        plugin = plugin_api.get_plugin("snowluma_extension")
        if plugin and plugin.config:
            config = cast(SnowLumaExtensionConfig, plugin.config)
            return config.plugin.error_hint
    except Exception:
        pass
    return ""


async def _call_snowluma_api(
    *,
    action_name: str,
    params: dict[str, Any],
    timeout: float = 30.0,
) -> tuple[bool, str]:
    """调用 snowluma_adapter API 并统一解析响应。

    Args:
        action_name: SnowLuma API 动作名称
        params: API 参数
        timeout: 超时时间（秒）

    Returns:
        tuple[bool, str]: (是否成功, 结果文本)
    """

    adapter = adapter_api.get_adapter(_SNOWLUMA_ADAPTER_SIGNATURE)
    if adapter is None:
        logger.warning(f"SnowLuma API 调用失败：adapter 未找到 (signature={_SNOWLUMA_ADAPTER_SIGNATURE})")
        return False, "snowluma_adapter 未启动：请先启用并启动 snowluma_adapter 插件。"

    if not hasattr(adapter, "send_snowluma_api"):
        logger.warning(f"SnowLuma API 调用失败：adapter 不支持 send_snowluma_api (type={type(adapter).__name__})")
        return False, "snowluma_adapter 不支持 send_snowluma_api：请确认 snowluma_adapter 版本兼容。"

    logger.debug(f"调用 SnowLuma API: action={action_name}, params={params}")

    try:
        resp = await adapter.send_snowluma_api(action_name, params, timeout=timeout)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.error(f"SnowLuma API 调用异常: action={action_name}, params={params}, error={exc}")
        return (
            False,
            f"调用 SnowLuma API 异常：{exc}\n- action={action_name}\n- params={params}",
        )

    logger.debug(f"SnowLuma API 响应: action={action_name}, resp={resp}")

    status = str(resp.get("status") or "").strip().lower()
    retcode = resp.get("retcode")
    if status == "ok" and (retcode == 0 or retcode is None):
        logger.info(f"SnowLuma API 调用成功: action={action_name}")
        return True, "ok"

    logger.warning(f"SnowLuma API 调用失败: action={action_name}, status={status}, retcode={retcode}, resp={resp}")
    return False, _format_snowluma_failure(action_name, resp, _get_error_hint())


async def _call_snowluma_api_with_data(
    *,
    action_name: str,
    params: dict[str, Any],
    timeout: float = 30.0,
) -> tuple[bool, str, Any]:
    """调用 snowluma_adapter API 并返回 data 字段。

    在 ``_call_snowluma_api`` 基础上额外返回响应的 ``data`` 字段，
    供需要处理返回数据的 Tool / 轮询器复用。

    Args:
        action_name: SnowLuma API 动作名称
        params: API 参数
        timeout: 超时时间（秒）

    Returns:
        tuple[bool, str, Any]: (是否成功, 结果文本, data 字段)
    """

    adapter = adapter_api.get_adapter(_SNOWLUMA_ADAPTER_SIGNATURE)
    if adapter is None:
        logger.warning(f"SnowLuma API 调用失败：adapter 未找到 (signature={_SNOWLUMA_ADAPTER_SIGNATURE})")
        return False, "snowluma_adapter 未启动：请先启用并启动 snowluma_adapter 插件。", None

    if not hasattr(adapter, "send_snowluma_api"):
        logger.warning(f"SnowLuma API 调用失败：adapter 不支持 send_snowluma_api (type={type(adapter).__name__})")
        return False, "snowluma_adapter 不支持 send_snowluma_api：请确认 snowluma_adapter 版本兼容。", None

    logger.debug(f"调用 SnowLuma API: action={action_name}, params={params}")

    try:
        resp = await adapter.send_snowluma_api(action_name, params, timeout=timeout)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.error(f"SnowLuma API 调用异常: action={action_name}, params={params}, error={exc}")
        return (
            False,
            f"调用 SnowLuma API 异常：{exc}\n- action={action_name}\n- params={params}",
            None,
        )

    logger.debug(f"SnowLuma API 响应: action={action_name}, resp={resp}")

    status = str(resp.get("status") or "").strip().lower()
    retcode = resp.get("retcode")
    if status == "ok" and (retcode == 0 or retcode is None):
        logger.info(f"SnowLuma API 调用成功: action={action_name}")
        return True, "ok", resp.get("data")

    logger.warning(f"SnowLuma API 调用失败: action={action_name}, status={status}, retcode={retcode}, resp={resp}")
    return False, _format_snowluma_failure(action_name, resp, _get_error_hint()), None


class _SnowLumaBaseAction(BaseAction):
    """snowluma_extension Action 基类：提供通用激活判断。"""

    associated_platforms: list[str] = ["qq"]
    associated_types: list[str] = ["text"]

    async def go_activate(self) -> bool:  # noqa: D401
        """根据插件配置判定是否激活。"""

        config = cast(SnowLumaExtensionConfig | None, self.plugin.config)
        if config is None or not config.plugin.enabled:
            return False

        return await self._feature_enabled(config)

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        raise NotImplementedError


class HandleGroupJoinRequestAction(_SnowLumaBaseAction):
    """处理加群请求（通过/拒绝）。"""

    name: str = "handle_group_join_request"
    description: str = (
        "通过或拒绝一个加群请求。需要先调用 get_group_join_requests 工具获取"
        "请求列表中的 flag，再用 flag 执行审批。approve=true 通过申请，"
        "approve=false 拒绝申请（可附理由）。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        if not config.join_request.enable:
            return False
        group_id = _get_group_id_from_context(self)
        if group_id is not None and not _is_group_allowed(
            group_id,
            config.join_request.group_list_type,
            config.join_request.group_list,
        ):
            return False
        return True

    async def execute(
        self,
        flag: Annotated[str, "要处理的加群请求标识（来自 get_group_join_requests 返回的 flag 字段）"],
        approve: Annotated[bool, "true=通过申请，false=拒绝申请"] = True,
        reason: Annotated[str, "拒绝理由（仅拒绝时有效，可留空）"] = "",
    ) -> tuple[bool, str]:
        """执行加群请求审批。

        Args:
            flag: 加群请求标识
            approve: 是否通过
            reason: 拒绝理由

        Returns:
            tuple[bool, str]: (是否成功, 结果描述)
        """
        if not flag or not flag.strip():
            return False, "flag 不能为空，请先调用 get_group_join_requests 获取有效的 flag。"

        config = cast(SnowLumaExtensionConfig | None, self.plugin.config)
        group_id = _get_group_id_from_context(self)
        if config is not None and group_id is not None and not _is_group_allowed(
            group_id,
            config.join_request.group_list_type,
            config.join_request.group_list,
        ):
            return False, f"群 {group_id} 不在加群审批允许名单中。"

        params: dict[str, Any] = {
            "flag": flag.strip(),
            "approve": bool(approve),
        }
        if not approve and reason:
            params["reason"] = reason

        ok, msg = await _call_snowluma_api(action_name="set_group_add_request", params=params)
        if ok:
            action_text = "通过" if approve else "拒绝"
            return True, f"已{action_text}加群请求（flag={flag}）。"
        return False, msg


# ==============================================================================
# SnowLuma 扩展动作
# ==============================================================================

class MuteGroupMemberAction(_SnowLumaBaseAction):
    """群成员禁言/解禁。"""

    name: str = "mute_group_member"
    description: str = (
        "在当前群聊中对指定用户执行禁言或解除禁言。需要你为群主或管理员，且目标权限低于你。"
        "传入 duration_seconds=0 表示解除禁言。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        return config.features.enable_mute

    async def execute(
        self,
        user_id: Annotated[str, "要禁言/解禁的目标 QQ 号"],
        duration_seconds: Annotated[int, "禁言时长（秒），0 表示解除禁言，例如 600 表示 10 分钟"] = 600,
    ) -> tuple[bool, str]:
        group_id = _get_group_id_from_context(self)
        if not group_id:
            return False, "该动作只能在群聊上下文使用：未获取到 group_id。"

        if duration_seconds < 0:
            return False, "duration_seconds 不能为负数（0 表示解除禁言）。"

        params = {
            "group_id": _coerce_int_if_digit(group_id),
            "user_id": _coerce_int_if_digit(user_id),
            "duration": int(duration_seconds),
        }

        ok, msg = await _call_snowluma_api(action_name="set_group_ban", params=params)
        if ok:
            if duration_seconds == 0:
                return True, f"已解除用户 {user_id} 的禁言。"
            return True, f"已禁言用户 {user_id}（{duration_seconds} 秒）。"
        return False, msg


class UnmuteGroupMemberAction(_SnowLumaBaseAction):
    """群成员解除禁言。"""

    name: str = "unmute_group_member"
    description: str = "在当前群聊中解除指定用户的禁言（duration=0）。"
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        return config.features.enable_mute

    async def execute(
        self,
        user_id: Annotated[str, "要解除禁言的目标 QQ 号"],
    ) -> tuple[bool, str]:
        group_id = _get_group_id_from_context(self)
        if not group_id:
            return False, "该动作只能在群聊上下文使用：未获取到 group_id。"

        params = {
            "group_id": _coerce_int_if_digit(group_id),
            "user_id": _coerce_int_if_digit(user_id),
            "duration": 0,
        }

        ok, msg = await _call_snowluma_api(action_name="set_group_ban", params=params)
        if ok:
            return True, f"已解除用户 {user_id} 的禁言。"
        return False, msg


class ReactToMessageAction(_SnowLumaBaseAction):
    """对指定消息添加表情回应（支持批量）。"""

    name: str = "react_to_message"
    description: str = (
        "对一条或多条消息添加表情回应（贴表情）。支持批量操作。"
        "传入 reactions 参数：一个 JSON 数组，每项格式为 {\"message_id\": \"消息ID\", \"emoji_id\": \"表情ID\"}。"
        "可以对同一条消息贴多个表情，也可以对不同消息分别贴表情。"
        "使用前请先调用 get_qq_face_list 工具查询可用的表情列表。"
        "emoji_id 必须使用表情 ID（数字，如 '76'、'66'），且必须来自 get_qq_face_list 返回的列表，严禁自行编造。"
        "示例：[{\"message_id\": \"12345\", \"emoji_id\": \"76\"}, {\"message_id\": \"12345\", \"emoji_id\": \"66\"}]"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        return config.features.enable_react

    @staticmethod
    def _resolve_emoji_id(raw_emoji: str) -> str | None:
        """将表情名称解析为数字 ID，返回 None 表示无法识别。"""
        raw_emoji = raw_emoji.strip()
        if raw_emoji.isdigit():
            return raw_emoji

        search_key = raw_emoji
        if not search_key.startswith("[表情："):
            search_key = f"[表情：{search_key}]"

        for face_id, face_name in QQ_FACE.items():
            if face_name == search_key or search_key in face_name:
                return face_id
        return None

    async def execute(
        self,
        reactions: Annotated[str, "表情回应数组的 JSON 字符串。每项含 message_id 和 emoji_id"],
    ) -> tuple[bool, str]:
        # 解析 reactions JSON
        try:
            reaction_list = orjson.loads(reactions)
        except Exception as e:
            return False, f"reactions 参数不是有效的 JSON：{e}"

        if not isinstance(reaction_list, list) or not reaction_list:
            return False, "reactions 必须是非空 JSON 数组。"

        # 预解析所有表情 ID
        tasks: list[tuple[str, str]] = []
        for item in reaction_list:
            if not isinstance(item, dict):
                continue
            msg_id = str(item.get("message_id", "")).strip()
            raw_emoji = str(item.get("emoji_id", "")).strip()
            if not msg_id or not raw_emoji:
                continue

            resolved = self._resolve_emoji_id(raw_emoji)
            if resolved is None:
                return False, f"无法识别的表情：{raw_emoji}。请使用 get_qq_face_list 工具查询可用的表情 ID。"
            tasks.append((msg_id, resolved))

        if not tasks:
            return False, "没有有效的表情回应任务。"

        # 逐个执行，每次间隔 0.5 秒
        success_count = 0
        fail_count = 0
        fail_details: list[str] = []

        for i, (msg_id, emoji_id) in enumerate(tasks):
            if i > 0:
                await asyncio.sleep(0.5 + random.uniform(-0.1, 0.1))

            params = {
                "message_id": _coerce_int_if_digit(msg_id),
                "emoji_id": emoji_id,
                "set": True,
            }

            ok, msg = await _call_snowluma_api(action_name="set_msg_emoji_like", params=params)
            if ok:
                success_count += 1
            else:
                fail_count += 1
                fail_details.append(f"消息{msg_id}/表情{emoji_id}: {msg[:50]}")

        if fail_count == 0:
            return True, f"已成功添加 {success_count} 个表情回应。"
        return False, (
            f"表情回应完成：成功 {success_count}/{len(tasks)}，失败 {fail_count}。"
            f"\n失败详情：{'；'.join(fail_details[:5])}"
        )


class PokeGroupMemberAction(_SnowLumaBaseAction):
    """戳一戳群成员。"""

    name: str = "poke_group_member"
    description: str = (
        "在当前群聊中戳一戳指定用户，可戳同一个人多次，也可戳多个不同的人。"
        "传入多个QQ号用逗号分隔即可批量戳不同的人。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        return config.features.enable_poke

    async def execute(
        self,
        user_ids: Annotated[str, "要戳一戳的目标QQ号，多个QQ号用英文逗号分隔（如 '123,456,789'）"],
        times: Annotated[int, "每人戳的次数，默认1次"] = 1,
        interval: Annotated[float, "每次戳之间的间隔（秒），默认0.5秒"] = 0.5,
    ) -> tuple[bool, str]:
        group_id = _get_group_id_from_context(self)
        if not group_id:
            return False, "该动作只能在群聊上下文使用：未获取到 group_id。"

        # 解析目标QQ号列表
        uid_list = [uid.strip() for uid in str(user_ids).split(",") if uid.strip()]
        if not uid_list:
            return False, "未提供有效的QQ号。"

        if times <= 0:
            times = 1

        if interval < 0:
            interval = 0.5

        total = len(uid_list) * times
        success_count = 0
        fail_count = 0
        fail_details: list[str] = []

        for uid in uid_list:
            for i in range(times):
                # 非第一次时等待间隔
                if not (len(uid_list) == 1 and i == 0):
                    await asyncio.sleep(interval)

                params = {
                    "group_id": _coerce_int_if_digit(group_id),
                    "user_id": _coerce_int_if_digit(uid),
                }

                ok, msg = await _call_snowluma_api(action_name="send_poke", params=params)
                if ok:
                    success_count += 1
                else:
                    fail_count += 1
                    fail_details.append(f"{uid}(第{i+1}次): {msg}")

        if fail_count == 0:
            if len(uid_list) == 1 and times == 1:
                return True, f"已戳一戳用户 {uid_list[0]}。"
            return True, f"已戳 {len(uid_list)} 人，每人 {times} 次，共 {total} 次全部成功。"
        else:
            return False, (
                f"戳一戳完成：成功 {success_count}/{total}，失败 {fail_count}。"
                f"\n失败详情：{'；'.join(fail_details[:5])}"
            )


class RecallMessageAction(_SnowLumaBaseAction):
    """撤回消息。"""

    name: str = "recall_message"
    description: str = (
        "撤回指定消息（需要机器人具备撤回权限；不同场景可能受时效限制）。"
    )
    chat_type: ChatType = ChatType.ALL

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        return config.features.enable_recall

    async def execute(
        self,
        message_id: Annotated[str, "要撤回的消息 ID"],
    ) -> tuple[bool, str]:
        params = {
            "message_id": _coerce_int_if_digit(message_id),
        }

        ok, msg = await _call_snowluma_api(action_name="delete_msg", params=params)
        if ok:
            return True, f"已撤回消息 {message_id}。"
        return False, msg


class GroupSignAction(_SnowLumaBaseAction):
    """群打卡。"""

    name: str = "group_sign"
    description: str = "在当前群聊中执行群打卡。"
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        return config.features.enable_group_sign

    async def execute(self) -> tuple[bool, str]:
        group_id = _get_group_id_from_context(self)
        if not group_id:
            return False, "该动作只能在群聊上下文使用：未获取到 group_id。"

        # 检查今天是否已打过卡
        today_str = datetime.now().strftime("%Y-%m-%d")
        try:
            record = await storage_api.load_json("snowluma_extension", "sign_record")
            if record and record.get("last_sign_date") == today_str:
                return True, "今日已打过卡，无需重复打卡。"
        except Exception:
            pass

        params = {"group_id": _coerce_int_if_digit(group_id)}

        ok, msg = await _call_snowluma_api(action_name="set_group_sign", params=params)
        if ok:
            # 记录今天已打卡
            try:
                await storage_api.save_json("snowluma_extension", "sign_record", {"last_sign_date": today_str})
            except Exception:
                pass
            return True, "已执行群打卡。"

        return False, msg


class KickGroupMemberAction(_SnowLumaBaseAction):
    """踢出群成员。"""

    name: str = "kick_group_member"
    description: str = (
        "在当前群聊中踢出指定用户。需要你为群主或管理员，且目标权限低于你。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        return config.features.enable_kick

    async def execute(
        self,
        user_id: Annotated[str, "要踢出的目标 QQ 号"],
        reject_add_request: Annotated[bool, "是否拒绝此人再次加群（true=拒绝，false=允许）"] = False,
    ) -> tuple[bool, str]:
        group_id = _get_group_id_from_context(self)
        if not group_id:
            return False, "该动作只能在群聊上下文使用：未获取到 group_id。"

        params = {
            "group_id": _coerce_int_if_digit(group_id),
            "user_id": _coerce_int_if_digit(user_id),
            "reject_add_request": bool(reject_add_request),
        }

        ok, msg = await _call_snowluma_api(action_name="set_group_kick", params=params)
        if ok:
            suffix = "（已拒绝再次加群）" if reject_add_request else ""
            return True, f"已踢出用户 {user_id}{suffix}。"
        return False, msg


# ==============================================================================
# SnowLuma 特有管理 Action
# ==============================================================================

class SetGroupNameAction(_SnowLumaBaseAction):
    """修改群名。"""

    name: str = "set_group_name"
    description: str = (
        "修改当前群聊的名称。需要你为群主或管理员。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        return config.features.enable_set_group_name

    async def execute(
        self,
        group_name: Annotated[str, "新的群名称"],
    ) -> tuple[bool, str]:
        group_id = _get_group_id_from_context(self)
        if not group_id:
            return False, "该动作只能在群聊上下文使用：未获取到 group_id。"

        params = {
            "group_id": _coerce_int_if_digit(group_id),
            "group_name": str(group_name),
        }

        ok, msg = await _call_snowluma_api(action_name="set_group_name", params=params)
        if ok:
            return True, f"已将群名修改为：{group_name}。"
        return False, msg


class SetGroupCardAction(_SnowLumaBaseAction):
    """修改群名片。"""

    name: str = "set_group_card"
    description: str = (
        "修改当前群聊中指定用户的群名片（也叫群昵称，即在群内显示的昵称名称）。"
        "修改自己的群名片不需要权限，修改他人的群名片需要你为群主或管理员。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        return config.features.enable_set_group_card

    async def execute(
        self,
        user_id: Annotated[str, "目标 QQ 号"],
        card: Annotated[str, "新的群名片（空字符串表示清除）"] = "",
    ) -> tuple[bool, str]:
        group_id = _get_group_id_from_context(self)
        if not group_id:
            return False, "该动作只能在群聊上下文使用：未获取到 group_id。"

        params = {
            "group_id": _coerce_int_if_digit(group_id),
            "user_id": _coerce_int_if_digit(user_id),
            "card": str(card),
        }

        ok, msg = await _call_snowluma_api(action_name="set_group_card", params=params)
        if ok:
            action_desc = f"将用户 {user_id} 的群名片修改为 {card}" if card else f"清除了用户 {user_id} 的群名片"
            return True, f"已{action_desc}。"
        return False, msg


class SetGroupSpecialTitleAction(_SnowLumaBaseAction):
    """修改群头衔。"""

    name: str = "set_group_special_title"
    description: str = (
        "修改当前群聊中指定用户的群专属头衔。需要你为群主（管理员不可）。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        return config.features.enable_set_group_special_title

    async def execute(
        self,
        user_id: Annotated[str, "目标 QQ 号"],
        special_title: Annotated[str, "新的专属头衔（空字符串表示清除）"] = "",
        duration: Annotated[int, "头衔有效期，单位秒。-1表示永久"] = -1,
    ) -> tuple[bool, str]:
        group_id = _get_group_id_from_context(self)
        if not group_id:
            return False, "该动作只能在群聊上下文使用：未获取到 group_id。"

        params = {
            "group_id": _coerce_int_if_digit(group_id),
            "user_id": _coerce_int_if_digit(user_id),
            "special_title": str(special_title),
            "duration": int(duration),
        }

        ok, msg = await _call_snowluma_api(action_name="set_group_special_title", params=params)
        if ok:
            action_desc = f"将用户 {user_id} 的群头衔修改为 {special_title}" if special_title else f"清除了用户 {user_id} 的群头衔"
            return True, f"已{action_desc}。"
        return False, msg


class SendGroupNoticeAction(_SnowLumaBaseAction):
    """发送群公告。"""

    name: str = "send_group_notice"
    description: str = (
        "在当前群聊中发布一条群公告。可附带图片，支持置顶、弹窗推送、新成员推送、改名引导和回执确认。"
        "群公告会展示在群公告页面，所有群成员可见。"
        "需要你为群主或管理员。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        return config.features.enable_send_group_notice

    async def execute(
        self,
        content: Annotated[str, "群公告的正文内容"],
        image: Annotated[str, "公告附带图片的URL或Base64（可选，留空则无图）"] = "",
        pinned: Annotated[bool, "是否置顶公告（true=置顶）"] = False,
        notice_type: Annotated[int, "公告类型：0=普通公告,1=弹窗推送,2=新成员推送,3=改名引导。默认0"] = 0,
        confirm_required: Annotated[bool, "是否需要群成员回执确认（true=需要确认）"] = False,
    ) -> tuple[bool, str]:
        group_id = _get_group_id_from_context(self)
        if not group_id:
            return False, "该动作只能在群聊上下文使用：未获取到 group_id。"

        params: dict[str, Any] = {
            "group_id": _coerce_int_if_digit(group_id),
            "content": str(content),
            "pinned": bool(pinned),
            "type": int(notice_type),
            "confirm_required": bool(confirm_required),
        }
        if image:
            params["image"] = str(image)

        ok, msg = await _call_snowluma_api(action_name="_send_group_notice", params=params)
        if ok:
            type_names = {0: "普通公告", 1: "弹窗推送", 2: "新成员推送", 3: "改名引导"}
            extras: list[str] = []
            if pinned:
                extras.append("已置顶")
            if notice_type != 0:
                extras.append(f"类型:{type_names.get(notice_type, str(notice_type))}")
            if confirm_required:
                extras.append("需回执确认")
            suffix = f"（{'，'.join(extras)}）" if extras else ""
            return True, f"已成功发布群公告{suffix}。"
        return False, msg


class DeleteGroupNoticeAction(_SnowLumaBaseAction):
    """删除群公告。"""

    name: str = "delete_group_notice"
    description: str = (
        "删除当前群聊中的指定群公告。需要提供公告ID（notice_id），"
        "可通过 get_group_notice 工具获取群公告列表来拿到每条公告的ID。"
        "需要你为群主或管理员。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        return config.features.enable_delete_group_notice

    async def execute(
        self,
        notice_id: Annotated[str, "要删除的群公告ID（可通过 get_group_notice 获取）"],
    ) -> tuple[bool, str]:
        group_id = _get_group_id_from_context(self)
        if not group_id:
            return False, "该动作只能在群聊上下文使用：未获取到 group_id。"

        params = {
            "group_id": _coerce_int_if_digit(group_id),
            "notice_id": str(notice_id),
        }

        ok, msg = await _call_snowluma_api(action_name="_del_group_notice", params=params)
        if ok:
            return True, f"已删除群公告 {notice_id}。"
        return False, msg


class SendGroupForwardMsgAction(_SnowLumaBaseAction):
    """发送群合并转发消息。"""

    name: str = "send_group_forward_msg"
    description: str = (
        "在当前群聊中发送合并转发消息（合并转发卡片）。"
        "传入 messages 参数：一个 JSON 数组，每个元素是一个转发节点对象。"
        "转发节点格式：{\"nickname\": \"发送者昵称\", \"user_id\": \"QQ号\", \"content\": [消息段]}。"
        "content 是 OneBot 消息段数组，支持：文本 {\"type\": \"text\", \"data\": {\"text\": \"内容\"}}、"
        "表情 {\"type\": \"face\", \"data\": {\"id\": \"表情ID\"}}。"
        "示例：[{\"nickname\": \"小明\", \"user_id\": \"10001\", \"content\": [{\"type\": \"text\", \"data\": {\"text\": \"你好\"}}]}]"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        return config.features.enable_send_forward_msg

    async def execute(
        self,
        messages: Annotated[str, "转发消息节点数组的 JSON 字符串。每个节点包含 nickname（昵称）、user_id（QQ号）、content（消息段数组）"],
    ) -> tuple[bool, str]:
        group_id = _get_group_id_from_context(self)
        if not group_id:
            return False, "该动作只能在群聊上下文使用：未获取到 group_id。"

        # 解析 messages JSON
        try:
            parsed_messages = orjson.loads(messages)
        except Exception as e:
            return False, f"messages 参数不是有效的 JSON：{e}"

        if not isinstance(parsed_messages, list) or not parsed_messages:
            return False, "messages 必须是非空 JSON 数组。"

        params = {
            "group_id": _coerce_int_if_digit(group_id),
            "messages": parsed_messages,
        }

        ok, msg = await _call_snowluma_api(action_name="send_group_forward_msg", params=params)
        if ok:
            return True, f"已成功发送合并转发消息（共 {len(parsed_messages)} 条节点）。"
        return False, msg


class SetEssenceMsgAction(_SnowLumaBaseAction):
    """设置精华消息。"""

    name: str = "set_essence_msg"
    description: str = (
        "将指定消息设为群精华消息。需要提供消息ID（message_id）。"
        "需要你为群主或管理员。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        return config.features.enable_essence_msg

    async def execute(
        self,
        message_id: Annotated[str, "要设为精华的消息 ID"],
    ) -> tuple[bool, str]:
        params = {
            "message_id": _coerce_int_if_digit(message_id),
        }

        ok, msg = await _call_snowluma_api(action_name="set_essence_msg", params=params)
        if ok:
            return True, f"已将消息 {message_id} 设为精华消息。"
        return False, msg


class DeleteEssenceMsgAction(_SnowLumaBaseAction):
    """移除精华消息。"""

    name: str = "delete_essence_msg"
    description: str = (
        "将指定消息从群精华消息中移除。需要提供消息ID（message_id）。"
        "需要你为群主或管理员。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        return config.features.enable_essence_msg

    async def execute(
        self,
        message_id: Annotated[str, "要移除精华的消息 ID"],
    ) -> tuple[bool, str]:
        params = {
            "message_id": _coerce_int_if_digit(message_id),
        }

        ok, msg = await _call_snowluma_api(action_name="delete_essence_msg", params=params)
        if ok:
            return True, f"已将消息 {message_id} 从精华消息中移除。"
        return False, msg


class ForwardGroupSingleMsgAction(_SnowLumaBaseAction):
    """转发单条消息到群。"""

    name: str = "forward_group_single_msg"
    description: str = (
        "将一条已有消息（通过 message_id 标识）转发到指定的群。"
        "可以转发任何类型的消息，包括文字、图片、合并转发等。"
        "需要提供要转发的消息 ID 和目标群号。"
    )
    chat_type: ChatType = ChatType.ALL

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        return config.features.enable_send_forward_msg

    async def execute(
        self,
        message_id: Annotated[str, "要转发的消息 ID"],
        group_id: Annotated[str, "目标群号"],
    ) -> tuple[bool, str]:
        params = {
            "message_id": _coerce_int_if_digit(message_id),
            "group_id": _coerce_int_if_digit(group_id),
        }

        ok, msg = await _call_snowluma_api(action_name="forward_group_single_msg", params=params)
        if ok:
            return True, f"已将消息 {message_id} 转发到群 {group_id}。"
        return False, msg


class ForwardFriendSingleMsgAction(_SnowLumaBaseAction):
    """转发单条消息给好友。"""

    name: str = "forward_friend_single_msg"
    description: str = (
        "将一条已有消息（通过 message_id 标识）转发给指定好友。"
        "可以转发任何类型的消息，包括文字、图片、合并转发等。"
        "需要提供要转发的消息 ID 和目标好友 QQ 号。"
    )
    chat_type: ChatType = ChatType.ALL

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        return config.features.enable_send_forward_msg

    async def execute(
        self,
        message_id: Annotated[str, "要转发的消息 ID"],
        user_id: Annotated[str, "目标好友 QQ 号"],
    ) -> tuple[bool, str]:
        params = {
            "message_id": _coerce_int_if_digit(message_id),
            "user_id": _coerce_int_if_digit(user_id),
        }

        ok, msg = await _call_snowluma_api(action_name="forward_friend_single_msg", params=params)
        if ok:
            return True, f"已将消息 {message_id} 转发给好友 {user_id}。"
        return False, msg


class SendLikeAction(_SnowLumaBaseAction):
    """给他人主页点赞。"""

    name: str = "send_like"
    description: str = (
        "给指定 QQ 用户的主页点赞。不需要好友关系，只要对方 QQ 号存在即可。"
        "点赞数量由配置决定（默认 10 个，非 SVIP 每日上限 10 次/人，SVIP 20 次/人）。"
        "每日对同一用户的点赞数有上限，超限会失败。"
    )
    chat_type: ChatType = ChatType.ALL
    associated_types: list[str] = ["text"]

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        return config.features.enable_send_like

    async def execute(
        self,
        user_id: Annotated[str, "要点赞的目标 QQ 号"],
    ) -> tuple[bool, str]:
        # 从配置读取点赞数
        config = cast(SnowLumaExtensionConfig | None, self.plugin.config)
        times = config.features.send_like_times if config is not None else 10

        params = {
            "user_id": _coerce_int_if_digit(user_id),
            "times": times,
        }

        ok, msg = await _call_snowluma_api(action_name="send_like", params=params)
        if ok:
            return True, f"已给用户 {user_id} 点赞 {times} 次。"
        return False, msg


class SetGroupWholeBanAction(_SnowLumaBaseAction):
    """全群禁言开关。"""

    name: str = "set_group_whole_ban"
    description: str = (
        "在当前群聊中开启或关闭全员禁言。需要你为群主或管理员。"
        "enable=true（默认）表示开启全员禁言，enable=false 表示关闭全员禁言。"
        "可传入 duration_seconds 指定定时自动关闭的秒数（仅 enable=true 时有效），"
        "例如 duration_seconds=1800 表示开启后 30 分钟自动关闭。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        return config.features.enable_set_group_whole_ban

    async def execute(
        self,
        enable: Annotated[bool, "是否开启全员禁言（true=开启，false=关闭）"] = True,
        duration_seconds: Annotated[int, "开启后多少秒自动关闭（仅 enable=true 时有效，0 表示不自动关闭）"] = 0,
    ) -> tuple[bool, str]:
        group_id = _get_group_id_from_context(self)
        if not group_id:
            return False, "该动作只能在群聊上下文使用：未获取到 group_id。"

        gid = _coerce_int_if_digit(group_id)

        params = {
            "group_id": gid,
            "enable": bool(enable),
        }

        ok, msg = await _call_snowluma_api(action_name="set_group_whole_ban", params=params)
        if ok:
            if enable and duration_seconds > 0:
                # 注册延迟任务自动关闭全群禁言（内存态，重启后失效）
                async def _auto_unban() -> None:
                    """延迟后自动关闭全群禁言。"""
                    await asyncio.sleep(duration_seconds)
                    unban_params = {"group_id": gid, "enable": False}
                    unban_ok, unban_msg = await _call_snowluma_api(
                        action_name="set_group_whole_ban", params=unban_params
                    )
                    if unban_ok:
                        logger.info(
                            f"定时全群禁言已自动关闭：group_id={gid}, "
                            f"持续 {duration_seconds}s"
                        )
                    else:
                        logger.warning(
                            f"定时全群禁言自动关闭失败：group_id={gid}, "
                            f"duration={duration_seconds}s, msg={unban_msg}"
                        )

                get_task_manager().create_task(
                    _auto_unban(),
                    name=f"snowluma_extension_whole_ban_auto_off_{gid}",
                    daemon=True,
                )
                return True, (
                    f"已开启全员禁言，将在 {duration_seconds} 秒后自动关闭。"
                )
            action_desc = "开启" if enable else "关闭"
            return True, f"已{action_desc}全员禁言。"
        return False, msg


class SendShareCardAction(_SnowLumaBaseAction):
    """发送推荐名片/群名片分享。"""

    name: str = "send_share_card"
    description: str = (
        "在当前会话中发送名片分享（群名片或个人名片）。\n"
        "- 分享群名片：传入 group_id（目标群号）\n"
        "- 分享个人名片：传入 user_id（目标 QQ 号）\n"
        "二者只能传一个；同时传入时以 group_id 为准。"
    )
    chat_type: ChatType = ChatType.ALL

    async def _feature_enabled(self, config: SnowLumaExtensionConfig) -> bool:
        return config.features.enable_send_share_card

    async def execute(
        self,
        user_id: Annotated[str, "要分享的个人名片 QQ 号（与 group_id 二选一）"] = "",
        group_id: Annotated[str, "要分享的群名片群号（与 user_id 二选一）"] = "",
    ) -> tuple[bool, str]:
        # 目标会话（发送到哪个群/私聊）
        target_group_id = _get_group_id_from_context(self)

        uid = str(user_id).strip() if user_id else ""
        gid = str(group_id).strip() if group_id else ""

        if not uid and not gid:
            return False, "必须提供 user_id（个人名片）或 group_id（群名片）中的至少一个。"

        # 二者都传时以 group_id 为准
        if gid:
            share_params: dict[str, Any] = {"group_id": _coerce_int_if_digit(gid)}
            card_kind = "群"
        else:
            share_params = {"user_id": _coerce_int_if_digit(uid)}
            card_kind = "个人"

        # 第一步：调用 send_ark_share 获取 Ark 卡片 JSON
        adapter = adapter_api.get_adapter(_SNOWLUMA_ADAPTER_SIGNATURE)
        if adapter is None:
            return False, "snowluma_adapter 未启动，无法发送名片。"
        try:
            resp = await adapter.send_snowluma_api("send_ark_share", share_params, timeout=30.0)  # type: ignore[attr-defined]
        except Exception as exc:
            return False, f"获取{card_kind}名片 Ark 卡片异常：{exc}"

        status = str(resp.get("status") or "").strip().lower()
        retcode = resp.get("retcode")
        if status != "ok" or (retcode not in (0, None)):
            return False, _format_snowluma_failure("send_ark_share", resp, _get_error_hint())

        data = resp.get("data") or {}
        ark_msg_str = data.get("arkMsg", "")
        if not ark_msg_str:
            return False, f"{card_kind}名片 Ark 卡片内容为空。"

        # 第二步：将 Ark JSON 作为 json 消息段发送到当前会话
        # SnowLuma 的 json 段格式：{"type": "json", "data": {"data": "<ark_json_string>"}}
        send_params: dict[str, Any] = {
            "message": [{"type": "json", "data": {"data": ark_msg_str}}],
        }
        if target_group_id:
            send_params["message_type"] = "group"
            send_params["group_id"] = _coerce_int_if_digit(target_group_id)
        else:
            # 私聊场景：从上下文取 user_id
            context = self.chat_stream.context
            target_user_id = None
            cur = context.current_message
            if cur is not None:
                target_user_id = cur.extra.get("user_id") or cur.extra.get("target_user_id")
            if not target_user_id:
                for m in reversed(context.unread_messages or []):
                    target_user_id = m.extra.get("user_id") or m.extra.get("target_user_id")
                    if target_user_id:
                        break
            if not target_user_id:
                return False, "无法确定发送目标：当前不在群聊上下文，也无法获取私聊 user_id。"
            send_params["message_type"] = "private"
            send_params["user_id"] = _coerce_int_if_digit(target_user_id)

        ok2, msg2 = await _call_snowluma_api(action_name="send_msg", params=send_params)
        if ok2:
            return True, f"已发送{card_kind}名片分享。"
        return False, msg2


__all__ = [
    "MuteGroupMemberAction",
    "HandleGroupJoinRequestAction",
    "UnmuteGroupMemberAction",
    "ReactToMessageAction",
    "PokeGroupMemberAction",
    "RecallMessageAction",
    "GroupSignAction",
    "KickGroupMemberAction",
    "SetGroupNameAction",
    "SetGroupCardAction",
    "SetGroupSpecialTitleAction",
    "SendGroupNoticeAction",
    "DeleteGroupNoticeAction",
    "SendGroupForwardMsgAction",
    "SetEssenceMsgAction",
    "DeleteEssenceMsgAction",
    "ForwardGroupSingleMsgAction",
    "ForwardFriendSingleMsgAction",
    "SendLikeAction",
    "SendShareCardAction",
    "SetGroupWholeBanAction",
]
