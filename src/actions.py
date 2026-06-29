"""snowluma_extension Actions。

每个 Action 都通过 `snowluma_adapter` 的 `send_snowluma_api(action, params)` 或者
向 core 发送含有 CommandType 的 MessageEnvelope 来调用 SnowLuma 功能。
并通过 go_activate() 读取配置开关决定是否向 LLM 暴露。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from src.app.plugin_system.api import adapter_api
from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base.action import BaseAction
from src.core.components.types import ChatType

logger = get_logger("snowluma_extension")

_SNOWLUMA_ADAPTER_SIGNATURE = "snowluma_adapter:adapter:snowluma_adapter"

if TYPE_CHECKING:
    pass


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
    """从 snowluma_extension 插件配置中获取 error_hint。"""

    try:
        from src.core.managers import get_plugin_manager
        plugin = get_plugin_manager().get_plugin("snowluma_extension")
        if plugin and plugin.config:
            config = plugin.config
            return str(getattr(getattr(config, "plugin", None), "error_hint", "") or "")  # type: ignore[union-attr]
    except Exception:
        pass
    return ""


async def _call_snowluma_api(
    *,
    action_name: str,
    params: dict[str, Any],
    timeout: float = 30.0,
) -> tuple[bool, str]:
    """调用 snowluma_adapter API 并统一解析响应。"""

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


class _SnowLumaBaseAction(BaseAction):
    """snowluma_extension Action 基类：提供通用激活判断。"""

    associated_platforms: list[str] = ["qq"]
    associated_types: list[str] = ["text"]

    async def go_activate(self) -> bool:  # noqa: D401
        """根据插件配置判定是否激活。"""

        config = getattr(self.plugin, "config", None)
        if config is None:
            return False

        plugin_section = getattr(config, "plugin", None)
        if plugin_section is None or not bool(getattr(plugin_section, "enabled", True)):
            return False

        return await self._feature_enabled(config)

    async def _feature_enabled(self, config: Any) -> bool:
        raise NotImplementedError


# ==============================================================================
# SnowLuma 扩展动作
# ==============================================================================

class MuteGroupMemberAction(_SnowLumaBaseAction):
    """群成员禁言。"""

    action_name: str = "mute_group_member"
    action_description: str = (
        "在当前群聊中对指定用户执行禁言。需要你为群主或管理员，且目标权限低于你。"
        "执行前请确认你的身份，如不确定可先用 get_group_member_info 查询你的角色。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: Any) -> bool:
        return bool(getattr(getattr(config, "features", None), "enable_mute", False))

    async def execute(
        self,
        user_id: Annotated[str, "要禁言的目标 QQ 号"],
        duration_seconds: Annotated[int, "禁言时长（秒），例如 600 表示 10 分钟"] = 600,
    ) -> tuple[bool, str]:
        group_id = _get_group_id_from_context(self)
        if not group_id:
            return False, "该动作只能在群聊上下文使用：未获取到 group_id。"

        if duration_seconds <= 0:
            return False, "duration_seconds 必须为正整数（单位：秒）。"

        params = {
            "group_id": _coerce_int_if_digit(group_id),
            "user_id": _coerce_int_if_digit(user_id),
            "duration": int(duration_seconds),
        }

        ok, msg = await _call_snowluma_api(action_name="set_group_ban", params=params)
        if ok:
            return True, f"已禁言用户 {user_id}（{duration_seconds} 秒）。"
        return False, msg


class UnmuteGroupMemberAction(_SnowLumaBaseAction):
    """群成员解除禁言。"""

    action_name: str = "unmute_group_member"
    action_description: str = "在当前群聊中解除指定用户的禁言（duration=0）。"
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: Any) -> bool:
        return bool(getattr(getattr(config, "features", None), "enable_unmute", False))

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
    """对指定消息添加表情回应。"""

    action_name: str = "react_to_message"
    action_description: str = (
        "对指定消息添加一个表情回应。"
        "使用前请先调用 get_qq_face_list 工具查询可用的表情列表，从中选择合适的表情。"
        "优先使用 emoji_id（数字字符串）作为参数，也可以使用表情名称（如'赞'、'爱心'、'笑哭'）。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: Any) -> bool:
        return bool(getattr(getattr(config, "features", None), "enable_react", False))

    async def execute(
        self,
        message_id: Annotated[str, "要回应的消息 ID"],
        emoji_id: Annotated[str, "表情 ID（数字，如 '76'）或表情名称（如 '赞'、'爱心'、'笑哭'）。优先使用 ID"],
    ) -> tuple[bool, str]:
        # 如果传入的不是纯数字，尝试从 QQ_FACE 表中按名称查找对应 ID
        raw_emoji = str(emoji_id).strip()
        resolved_emoji_id = raw_emoji

        if not raw_emoji.isdigit():
            from plugins.snowluma_adapter.src.event_models import QQ_FACE

            # 在表情映射值中搜索匹配的名称
            # QQ_FACE 格式: {"76": "[表情：赞]"}，用户可能传 "赞" 或 "[表情：赞]"
            search_key = raw_emoji
            if not search_key.startswith("[表情："):
                search_key = f"[表情：{search_key}]"

            found_id = None
            for face_id, face_name in QQ_FACE.items():
                if face_name == search_key or search_key in face_name:
                    found_id = face_id
                    break

            if found_id:
                resolved_emoji_id = found_id
            else:
                return False, f"无法识别的表情：{raw_emoji}。请使用 get_qq_face_list 工具查询可用的表情 ID。"

        params = {
            "message_id": _coerce_int_if_digit(message_id),
            "emoji_id": resolved_emoji_id,
            "set": True,
        }

        ok, msg = await _call_snowluma_api(action_name="set_msg_emoji_like", params=params)
        if ok:
            return True, f"已对消息 {message_id} 添加表情回应（emoji_id={resolved_emoji_id}）。"
        return False, msg


class SendFaceAction(_SnowLumaBaseAction):
    """发送 QQ 原生表情。"""

    action_name: str = "send_face"
    action_description: str = (
        "发送一个 QQ 原生表情到当前聊天中。"
        "使用前请先调用 get_qq_face_list 工具查询可用的表情列表，从中选择合适的表情。"
        "优先使用表情 ID（数字字符串）作为参数，也可以使用表情名称（如'赞'、'爱心'、'笑哭'）。"
        "可以单独发送表情，也可以和文字消息一起发送。"
    )
    chat_type: ChatType = ChatType.ALL

    async def _feature_enabled(self, config: Any) -> bool:
        return bool(getattr(getattr(config, "features", None), "enable_react", False))

    async def execute(
        self,
        face_id: Annotated[str, "表情 ID（数字，如 '76'）或表情名称（如 '赞'）。优先使用 ID"],
        text: Annotated[str, "可选，附带的文字消息。留空则只发表情"] = "",
    ) -> tuple[bool, str]:
        from src.core.models.message import Message, MessageType

        # 解析表情名称为 ID
        raw_face = str(face_id).strip()
        resolved_face_id = raw_face

        if not raw_face.isdigit():
            from plugins.snowluma_adapter.src.event_models import QQ_FACE

            search_key = raw_face
            if not search_key.startswith("[表情："):
                search_key = f"[表情：{search_key}]"

            found_id = None
            for fid, fname in QQ_FACE.items():
                if fname == search_key or search_key in fname:
                    found_id = fid
                    break

            if found_id:
                resolved_face_id = found_id
            else:
                return False, f"无法识别的表情：{raw_face}。请使用 get_qq_face_list 工具查询可用的表情 ID。"

        # 构建 Message，通过 extra["media"] 携带 face 消息段
        media_list: list[dict[str, Any]] = [{"type": "face", "data": resolved_face_id}]

        message = Message(
            content=text,
            processed_plain_text=text or None,
            message_type=MessageType.TEXT,
            platform=self.chat_stream.platform,
            chat_type=self.chat_stream.chat_type,
            stream_id=self.chat_stream.stream_id,
            media=media_list,
        )

        ok = await self._send_to_stream(message)
        if ok:
            from plugins.snowluma_adapter.src.event_models import QQ_FACE

            face_name = QQ_FACE.get(resolved_face_id, f"表情{resolved_face_id}")
            if text:
                return True, f"已发送表情 {face_name}（id={resolved_face_id}）和文字：{text}"
            return True, f"已发送表情 {face_name}（id={resolved_face_id}）。"
        return False, "发送表情失败。"


class PokeGroupMemberAction(_SnowLumaBaseAction):
    """戳一戳群成员。"""

    action_name: str = "poke_group_member"
    action_description: str = (
        "在当前群聊中戳一戳指定用户，可戳同一个人多次，也可戳多个不同的人。"
        "传入多个QQ号用逗号分隔即可批量戳不同的人。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: Any) -> bool:
        return bool(getattr(getattr(config, "features", None), "enable_poke", False))

    async def execute(
        self,
        user_ids: Annotated[str, "要戳一戳的目标QQ号，多个QQ号用英文逗号分隔（如 '123,456,789'）"],
        times: Annotated[int, "每人戳的次数，默认1次"] = 1,
        interval: Annotated[float, "每次戳之间的间隔（秒），默认0.5秒"] = 0.5,
    ) -> tuple[bool, str]:
        import asyncio

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

    action_name: str = "recall_message"
    action_description: str = (
        "撤回指定消息（需要机器人具备撤回权限；不同场景可能受时效限制）。"
    )
    chat_type: ChatType = ChatType.ALL

    async def _feature_enabled(self, config: Any) -> bool:
        return bool(getattr(getattr(config, "features", None), "enable_recall", False))

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

    action_name: str = "group_sign"
    action_description: str = "在当前群聊中执行群打卡。"
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: Any) -> bool:
        return bool(getattr(getattr(config, "features", None), "enable_group_sign", False))

    async def execute(self) -> tuple[bool, str]:
        group_id = _get_group_id_from_context(self)
        if not group_id:
            return False, "该动作只能在群聊上下文使用：未获取到 group_id。"

        params = {"group_id": _coerce_int_if_digit(group_id)}

        ok, msg = await _call_snowluma_api(action_name="set_group_sign", params=params)
        if ok:
            return True, "已执行群打卡。"

        return False, msg


class KickGroupMemberAction(_SnowLumaBaseAction):
    """踢出群成员。"""

    action_name: str = "kick_group_member"
    action_description: str = (
        "在当前群聊中踢出指定用户。需要你为群主或管理员，且目标权限低于你。"
        "执行前请确认你的身份，如不确定可先用 get_group_member_info 查询你的角色。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: Any) -> bool:
        return bool(getattr(getattr(config, "features", None), "enable_kick", False))

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
# 新增的 SnowLuma 特有管理 Action
# ==============================================================================

class SetGroupNameAction(_SnowLumaBaseAction):
    """修改群名。"""

    action_name: str = "set_group_name"
    action_description: str = (
        "修改当前群聊的名称。需要你为群主或管理员。"
        "执行前请确认你的身份，如不确定可先用 get_group_member_info 查询你的角色。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: Any) -> bool:
        return bool(getattr(getattr(config, "features", None), "enable_set_group_name", False))

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

    action_name: str = "set_group_card"
    action_description: str = (
        "修改当前群聊中指定用户的群名片（也叫群昵称，即在群内显示的昵称名称）。"
        "修改自己的群名片不需要权限，修改他人的群名片需要你为群主或管理员，"
        "若需修改他人的群名片且不确定自身权限，可先用 get_group_member_info 查询你的角色。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: Any) -> bool:
        return bool(getattr(getattr(config, "features", None), "enable_set_group_card", False))

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

    action_name: str = "set_group_special_title"
    action_description: str = (
        "修改当前群聊中指定用户的群专属头衔。需要你为群主（管理员不可）。"
        "执行前请确认你的身份，如不确定可先用 get_group_member_info 查询你的角色。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: Any) -> bool:
        return bool(getattr(getattr(config, "features", None), "enable_set_group_special_title", False))

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

    action_name: str = "send_group_notice"
    action_description: str = (
        "在当前群聊中发布一条群公告。可附带图片。群公告会展示在群公告页面，所有群成员可见。"
        "需要你为群主或管理员。执行前请确认你的身份，如不确定可先用 get_group_member_info 查询你的角色。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: Any) -> bool:
        return bool(getattr(getattr(config, "features", None), "enable_send_group_notice", False))

    async def execute(
        self,
        content: Annotated[str, "群公告的正文内容"],
        image: Annotated[str, "公告附带图片的URL或Base64（可选，留空则无图）"] = "",
    ) -> tuple[bool, str]:
        group_id = _get_group_id_from_context(self)
        if not group_id:
            return False, "该动作只能在群聊上下文使用：未获取到 group_id。"

        params: dict[str, Any] = {
            "group_id": _coerce_int_if_digit(group_id),
            "content": str(content),
        }
        if image:
            params["image"] = str(image)

        ok, msg = await _call_snowluma_api(action_name="_send_group_notice", params=params)
        if ok:
            return True, "已成功发布群公告。"
        return False, msg


class DeleteGroupNoticeAction(_SnowLumaBaseAction):
    """删除群公告。"""

    action_name: str = "delete_group_notice"
    action_description: str = (
        "删除当前群聊中的指定群公告。需要提供公告ID（notice_id），"
        "可通过 get_group_notice 工具获取群公告列表来拿到每条公告的ID。"
        "需要你为群主或管理员。执行前请确认你的身份，如不确定可先用 get_group_member_info 查询你的角色。"
    )
    chat_type: ChatType = ChatType.GROUP

    async def _feature_enabled(self, config: Any) -> bool:
        return bool(getattr(getattr(config, "features", None), "enable_delete_group_notice", False))

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


__all__ = [
    "MuteGroupMemberAction",
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
]
