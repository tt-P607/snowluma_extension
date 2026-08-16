"""Bot 身份与群权限自动注入 System Reminder。

- 群消息到达时，后台查询 bot 自己在当前群的完整资料与群荣誉，
  按 8 小时 TTL 缓存刷新，并将结果写入流私有 reminder（bucket: actor）。
- bot 主动通过 get_group_member_info 查询自己时，也会触发同一刷新逻辑，
  使 LLM 上下文始终保持最新的权限信息，无需 bot 自行记忆。
"""

from __future__ import annotations

import time
from typing import Any

from src.app.plugin_system.api import adapter_api, prompt_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import EventType, Message, SystemReminderBucket
from src.core.prompt import SystemReminderInsertType
from src.kernel.concurrency import get_task_manager
from src.kernel.event import EventDecision

from .actions import _coerce_int_if_digit

logger = get_logger("snowluma_extension")

_SNOWLUMA_ADAPTER_SIGNATURE = "snowluma_adapter:adapter:snowluma_adapter"

# reminder 名称，在流私有 bucket 内唯一（覆盖式更新）
_REMINDER_NAME = "bot_role"

# 每群身份/荣誉缓存 TTL（秒）：默认 8 小时，可被配置覆盖
_DEFAULT_TTL_SECONDS = 8 * 60 * 60

# 群角色 → 角色显示名
_ROLE_MAP = {"owner": "群主", "admin": "管理员", "member": "普通成员"}

# 群角色 → 日志颜色（按 QQ 身份标识配色：群主=金黄、管理员=透亮绿、普通成员=纯白）
_ROLE_COLOR_MAP = {"owner": "#F9C74F", "admin": "#6EE7B7", "member": "#FFFFFF"}

# 需要群主身份才能执行的操作（管理员不可）
_OWNER_ONLY_FEATURES = {"enable_set_group_special_title"}

# 群管理功能开关 → 能力描述（仅当开关开启且角色允许时列出）
_PERMISSION_ITEMS: tuple[tuple[str, str], ...] = (
    ("enable_mute", "禁言/解禁"),
    ("enable_kick", "踢人"),
    ("enable_set_group_name", "修改群名"),
    ("enable_set_group_card", "修改群名片"),
    ("enable_set_group_special_title", "设置群专属头衔"),
    ("enable_send_group_notice", "发布群公告"),
    ("enable_delete_group_notice", "删除群公告"),
    ("enable_essence_msg", "设置/移除精华消息"),
    ("enable_set_group_whole_ban", "全员禁言"),
)

# 每群刷新时间缓存：group_id -> 最近刷新时间戳
_refreshed_at: dict[str, float] = {}


def _get_ttl_seconds(config: Any) -> int:
    """从插件配置读取刷新 TTL（秒），异常时回退默认值。

    Args:
        config: 插件配置实例。

    Returns:
        刷新 TTL 秒数。
    """

    try:
        ttl = int(getattr(getattr(config, "bot_role", None), "ttl_seconds", 0) or 0)
        if ttl > 0:
            return ttl
    except Exception:
        pass
    return _DEFAULT_TTL_SECONDS


def _should_refresh(group_id: str, config: Any) -> bool:
    """判断指定群的身份信息是否需要重新查询。

    Args:
        group_id: 群号。
        config: 插件配置实例。

    Returns:
        True 表示超过 TTL 需要刷新；False 表示仍处于有效期内。
    """

    last = _refreshed_at.get(str(group_id))
    if last is None:
        return True
    return (time.time() - last) >= _get_ttl_seconds(config)


def _mark_refreshed(group_id: str) -> None:
    """记录指定群最近刷新时间。

    Args:
        group_id: 群号。
    """

    _refreshed_at[str(group_id)] = time.time()


def _format_time(ts: Any) -> str:
    """将时间戳格式化为日期字符串；无效输入返回空字符串。"""

    from datetime import datetime

    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return ""


