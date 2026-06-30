"""snowluma_extension 插件入口。

提供 SnowLuma 的高级 Action/Tool 能力。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import cast

from src.app.plugin_system.api import adapter_api, storage_api
from src.core.components.types import EventType
from src.kernel.event import EventDecision, get_event_bus
from src.kernel.scheduler import TriggerType, get_unified_scheduler

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import BasePlugin
from src.core.components.loader import register_plugin

from .config import SnowLumaExtensionConfig
from .src.actions import (
    DeleteEssenceMsgAction,
    DeleteGroupNoticeAction,
    ForwardFriendSingleMsgAction,
    ForwardGroupSingleMsgAction,
    GroupSignAction,
    KickGroupMemberAction,
    MuteGroupMemberAction,
    PokeGroupMemberAction,
    ReactToMessageAction,
    RecallMessageAction,
    SendFaceAction,
    SendGroupForwardMsgAction,
    SendGroupNoticeAction,
    SendLikeAction,
    SetEssenceMsgAction,
    SetGroupCardAction,
    SetGroupNameAction,
    SetGroupSpecialTitleAction,
    UnmuteGroupMemberAction,
)
from .src.tools import (
    GetEssenceMsgListTool,
    GetGroupHonorInfoTool,
    GetGroupMemberInfoTool,
    GetGroupNoticeTool,
    GetGroupShutListTool,
    GetQQFaceListTool,
)

logger = get_logger("snowluma_extension")


@register_plugin
class SnowLumaExtensionPlugin(BasePlugin):
    """SnowLuma Extension 插件。

    整合大模型主动触发的 Actions 与查询 Tools。
    """

    plugin_name = "snowluma_extension"
    plugin_version = "1.0.1"
    plugin_author = "MoFox Team"
    plugin_description = "SnowLuma 高级能力支持（群管理 Actions / 查询 Tools / 定时打卡）"
    configs: list[type] = [SnowLumaExtensionConfig]

    def get_components(self) -> list[type]:
        components: list[type] = [
            # 群管理 Actions
            MuteGroupMemberAction,
            UnmuteGroupMemberAction,
            ReactToMessageAction,
            SendFaceAction,
            PokeGroupMemberAction,
            RecallMessageAction,
            GroupSignAction,
            KickGroupMemberAction,
            SetGroupNameAction,
            SetGroupCardAction,
            SetGroupSpecialTitleAction,
            SendGroupNoticeAction,
            DeleteGroupNoticeAction,
            SendGroupForwardMsgAction,
            SetEssenceMsgAction,
            DeleteEssenceMsgAction,
            ForwardGroupSingleMsgAction,
            ForwardFriendSingleMsgAction,
            SendLikeAction,
        ]

        # Tool 组件按需注册
        if self.config:
            config = cast(SnowLumaExtensionConfig, self.config)
            if config.features.enable_get_group_member_info:
                components.append(GetGroupMemberInfoTool)
            if config.features.enable_get_group_notice:
                components.append(GetGroupNoticeTool)
            if config.features.enable_react:
                components.append(GetQQFaceListTool)
            if config.features.enable_get_essence_msg:
                components.append(GetEssenceMsgListTool)
            if config.features.enable_get_group_honor:
                components.append(GetGroupHonorInfoTool)
            if config.features.enable_mute:
                components.append(GetGroupShutListTool)

        return components

    async def on_plugin_loaded(self) -> None:
        """插件加载完成后注册定时任务。

        定时打卡任务在 ON_START 事件中注册，确保调度器已启动。
        """
        if not self.config:
            return

        config: SnowLumaExtensionConfig = self.config  # type: ignore[union-attr]

        # 定时群打卡：订阅 ON_START 事件，等调度器启动后再注册
        if config.scheduled_sign.enable:
            bus = get_event_bus()

            async def _on_start_callback(
                event_name: str, params: dict[str, object]
            ) -> tuple[EventDecision, dict[str, object]]:
                """ON_START 回调：调度器已就绪，注册定时打卡并检查补打。"""
                await self._setup_scheduled_sign()
                return EventDecision.SUCCESS, params

            bus.subscribe(EventType.ON_START, _on_start_callback, priority=10)
            logger.debug("已订阅 ON_START 事件，等待调度器启动后注册定时打卡")

    async def _setup_scheduled_sign(self) -> None:
        """注册定时群打卡调度任务。

        使用插件自身的群号列表配置，每天在指定时间点打卡。
        群与群之间有随机抖动延迟，避免同时打卡触发风控。
        """
        import asyncio

        sign_config = self.config.scheduled_sign  # type: ignore[union-attr]

        # 从自身配置读取群号列表
        if not sign_config.group_ids:
            logger.warning("定时打卡已启用但未配置 group_ids，跳过")
            return

        group_ids = [str(g) for g in sign_config.group_ids]

        # 计算首次触发时间（今天的 sign_time 或明天的）
        sign_time = sign_config.sign_time
        try:
            hour, minute = map(int, sign_time.split(":"))
        except (ValueError, AttributeError):
            logger.warning(f"定时打卡时间格式无效：{sign_time}（应为 HH:MM），使用默认 08:00")
            hour, minute = 8, 0

        now = datetime.now()
        today_sign_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        missed_today = today_sign_dt <= now

        trigger_dt = today_sign_dt
        if missed_today:
            trigger_dt += timedelta(days=1)

        trigger_at = trigger_dt.strftime("%Y-%m-%dT%H:%M:%S")
        interval_seconds = 86400  # 24 小时

        jitter_min = max(0, sign_config.jitter_min_seconds)
        jitter_max = max(jitter_min, sign_config.jitter_max_seconds)

        async def _do_sign() -> None:
            """执行定时打卡，群之间随机抖动。"""
            adapter = adapter_api.get_adapter("snowluma_adapter:adapter:snowluma_adapter")
            if adapter is None:
                logger.warning("定时打卡失败：snowluma_adapter 未启动")
                return

            for gid in group_ids:
                # 随机抖动延迟
                if jitter_max > 0:
                    delay = random.uniform(jitter_min, jitter_max)
                    logger.debug(f"群 {gid} 打卡前等待 {delay:.1f}s")
                    await asyncio.sleep(delay)

                try:
                    params = {"group_id": int(gid) if gid.isdigit() else gid}
                    await adapter.send_snowluma_api("set_group_sign", params, timeout=30.0)  # type: ignore[attr-defined]
                    logger.info(f"定时打卡成功：group_id={gid}")
                except Exception as exc:
                    logger.error(f"定时打卡失败：group_id={gid}, error={exc}")

            # 记录今天已打卡
            try:
                await storage_api.save_json("snowluma_extension", "sign_record", {"last_sign_date": now.strftime("%Y-%m-%d")})
            except Exception:
                pass

        # 补打：如果今天打卡时间已过，检查是否已打过，未打过则延迟补打
        if missed_today:

            async def _delayed_sign() -> None:
                """延迟补打，等待 adapter WebSocket 连接建立。"""
                # 检查今天是否已打过卡
                today_str = datetime.now().strftime("%Y-%m-%d")
                try:
                    record = await storage_api.load_json("snowluma_extension", "sign_record")
                    if record and record.get("last_sign_date") == today_str:
                        logger.info("今日已打过卡，跳过补打")
                        return
                except Exception:
                    pass

                logger.info("检测到今日打卡时间已过且未打过卡，10 秒后自动补打")
                await asyncio.sleep(10)
                await _do_sign()

            asyncio.create_task(_delayed_sign())

        try:
            scheduler = get_unified_scheduler()
            await scheduler.create_schedule(
                callback=_do_sign,
                trigger_type=TriggerType.TIME,
                trigger_config={
                    "trigger_at": trigger_at,
                    "interval_seconds": interval_seconds,
                },
                is_recurring=True,
                task_name="snowluma_extension_scheduled_sign",
                force_overwrite=True,
            )
            logger.info(
                f"定时打卡已注册：groups={group_ids}, "
                f"每日 {sign_time} 打卡, "
                f"抖动 {jitter_min}-{jitter_max}s, "
                f"首次触发={trigger_at}"
            )
        except Exception as exc:
            logger.error(f"注册定时打卡任务失败：{exc}")
