"""snowluma_extension 插件入口。

提供 SnowLuma 的高级 Action/Tool 能力。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import cast

from src.app.plugin_system.api import adapter_api
from src.kernel.scheduler import TriggerType, get_unified_scheduler

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import BasePlugin
from src.core.components.loader import register_plugin

from .config import SnowLumaExtensionConfig
from .src.actions import (
    DeleteGroupNoticeAction,
    GroupSignAction,
    KickGroupMemberAction,
    MuteGroupMemberAction,
    PokeGroupMemberAction,
    ReactToMessageAction,
    RecallMessageAction,
    SendFaceAction,
    SendGroupNoticeAction,
    SetGroupCardAction,
    SetGroupNameAction,
    SetGroupSpecialTitleAction,
    UnmuteGroupMemberAction,
)
from .src.tools import GetGroupMemberInfoTool, GetGroupNoticeTool, GetQQFaceListTool

logger = get_logger("snowluma_extension")


@register_plugin
class SnowLumaExtensionPlugin(BasePlugin):
    """SnowLuma Extension 插件。

    整合大模型主动触发的 Actions 与查询 Tools。
    """

    plugin_name = "snowluma_extension"
    plugin_version = "1.0.0"
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

        return components

    async def on_plugin_loaded(self) -> None:
        """插件加载完成后注册定时任务。"""
        if not self.config:
            return

        config: SnowLumaExtensionConfig = self.config  # type: ignore[union-attr]

        # 定时群打卡
        if config.scheduled_sign.enable:
            await self._setup_scheduled_sign()

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
        trigger_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if trigger_dt <= now:
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
