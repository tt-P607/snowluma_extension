"""snowluma_extension Tools。

Tool 组件侧重于"查询"功能，供 LLM 调用以获取信息。
与 Action 不同，Tool 的返回值会直接展示给 LLM。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from src.app.plugin_system.api import adapter_api
from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base.tool import BaseTool
from src.core.components.types import ChatType

from .actions import _coerce_int_if_digit, _format_snowluma_failure, _get_error_hint, _SNOWLUMA_ADAPTER_SIGNATURE

logger = get_logger("snowluma_extension")


class GetGroupMemberInfoTool(BaseTool):
    """获取群成员信息。"""

    name: str = "get_group_member_info"
    description: str = (
        "获取当前群聊中指定成员的详细信息。"
        "返回的信息包括：QQ号、昵称、群名片（群昵称）、角色身份（群主owner/管理员admin/普通成员member）、"
        "专属头衔、群等级、性别、年龄、入群时间、最后发言时间。"
        "常用于：查询某人的群内身份和权限、查看自己的角色以确认是否有管理权限、"
        "查看群成员的头衔和名片等。传入自己的QQ号即可查询自身权限。"
    )
    chat_type: ChatType = ChatType.GROUP
    associated_platforms: list[str] = ["qq"]

    async def execute(
        self,
        user_id: Annotated[str, "要查询的目标 QQ 号"],
        no_cache: Annotated[bool, "是否不使用缓存（true=强制从服务器获取最新数据）"] = False,
    ) -> tuple[bool, str]:
        group_id = _get_group_id_from_context_tool(self)
        if not group_id:
            return False, "该工具只能在群聊上下文使用：未获取到 group_id。"

        params = {
            "group_id": _coerce_int_if_digit(group_id),
            "user_id": _coerce_int_if_digit(user_id),
            "no_cache": bool(no_cache),
        }

        adapter = adapter_api.get_adapter(_SNOWLUMA_ADAPTER_SIGNATURE)
        if adapter is None:
            return False, "snowluma_adapter 未启动：请先启用并启动 snowluma_adapter 插件。"

        if not hasattr(adapter, "send_snowluma_api"):
            return False, "snowluma_adapter 不支持 send_snowluma_api：请确认 snowluma_adapter 版本兼容。"

        logger.debug(f"调用 SnowLuma API: action=get_group_member_info, params={params}")

        try:
            resp = await adapter.send_snowluma_api("get_group_member_info", params, timeout=30.0)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.error(f"SnowLuma API 调用异常: action=get_group_member_info, error={exc}")
            return False, f"调用 SnowLuma API 异常：{exc}"

        logger.debug(f"SnowLuma API 响应: action=get_group_member_info, resp={resp}")

        status = str(resp.get("status") or "").strip().lower()
        retcode = resp.get("retcode")
        if status != "ok" or (retcode != 0 and retcode is not None):
            logger.warning(f"SnowLuma API 调用失败: action=get_group_member_info, status={status}, retcode={retcode}")
            return False, _format_snowluma_failure("get_group_member_info", resp, _get_error_hint())

        data = resp.get("data") or {}

        role_map = {"owner": "群主", "admin": "管理员", "member": "普通成员"}
        sex_map = {"male": "男", "female": "女", "unknown": "未知"}

        nickname = data.get("nickname", "未知")
        card = data.get("card", "")
        role = role_map.get(data.get("role", ""), data.get("role", "未知"))
        title = data.get("title", "")
        level = data.get("level", "")
        sex = sex_map.get(data.get("sex", ""), data.get("sex", "未知"))
        age = data.get("age", 0)
        join_time = data.get("join_time", 0)

        lines: list[str] = [
            f"QQ号：{user_id}",
            f"昵称：{nickname}",
        ]
        if card:
            lines.append(f"群名片：{card}")
        lines.append(f"角色：{role}")
        if title:
            lines.append(f"专属头衔：{title}")
        if level:
            lines.append(f"群等级：{level}")
        lines.append(f"性别：{sex}")
        if age:
            lines.append(f"年龄：{age}")
        if join_time:
            lines.append(f"入群时间：{datetime.fromtimestamp(join_time).strftime('%Y-%m-%d %H:%M:%S')}")

        # bot 主动查询自己时，同步刷新该群的 bot_role reminder（忽略 TTL 强制更新）
        await _maybe_refresh_bot_role_on_self_query(
            tool=self,
            group_id=group_id,
            user_id=user_id,
        )

        logger.info(f"SnowLuma API 调用成功: action=get_group_member_info, user_id={user_id}")
        return True, "\n".join(lines)


def _get_group_id_from_context_tool(tool: BaseTool) -> Any:
    """从 Tool 的触发消息中提取 group_id。"""

    msg = tool.trigger_message
    if msg is not None:
        group_id = msg.extra.get("group_id") or msg.extra.get("target_group_id")
        if group_id is not None:
            return group_id

    return None


async def _maybe_refresh_bot_role_on_self_query(
    *,
    tool: BaseTool,
    group_id: Any,
    user_id: str,
) -> None:
    """bot 主动查询自己时，强制刷新该群的 bot_role reminder 与 TTL。

    Args:
        tool: 当前工具实例。
        group_id: 群号。
        user_id: 被查询的 QQ 号。
    """

    if not group_id or not user_id:
        return

    try:
        bot_info = await adapter_api.get_bot_info_by_platform("qq")
    except Exception:
        bot_info = None
    if not bot_info or not bot_info.get("bot_id"):
        return
    if str(user_id) != str(bot_info["bot_id"]):
        return

    config = getattr(tool.plugin, "config", None)
    bot_role_cfg = getattr(config, "bot_role", None) if config is not None else None
    if bot_role_cfg is None or not bool(getattr(bot_role_cfg, "enable", False)):
        return

    stream_id = tool.get_current_stream_id()
    if not stream_id:
        return

    from .bot_role_reminder import fetch_and_update_bot_role

    try:
        await fetch_and_update_bot_role(
            stream_id=stream_id,
            group_id=str(group_id),
            config=config,
            force=True,
            source="self_query",
        )
    except Exception as exc:
        logger.warning(f"bot 主动查询自己时刷新 bot_role reminder 失败: {exc}")


class GetGroupNoticeTool(BaseTool):
    """获取群公告列表。"""

    name: str = "get_group_notice"
    description: str = (
        "获取当前群聊的所有群公告列表，包括每条公告的完整正文内容、发布者QQ、"
        "发布时间、阅读数、是否含图片、公告ID（notice_id）、是否置顶、公告类型和是否需要回执确认。"
        "公告类型：0=普通公告,1=弹窗推送,2=新成员推送,3=改名引导。"
        "获取群公告不需要特殊权限，但发送和删除群公告需要你为群主或管理员。"
        "返回的 notice_id 可用于删除群公告。"
    )
    chat_type: ChatType = ChatType.GROUP
    associated_platforms: list[str] = ["qq"]

    async def execute(self) -> tuple[bool, str]:
        group_id = _get_group_id_from_context_tool(self)
        if not group_id:
            return False, "该工具只能在群聊上下文使用：未获取到 group_id。"

        params = {
            "group_id": _coerce_int_if_digit(group_id),
        }

        adapter = adapter_api.get_adapter(_SNOWLUMA_ADAPTER_SIGNATURE)
        if adapter is None:
            return False, "snowluma_adapter 未启动：请先启用并启动 snowluma_adapter 插件。"

        if not hasattr(adapter, "send_snowluma_api"):
            return False, "snowluma_adapter 不支持 send_snowluma_api"

        logger.debug(f"调用 SnowLuma API: action=_get_group_notice, params={params}")

        try:
            resp = await adapter.send_snowluma_api("_get_group_notice", params, timeout=30.0)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.error(f"SnowLuma API 调用异常: action=_get_group_notice, error={exc}")
            return False, f"调用 SnowLuma API 异常：{exc}"

        logger.debug(f"SnowLuma API 响应: action=_get_group_notice, resp={resp}")

        status = str(resp.get("status") or "").strip().lower()
        retcode = resp.get("retcode")
        if status != "ok" or (retcode != 0 and retcode is not None):
            logger.warning(f"SnowLuma API 调用失败: action=_get_group_notice, status={status}, retcode={retcode}")
            return False, _format_snowluma_failure("_get_group_notice", resp, _get_error_hint())

        notices = resp.get("data") or []
        if not notices:
            return True, "当前群聊没有群公告。"

        from datetime import datetime

        type_names = {0: "普通公告", 1: "弹窗推送", 2: "新成员推送", 3: "改名引导"}

        lines: list[str] = []
        for i, notice in enumerate(notices, 1):
            notice_id = notice.get("notice_id", "")
            sender_id = notice.get("sender_id", "")
            publish_time = notice.get("publish_time", 0)
            text = notice.get("message", {}).get("text", "")
            read_num = notice.get("read_num", 0)
            has_image = bool(notice.get("message", {}).get("image"))
            is_pinned = notice.get("pinned", False)
            notice_type = notice.get("type", 0)
            need_confirm = notice.get("confirm_required", False)

            time_str = datetime.fromtimestamp(publish_time).strftime("%Y-%m-%d %H:%M") if publish_time else "未知"

            lines.append(f"--- 公告 {i} ---")
            lines.append(f"公告ID：{notice_id}")
            lines.append(f"发布者：{sender_id}")
            lines.append(f"发布时间：{time_str}")
            lines.append(f"阅读数：{read_num}")
            if is_pinned:
                lines.append("置顶：是")
            if notice_type and notice_type != 0:
                lines.append(f"类型：{type_names.get(notice_type, str(notice_type))}")
            if need_confirm:
                lines.append("需回执确认：是")
            if has_image:
                lines.append("含图片：是")
            lines.append(f"正文：{text}")
            lines.append("")

        logger.info(f"SnowLuma API 调用成功: action=_get_group_notice, count={len(notices)}")
        return True, "\n".join(lines)


class GetQQFaceListTool(BaseTool):
    """查询 QQ 表情列表。"""

    name: str = "get_qq_face_list"
    description: str = (
        "查询 QQ 可用表情列表，返回所有表情的 ID 和名称映射。"
        "在调用 react_to_message 贴表情之前，先用本工具查询可用的表情，"
        "然后选择合适的表情 ID 传给 react_to_message。"
        "\n\n此外，你也可以在回复文本中直接插入表情标记来发送 QQ 原生表情："
        "在文本任意位置写 【face:ID】（注意是中文方括号【】），系统会自动转换为真实表情发送。"
        "例如：\"谢谢啦 【face:353】\"、\"好开心 【face:21】\"。"
        "支持同一条消息插入多个标记。"
        "\n\n重要规则："
        "1. 必须使用本工具返回的表情 ID，严禁自行编造、猜测或修改 ID。"
        "2. 只能使用上面列出的 ID，找不到合适的就不要用。"
        "3. 文本中插入表情时必须用 【face:ID】 格式（中文方括号），不要用 [face:ID] 或 [表情：名称]。"
    )
    associated_platforms: list[str] = ["qq"]

    async def execute(self) -> tuple[bool, str]:
        """返回完整的 QQ 表情映射表。"""
        from plugins.snowluma_adapter.src.event_models import QQ_FACE

        lines: list[str] = ["QQ 表情列表（ID: 名称）：", ""]
        for face_id, face_name in QQ_FACE.items():
            # face_name 格式: "[表情：赞]"，提取中间名称
            if face_name.startswith("[表情：") and face_name.endswith("]"):
                display_name = face_name[4:-1]
            else:
                display_name = face_name
            lines.append(f"  {face_id}: {display_name}")

        lines.append("")
        lines.append("使用说明：")
        lines.append("1. 在文本中插入表情：写 【face:ID】（中文方括号），如 【face:353】")
        lines.append("2. 调用 react_to_message 时 emoji_id 传表情 ID（数字），如 '353'、'21'")
        lines.append("3. 只能使用上面列出的 ID，严禁自行编造或猜测")

        return True, "\n".join(lines)


class GetEssenceMsgListTool(BaseTool):
    """获取群精华消息列表。"""

    name: str = "get_essence_msg_list"
    description: str = (
        "获取当前群聊的精华消息列表。"
        "返回每条精华消息的消息ID、发送者QQ号、昵称、发送时间和消息内容。"
    )
    chat_type: ChatType = ChatType.GROUP
    associated_platforms: list[str] = ["qq"]

    async def execute(self) -> tuple[bool, str]:
        """返回群精华消息列表。"""
        group_id = _get_group_id_from_context_tool(self)
        if not group_id:
            return False, "该工具只能在群聊上下文使用：未获取到 group_id。"

        params = {"group_id": _coerce_int_if_digit(group_id)}

        adapter = adapter_api.get_adapter(_SNOWLUMA_ADAPTER_SIGNATURE)
        if adapter is None:
            return False, "snowluma_adapter 未启动。"
        if not hasattr(adapter, "send_snowluma_api"):
            return False, "snowluma_adapter 不支持 send_snowluma_api。"

        try:
            resp = await adapter.send_snowluma_api("get_essence_msg_list", params, timeout=30.0)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.error(f"获取精华消息列表失败: {exc}")
            return False, f"获取精华消息列表异常：{exc}"

        data = resp.get("data") if isinstance(resp, dict) else None
        if not data or not isinstance(data, dict):
            return False, "获取精华消息列表失败：返回数据为空。"

        msg_list = data.get("essence_list") or data.get("messages") or []
        if not msg_list:
            return True, "当前群没有精华消息。"

        lines: list[str] = [f"群精华消息列表（共 {len(msg_list)} 条）："]
        for i, msg in enumerate(msg_list, 1):
            msg_id = msg.get("message_id", "")
            sender_uid = msg.get("sender_id") or msg.get("user_id", "")
            sender_nick = msg.get("sender_nick") or msg.get("nickname", "")
            msg_time = msg.get("sender_time") or msg.get("time", "")
            content = msg.get("content") or msg.get("raw_message", "")
            if msg_time:
                try:
                    time_str = datetime.fromtimestamp(int(msg_time)).strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError, OSError):
                    time_str = str(msg_time)
            else:
                time_str = "未知时间"
            lines.append(f"--- 精华 {i} ---")
            lines.append(f"消息ID：{msg_id}")
            lines.append(f"发送者：{sender_nick}({sender_uid})")
            lines.append(f"时间：{time_str}")
            lines.append(f"内容：{content}")
            lines.append("")

        logger.info(f"获取精华消息列表成功: count={len(msg_list)}")
        return True, "\n".join(lines)


class GetGroupHonorInfoTool(BaseTool):
    """获取群荣誉信息。"""

    name: str = "get_group_honor_info"
    description: str = (
        "获取当前群聊的荣誉信息，包括龙王、群聊之火、群聊炽焰等。"
        "龙王是当日发言最多的人；群聊之火是连续发消息的人；群聊炽焰是长期连续发消息的人。"
    )
    chat_type: ChatType = ChatType.GROUP
    associated_platforms: list[str] = ["qq"]

    async def execute(self) -> tuple[bool, str]:
        """返回群荣誉信息。"""
        group_id = _get_group_id_from_context_tool(self)
        if not group_id:
            return False, "该工具只能在群聊上下文使用：未获取到 group_id。"

        params = {
            "group_id": _coerce_int_if_digit(group_id),
            "type": "all",
        }

        adapter = adapter_api.get_adapter(_SNOWLUMA_ADAPTER_SIGNATURE)
        if adapter is None:
            return False, "snowluma_adapter 未启动。"
        if not hasattr(adapter, "send_snowluma_api"):
            return False, "snowluma_adapter 不支持 send_snowluma_api。"

        try:
            resp = await adapter.send_snowluma_api("get_group_honor_info", params, timeout=30.0)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.error(f"获取群荣誉信息失败: {exc}")
            return False, f"获取群荣誉信息异常：{exc}"

        data = resp.get("data") if isinstance(resp, dict) else None
        if not data or not isinstance(data, dict):
            return False, "获取群荣誉信息失败：返回数据为空。"

        lines: list[str] = []

        current_talkative = data.get("current_talkative")
        if current_talkative:
            lines.append("=== 当前龙王 ===")
            lines.append(f"{current_talkative.get('nickname', '')}({current_talkative.get('user_id', '')})：{current_talkative.get('description', '')}")
            lines.append("")

        talkative_list = data.get("talkative_list") or []
        if talkative_list:
            lines.append(f"=== 历史龙王（共 {len(talkative_list)} 位）===")
            for i, item in enumerate(talkative_list[:10], 1):
                lines.append(f"{i}. {item.get('nickname', '')}({item.get('user_id', '')})：{item.get('description', '')}")
            if len(talkative_list) > 10:
                lines.append(f"... 还有 {len(talkative_list) - 10} 位")
            lines.append("")

        performer_list = data.get("performer_list") or []
        if performer_list:
            lines.append(f"=== 群聊之火（连续发消息，共 {len(performer_list)} 位）===")
            for i, item in enumerate(performer_list[:10], 1):
                lines.append(f"{i}. {item.get('nickname', '')}({item.get('user_id', '')})：{item.get('description', '')}")
            if len(performer_list) > 10:
                lines.append(f"... 还有 {len(performer_list) - 10} 位")
            lines.append("")

        legend_list = data.get("legend_list") or []
        if legend_list:
            lines.append(f"=== 群聊炽焰（长期连续发消息，共 {len(legend_list)} 位）===")
            for i, item in enumerate(legend_list[:10], 1):
                lines.append(f"{i}. {item.get('nickname', '')}({item.get('user_id', '')})：{item.get('description', '')}")
            if len(legend_list) > 10:
                lines.append(f"... 还有 {len(legend_list) - 10} 位")
            lines.append("")

        if not lines:
            return True, "当前群没有任何荣誉信息。"

        logger.info(f"获取群荣誉信息成功: talkative={len(talkative_list)}, performer={len(performer_list)}, legend={len(legend_list)}")
        return True, "\n".join(lines)


class GetGroupShutListTool(BaseTool):
    """获取群禁言列表。"""

    name: str = "get_group_shut_list"
    description: str = (
        "获取当前群聊中仍在禁言中的成员列表。"
        "返回每个被禁言成员的 QQ 号、昵称和禁言到期时间。"
    )
    chat_type: ChatType = ChatType.GROUP
    associated_platforms: list[str] = ["qq"]

    async def execute(self) -> tuple[bool, str]:
        """返回群禁言列表。"""
        group_id = _get_group_id_from_context_tool(self)
        if not group_id:
            return False, "该工具只能在群聊上下文使用：未获取到 group_id。"

        params = {"group_id": _coerce_int_if_digit(group_id)}

        adapter = adapter_api.get_adapter(_SNOWLUMA_ADAPTER_SIGNATURE)
        if adapter is None:
            return False, "snowluma_adapter 未启动。"
        if not hasattr(adapter, "send_snowluma_api"):
            return False, "snowluma_adapter 不支持 send_snowluma_api。"

        try:
            resp = await adapter.send_snowluma_api("get_group_shut_list", params, timeout=30.0)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.error(f"获取群禁言列表失败: {exc}")
            return False, f"获取群禁言列表异常：{exc}"

        data = resp.get("data") if isinstance(resp, dict) else None
        if not data:
            return True, "当前群没有禁言中的成员。"

        shut_list = data if isinstance(data, list) else data.get("list") or data.get("members") or []
        if not shut_list:
            return True, "当前群没有禁言中的成员。"

        lines: list[str] = [f"群禁言列表（共 {len(shut_list)} 人）："]
        for i, item in enumerate(shut_list, 1):
            uid = item.get("user_id", "")
            nick = item.get("nickname", "")
            shut_time = item.get("shut_up_time", 0)
            if shut_time:
                try:
                    time_str = datetime.fromtimestamp(int(shut_time)).strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError, OSError):
                    time_str = str(shut_time)
            else:
                time_str = "未知"
            lines.append(f"{i}. {nick}({uid}) - 解禁时间：{time_str}")

        logger.info(f"获取群禁言列表成功: count={len(shut_list)}")
        return True, "\n".join(lines)


class GetGroupInfoTool(BaseTool):
    """获取群信息。"""

    name: str = "get_group_info"
    description: str = (
        "获取当前群聊的基本信息，包括群名、群号、当前成员数、成员上限、建群时间、群等级和群简介。"
        "常用于：了解群的整体概况、查看群人数和上限、获取群名等。"
    )
    chat_type: ChatType = ChatType.GROUP
    associated_platforms: list[str] = ["qq"]

    async def execute(
        self,
        no_cache: Annotated[bool, "是否不使用缓存（true=强制从服务器获取最新数据）"] = False,
    ) -> tuple[bool, str]:
        """返回群基本信息。"""
        group_id = _get_group_id_from_context_tool(self)
        if not group_id:
            return False, "该工具只能在群聊上下文使用：未获取到 group_id。"

        params = {
            "group_id": _coerce_int_if_digit(group_id),
            "no_cache": bool(no_cache),
        }

        adapter = adapter_api.get_adapter(_SNOWLUMA_ADAPTER_SIGNATURE)
        if adapter is None:
            return False, "snowluma_adapter 未启动。"
        if not hasattr(adapter, "send_snowluma_api"):
            return False, "snowluma_adapter 不支持 send_snowluma_api。"

        try:
            resp = await adapter.send_snowluma_api("get_group_info", params, timeout=30.0)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.error(f"获取群信息失败: {exc}")
            return False, f"获取群信息异常：{exc}"

        status = str(resp.get("status") or "").strip().lower()
        retcode = resp.get("retcode")
        if status != "ok" or (retcode != 0 and retcode is not None):
            return False, _format_snowluma_failure("get_group_info", resp, _get_error_hint())

        data = resp.get("data") or {}

        group_name = data.get("group_name", "未知")
        member_count = data.get("member_count", 0)
        max_member_count = data.get("max_member_count", 0)
        group_create_time = data.get("group_create_time", 0)
        group_level = data.get("group_level", 0)
        group_memo = data.get("group_memo", "")

        lines: list[str] = [
            f"群号：{group_id}",
            f"群名：{group_name}",
            f"成员数：{member_count}",
            f"成员上限：{max_member_count}",
        ]
        if group_level:
            lines.append(f"群等级：{group_level}")
        if group_create_time:
            try:
                lines.append(f"建群时间：{datetime.fromtimestamp(int(group_create_time)).strftime('%Y-%m-%d %H:%M:%S')}")
            except (ValueError, TypeError, OSError):
                lines.append(f"建群时间戳：{group_create_time}")
        if group_memo:
            lines.append(f"群简介：{group_memo}")

        logger.info(f"获取群信息成功: group_id={group_id}")
        return True, "\n".join(lines)


class GetGroupMemberListTool(BaseTool):
    """获取群成员列表。"""

    name: str = "get_group_member_list"
    description: str = (
        "获取当前群聊的全部成员列表。"
        "返回每个成员的 QQ 号、昵称、群名片、角色身份（群主/管理员/普通成员）、"
        "专属头衔、群等级、性别、入群时间和最后发言时间。"
        "常用于：查看群内所有成员、统计人数、查找管理员等。"
        "注意：大群成员列表可能较长，返回内容可能被截断。"
    )
    chat_type: ChatType = ChatType.GROUP
    associated_platforms: list[str] = ["qq"]

    async def execute(
        self,
        no_cache: Annotated[bool, "是否不使用缓存（true=强制从服务器获取最新数据）"] = False,
    ) -> tuple[bool, str]:
        """返回群成员列表。"""
        group_id = _get_group_id_from_context_tool(self)
        if not group_id:
            return False, "该工具只能在群聊上下文使用：未获取到 group_id。"

        params = {
            "group_id": _coerce_int_if_digit(group_id),
            "no_cache": bool(no_cache),
        }

        adapter = adapter_api.get_adapter(_SNOWLUMA_ADAPTER_SIGNATURE)
        if adapter is None:
            return False, "snowluma_adapter 未启动。"
        if not hasattr(adapter, "send_snowluma_api"):
            return False, "snowluma_adapter 不支持 send_snowluma_api。"

        try:
            resp = await adapter.send_snowluma_api("get_group_member_list", params, timeout=60.0)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.error(f"获取群成员列表失败: {exc}")
            return False, f"获取群成员列表异常：{exc}"

        status = str(resp.get("status") or "").strip().lower()
        retcode = resp.get("retcode")
        if status != "ok" or (retcode != 0 and retcode is not None):
            return False, _format_snowluma_failure("get_group_member_list", resp, _get_error_hint())

        members = resp.get("data") or []
        if not members:
            return True, "当前群没有成员数据。"

        role_map = {"owner": "群主", "admin": "管理员", "member": "普通成员"}
        sex_map = {"male": "男", "female": "女", "unknown": "未知"}

        lines: list[str] = [f"群成员列表（共 {len(members)} 人）："]

        # 按角色排序：群主 > 管理员 > 普通成员
        role_order = {"owner": 0, "admin": 1, "member": 2}
        members_sorted = sorted(members, key=lambda m: role_order.get(m.get("role", "member"), 2))

        for i, m in enumerate(members_sorted, 1):
            uid = m.get("user_id", "")
            nickname = m.get("nickname", "未知")
            card = m.get("card", "")
            role = role_map.get(m.get("role", ""), m.get("role", "未知"))
            title = m.get("title", "")
            level = m.get("level", "")
            sex = sex_map.get(m.get("sex", ""), m.get("sex", "未知"))
            join_time = m.get("join_time", 0)

            display_name = card if card else nickname
            entry = f"{i}. {display_name}({uid}) [{role}]"
            if title:
                entry += f" 头衔:{title}"
            if level:
                entry += f" Lv:{level}"
            if sex != "未知":
                entry += f" {sex}"
            if join_time:
                try:
                    entry += f" 入群:{datetime.fromtimestamp(int(join_time)).strftime('%Y-%m-%d')}"
                except (ValueError, TypeError, OSError):
                    pass
            lines.append(entry)

        logger.info(f"获取群成员列表成功: group_id={group_id}, count={len(members)}")
        return True, "\n".join(lines)


class GetBotMessagesTool(BaseTool):
    """查询 bot 自己最近发送的消息列表。"""

    name: str = "get_bot_messages"
    description: str = (
        "查询你自己（bot）最近发送的消息列表，返回每条消息的 message_id 和内容摘要。"
        "结果按时间倒序排列（最新的在最前面）。"
        "主要用于获取 message_id 以便后续撤回消息（recall_message）、贴表情回应（react_to_message）等操作。"
        "注意：QQ 撤回消息有 2 分钟时效限制，超过时效的消息即使拿到 message_id 也无法撤回。"
    )
    chat_type: ChatType = ChatType.ALL
    associated_platforms: list[str] = ["qq"]

    async def execute(
        self,
        count: Annotated[int, "查询的消息数量，默认5条，最大20条"] = 5,
    ) -> tuple[bool, str]:
        from src.core.managers.stream_manager import get_stream_manager

        stream_id = self.get_current_stream_id()
        if not stream_id:
            return False, "无法获取当前聊天流 ID。"

        chat_stream = get_stream_manager()._streams.get(stream_id)  # noqa: SLF001
        if chat_stream is None:
            return False, "当前聊天流不存在，无法查询历史消息。"

        context = chat_stream.context

        # 从历史消息中逆序查找 bot 自己发送的消息
        bot_messages = [
            msg
            for msg in reversed(context.history_messages)
            if msg.sender_role == "bot" and msg.message_id
        ]

        if not bot_messages:
            return True, "你最近没有发送过消息。"

        # 限制数量
        count = max(1, min(count, 20))
        bot_messages = bot_messages[:count]

        lines: list[str] = []
        for msg in bot_messages:
            # 内容摘要
            if msg.processed_plain_text:
                text = msg.processed_plain_text[:60]
            else:
                text = str(msg.content)[:60]

            # 时间格式化
            time_str = ""
            try:
                from datetime import datetime as _dt

                if isinstance(msg.time, (int, float)):
                    ts = float(msg.time)
                elif isinstance(msg.time, _dt):
                    ts = msg.time.timestamp()
                else:
                    ts = 0.0
                time_str = _dt.fromtimestamp(ts).strftime("%H:%M:%S")
            except (ValueError, TypeError, OSError):
                time_str = "未知时间"

            lines.append(f"[{time_str}] message_id={msg.message_id} 内容: {text}")

        result = f"你最近发送了 {len(bot_messages)} 条消息：\n" + "\n".join(lines)
        result += "\n\n提示：使用 recall_message Action 并传入 message_id 即可撤回对应消息。"
        return True, result


__all__ = [
    "GetGroupMemberInfoTool",
    "GetGroupNoticeTool",
    "GetQQFaceListTool",
    "GetEssenceMsgListTool",
    "GetGroupHonorInfoTool",
    "GetGroupShutListTool",
    "GetGroupInfoTool",
    "GetGroupMemberListTool",
    "GetBotMessagesTool",
]
