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

    tool_name: str = "get_group_member_info"
    tool_description: str = (
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
        last_sent_time = data.get("last_sent_time", 0)

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
        if last_sent_time:
            lines.append(f"最后发言：{datetime.fromtimestamp(last_sent_time).strftime('%Y-%m-%d %H:%M:%S')}")

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


class GetGroupNoticeTool(BaseTool):
    """获取群公告列表。"""

    tool_name: str = "get_group_notice"
    tool_description: str = (
        "获取当前群聊的所有群公告列表，包括每条公告的完整正文内容、发布者QQ、"
        "发布时间、阅读数、是否含图片和公告ID（notice_id）。"
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

        lines: list[str] = []
        for i, notice in enumerate(notices, 1):
            notice_id = notice.get("notice_id", "")
            sender_id = notice.get("sender_id", "")
            publish_time = notice.get("publish_time", 0)
            text = notice.get("message", {}).get("text", "")
            read_num = notice.get("read_num", 0)
            has_image = bool(notice.get("message", {}).get("image"))

            time_str = datetime.fromtimestamp(publish_time).strftime("%Y-%m-%d %H:%M") if publish_time else "未知"

            lines.append(f"--- 公告 {i} ---")
            lines.append(f"公告ID：{notice_id}")
            lines.append(f"发布者：{sender_id}")
            lines.append(f"发布时间：{time_str}")
            lines.append(f"阅读数：{read_num}")
            if has_image:
                lines.append("含图片：是")
            lines.append(f"正文：{text}")
            lines.append("")

        logger.info(f"SnowLuma API 调用成功: action=_get_group_notice, count={len(notices)}")
        return True, "\n".join(lines)


class GetQQFaceListTool(BaseTool):
    """查询 QQ 表情列表。"""

    tool_name: str = "get_qq_face_list"
    tool_description: str = (
        "查询 QQ 可用表情列表，返回所有表情的 ID 和名称映射。"
        "在调用 react_to_message 贴表情之前，先用本工具查询可用的表情，"
        "然后选择合适的表情 ID 传给 react_to_message。"
    )
    associated_platforms: list[str] = ["qq"]

    async def execute(self) -> tuple[bool, str]:
        """返回完整的 QQ 表情映射表。"""
        from plugins.snowluma_adapter.src.event_models import QQ_FACE

        lines: list[str] = ["QQ 表情列表（emoji_id: 表情名称）：", ""]
        for face_id, face_name in QQ_FACE.items():
            # face_name 格式: "[表情：赞]"，提取中间名称
            display_name = face_name
            lines.append(f"  {face_id}: {display_name}")

        lines.append("")
        lines.append("提示：调用 react_to_message 时优先使用表情 ID（数字）。")

        return True, "\n".join(lines)


class GetEssenceMsgListTool(BaseTool):
    """获取群精华消息列表。"""

    tool_name: str = "get_essence_msg_list"
    tool_description: str = (
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

    tool_name: str = "get_group_honor_info"
    tool_description: str = (
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

    tool_name: str = "get_group_shut_list"
    tool_description: str = (
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


__all__ = [
    "GetGroupMemberInfoTool",
    "GetGroupNoticeTool",
    "GetQQFaceListTool",
    "GetEssenceMsgListTool",
    "GetGroupHonorInfoTool",
    "GetGroupShutListTool",
]