def _build_permission_lines(role: str, config: Any) -> list[str]:
    """根据角色与功能开关生成可执行操作清单。

    Args:
        role: 群角色（owner/admin/member）。
        config: 插件配置实例。

    Returns:
        权限描述行列表；普通成员返回空列表。
    """

    if role not in ("owner", "admin"):
        return []

    features = getattr(config, "features", None)
    if features is None:
        return []

    lines: list[str] = []
    for feature, description in _PERMISSION_ITEMS:
        if not bool(getattr(features, feature, False)):
            continue
        if feature in _OWNER_ONLY_FEATURES and role != "owner":
            continue
        lines.append(description)
    return lines


def _collect_bot_honors(honor_data: dict[str, Any], bot_id: str) -> list[str]:
    """从群荣誉数据中筛选出 bot 自己获得的荣誉。

    Args:
        honor_data: get_group_honor_info 返回的 data 字典。
        bot_id: bot 的 QQ 号。

    Returns:
        荣誉名称列表；无荣誉返回空列表。
    """

    honors: list[str] = []
    bot_id_str = str(bot_id)

    # 各荣誉类型的名称与对应列表字段
    entries: list[tuple[str, Any]] = [
        ("当前龙王", honor_data.get("current_talkative")),
        ("历史龙王", honor_data.get("talkative_list")),
        ("群聊之火", honor_data.get("performer_list")),
        ("群聊炽焰", honor_data.get("legend_list")),
    ]

    for label, item in entries:
        if isinstance(item, dict):
            if str(item.get("user_id", "")) == bot_id_str:
                honors.append(label)
        elif isinstance(item, list):
            for member in item:
                if isinstance(member, dict) and str(member.get("user_id", "")) == bot_id_str:
                    honors.append(label)
                    break
    return honors


def _build_honor_board(honor_data: dict[str, Any]) -> list[str]:
    """渲染本群荣誉榜（当前龙王/群聊之火/群聊炽焰）。"""

    lines: list[str] = []

    def _fmt_member(member: Any) -> str:
        if not isinstance(member, dict):
            return ""
        return f"{member.get('nickname', '')}({member.get('user_id', '')})"

    current = honor_data.get("current_talkative")
    if isinstance(current, dict) and current:
        lines.append(f"- 当前龙王：{_fmt_member(current)}")

    for label, field in (("群聊之火", "performer_list"), ("群聊炽焰", "legend_list")):
        members = honor_data.get(field) or []
        names = [_fmt_member(m) for m in members if _fmt_member(m)]
        if names:
            lines.append(f"- {label}：{'、'.join(names[:10])}")

    return lines


def _build_reminder_text(
    *,
    group_id: str,
    bot_id: str,
    member_data: dict[str, Any],
    honor_data: dict[str, Any] | None,
    config: Any,
) -> str:
    """渲染注入 LLM 的 bot 身份/权限 reminder 文本。

    Args:
        group_id: 群号。
        bot_id: bot 的 QQ 号。
        member_data: get_group_member_info 返回的 data 字典。
        honor_data: get_group_honor_info 返回的 data 字典；None 表示未获取。
        config: 插件配置实例。

    Returns:
        reminder 文本内容。
    """

    role = str(member_data.get("role", "") or "member")
    role_name = _ROLE_MAP.get(role, "普通成员")

    lines: list[str] = []
    lines.append(f"你在当前群（群号: {group_id}）的身份资料：")
    lines.append("")
    lines.append("【身份】")
    lines.append(f"- QQ号：{bot_id}")
    nickname = str(member_data.get("nickname", "") or "")
    card = str(member_data.get("card", "") or "")
    if nickname:
        lines.append(f"- 昵称：{nickname}")
    if card:
        lines.append(f"- 群昵称（群名片）：{card}")
    lines.append(f"- 身份角色：{role_name}")
    title = str(member_data.get("title", "") or "").strip()
    lines.append(f"- 专属头衔：{title if title else '无'}")
    level = member_data.get("level", "")
    if level:
        lines.append(f"- 群等级：{level}")

    join_time = _format_time(member_data.get("join_time", 0))
    if join_time:
        lines.append(f"- 入群时间：{join_time}")

    # 你的本群荣誉
    lines.append("")
    lines.append("【你的本群荣誉】")
    if honor_data:
        bot_honors = _collect_bot_honors(honor_data, bot_id)
        if bot_honors:
            lines.append("、".join(bot_honors))
        else:
            lines.append("无")
    else:
        lines.append("无")

    # 本群荣誉榜
    if honor_data:
        board = _build_honor_board(honor_data)
        if board:
            lines.append("")
            lines.append("【本群荣誉榜】")
            lines.extend(board)

    # 可执行操作（动态）
    lines.append("")
    permission_lines = _build_permission_lines(role, config)
    if role in ("owner", "admin"):
        if permission_lines:
            lines.append("【可执行的管理操作】")
            lines.append(f"你是{role_name}，且以下功能已开启：")
            lines.append("、".join(permission_lines) + "。")
        else:
            lines.append(f"你是{role_name}，但目前未启用任何管理类功能。")
    else:
        lines.append("【权限说明】")
        lines.append("你是普通成员，无管理权限；仅可执行无需权限的操作（戳一戳、打卡、贴表情、点赞等）。")

    return "\n".join(lines)


async def update_bot_role_reminder(
    *,
    stream_id: str,
    group_id: str,
    bot_id: str,
    member_data: dict[str, Any],
    honor_data: dict[str, Any] | None,
    config: Any,
    source: str = "auto",
    group_name: str = "",
) -> None:
    """将 bot 身份/权限信息写入指定流的 system reminder。

    Args:
        stream_id: 聊天流 ID。
        group_id: 群号。
        bot_id: bot 的 QQ 号。
        member_data: get_group_member_info 返回的 data 字典。
        honor_data: get_group_honor_info 返回的 data 字典；None 表示未获取。
        config: 插件配置实例。
        source: 刷新来源（auto=后台自动 / self_query=bot 主动查询自己）。
        group_name: 群名（仅用于日志展示，可为空）。
    """

    if not stream_id or not group_id or not bot_id:
        return

    content = _build_reminder_text(
        group_id=group_id,
        bot_id=bot_id,
        member_data=member_data,
        honor_data=honor_data,
        config=config,
    )

    role = str(member_data.get("role", "") or "member")
    role_name = _ROLE_MAP.get(role, "普通成员")

    try:
        prompt_api.add_stream_reminder(
            stream_id=stream_id,
            bucket=SystemReminderBucket.ACTOR.value,
            name=_REMINDER_NAME,
            content=content,
            insert_type=SystemReminderInsertType.DYNAMIC,
        )
        source_label = "bot 主动查询" if source == "self_query" else "后台自动"
        group_label = f"{group_name}({group_id})" if group_name else f"{group_id}"
        role_color = _ROLE_COLOR_MAP.get(role, "#9ECE6A")
        logger.info(
            f"[#7AA2F7][Bot身份][/#7AA2F7] "
            f"[#F9E2AF]已更新 bot 在群 {group_label} 的身份权限[/#F9E2AF] "
            f"[#a6adc8]（bot={bot_id}, 角色=[{role_color}]{role_name}[/{role_color}], "
            f"来源={source_label}）[/#a6adc8]"
        )
    except Exception as exc:
        logger.error(f"写入 bot_role reminder 失败: stream_id={stream_id}, error={exc}")


async def fetch_and_update_bot_role(
    *,
    stream_id: str,
    group_id: str,
    config: Any,
    force: bool = False,
    source: str = "auto",
) -> None:
    """查询 bot 在当前群的成员资料与群荣誉，并写入流 reminder。

    Args:
        stream_id: 聊天流 ID。
        group_id: 群号。
        config: 插件配置实例。
        force: 为 True 时忽略 TTL 强制刷新（bot 主动查询自己时使用）。
        source: 刷新来源（auto=后台自动 / self_query=bot 主动查询自己）。
    """

    if not stream_id or not group_id:
        return

    if not force and not _should_refresh(group_id, config):
        return

    adapter = adapter_api.get_adapter(_SNOWLUMA_ADAPTER_SIGNATURE)
    if adapter is None or not hasattr(adapter, "send_snowluma_api"):
        return

    try:
        bot_info = await adapter_api.get_bot_info_by_platform("qq")
    except Exception:
        bot_info = None
    if not bot_info or not bot_info.get("bot_id"):
        return
    bot_id = str(bot_info["bot_id"])

    gid = _coerce_int_if_digit(group_id)
    try:
        member_resp = await adapter.send_snowluma_api(  # type: ignore[attr-defined]
            "get_group_member_info",
            {"group_id": gid, "user_id": _coerce_int_if_digit(bot_id), "no_cache": True},
            timeout=30.0,
        )
    except Exception as exc:
        logger.warning(f"查询 bot 群成员信息失败: group_id={group_id}, error={exc}")
        return

    member_data = member_resp.get("data") if isinstance(member_resp, dict) else None
    if not isinstance(member_data, dict) or not member_data:
        logger.warning(f"bot 群成员信息为空: group_id={group_id}")
        return

    honor_data: dict[str, Any] | None = None
    try:
        honor_resp = await adapter.send_snowluma_api(  # type: ignore[attr-defined]
            "get_group_honor_info",
            {"group_id": gid, "type": "all"},
            timeout=30.0,
        )
        candidate = honor_resp.get("data") if isinstance(honor_resp, dict) else None
        if isinstance(candidate, dict):
            honor_data = candidate
    except Exception as exc:
        logger.warning(f"查询群荣誉信息失败: group_id={group_id}, error={exc}")

    # 查询群名（仅用于日志展示，失败不阻塞主流程）
    group_name = ""
    try:
        group_resp = await adapter.send_snowluma_api(  # type: ignore[attr-defined]
            "get_group_info",
            {"group_id": gid, "no_cache": True},
            timeout=30.0,
        )
        group_data = group_resp.get("data") if isinstance(group_resp, dict) else None
        if isinstance(group_data, dict):
            group_name = str(group_data.get("group_name", "") or "").strip()
    except Exception as exc:
        logger.debug(f"查询群名失败: group_id={group_id}, error={exc}")

    await update_bot_role_reminder(
        stream_id=stream_id,
        group_id=group_id,
        bot_id=bot_id,
        member_data=member_data,
        honor_data=honor_data,
        config=config,
        source=source,
        group_name=group_name,
    )
    _mark_refreshed(group_id)


class BotRoleReminderHandler(BaseEventHandler):
    """Bot 身份/群权限自动注入处理器。

    订阅群消息接收事件，在群消息到达时按 TTL 刷新 bot 身份 reminder。
    查询在后台任务中执行，不阻塞消息处理主流程。
    """

    name: str = "bot_role_reminder_handler"
    description: str = "自动查询 bot 在群内的身份资料与荣誉，注入 LLM 上下文"
    weight: int = 5
    init_subscribe: list[EventType | str] = [EventType.ON_MESSAGE_RECEIVED]

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理群消息事件，后台刷新 bot 身份 reminder。

        Args:
            event_name: 事件名称。
            params: 事件参数字典，含 ``message``。

        Returns:
            tuple[EventDecision, dict]: 事件决策与参数。
        """

        config = getattr(self.plugin, "config", None)
        if config is None:
            return EventDecision.SUCCESS, params

        bot_role_cfg = getattr(config, "bot_role", None)
        if bot_role_cfg is None or not bool(getattr(bot_role_cfg, "enable", False)):
            return EventDecision.SUCCESS, params

        message = params.get("message")
        if not isinstance(message, Message):
            return EventDecision.SUCCESS, params

        if message.platform != "qq" or message.chat_type != "group":
            return EventDecision.SUCCESS, params

        stream_id = message.stream_id
        extra = message.extra or {}
        group_id = extra.get("group_id") or extra.get("target_group_id")
        if not stream_id or not group_id:
            return EventDecision.SUCCESS, params

        get_task_manager().create_task(
            fetch_and_update_bot_role(
                stream_id=stream_id,
                group_id=str(group_id),
                config=config,
            ),
            name="snowluma_extension_bot_role_refresh",
            daemon=True,
        )
        return EventDecision.SUCCESS, params


__all__ = [
    "BotRoleReminderHandler",
    "fetch_and_update_bot_role",
    "update_bot_role_reminder",
]
