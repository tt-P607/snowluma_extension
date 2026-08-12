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
from src.kernel.concurrency import get_task_manager
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
    SendGroupForwardMsgAction,
    SendGroupNoticeAction,
    SendLikeAction,
    SendShareCardAction,
    SetEssenceMsgAction,
    SetGroupCardAction,
    SetGroupNameAction,
    SetGroupSpecialTitleAction,
    SetGroupWholeBanAction,
    UnmuteGroupMemberAction,
)
from .src.bot_role_reminder import BotRoleReminderHandler
from .src.face_intercept_handler import FaceInterceptHandler
from .src.tools import (
    GetBotMessagesTool,
    GetEssenceMsgListTool,
    GetGroupHonorInfoTool,
    GetGroupInfoTool,
    GetGroupMemberInfoTool,
    GetGroupMemberListTool,
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
    configs: list[type] = [SnowLumaExtensionConfig]

    def get_components(self) -> list[type]:
        config = cast(SnowLumaExtensionConfig, self.config)
        if not config.plugin.enabled:
            return []

        components: list[type] = [
            # 群管理 Actions
            MuteGroupMemberAction,
            UnmuteGroupMemberAction,
            SetGroupWholeBanAction,
            ReactToMessageAction,
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
            SendShareCardAction,
            # 事件拦截器
            FaceInterceptHandler,
            # Bot 身份/群权限自动注入
            BotRoleReminderHandler,
        ]

        # Tool 组件按需注册
        if self.config:
            config = cast(SnowLumaExtensionConfig, self.config)
            if config.features.enable_get_group_member_info:
                components.append(GetGroupMemberInfoTool)
                components.append(GetGroupInfoTool)
                components.append(GetGroupMemberListTool)
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
            if config.features.enable_recall:
                components.append(GetBotMessagesTool)

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

        使用一次性 ``delay_seconds`` 任务 + 回调内延迟自重注册模式。

        调度器 ``_check_time_trigger`` 在 ``is_recurring=True`` 且 config 含
        ``interval_seconds`` 时会忽略 ``trigger_at``，导致首次触发时间为
        ``created_at + interval_seconds`` 而非配置的目标时间。
        因此这里改为注册一次性延迟任务，每次回调完成后重新注册下一天的
        延迟任务，保证触发时间始终对齐目标。
        """
        import asyncio

        sign_config = self.config.scheduled_sign  # type: ignore[union-attr]

        if not sign_config.group_ids:
            logger.warning("定时打卡已启用但未配置 group_ids，跳过")
            return

        group_ids = [str(g) for g in sign_config.group_ids]

        sign_time = sign_config.sign_time
        try:
            hour, minute = map(int, sign_time.split(":"))
        except (ValueError, AttributeError):
            logger.warning(
                f"定时打卡时间格式无效：{sign_time}（应为 HH:MM），使用默认 08:00"
            )
            hour, minute = 8, 0

        jitter_min = max(0, sign_config.jitter_min_seconds)
        jitter_max = max(jitter_min, sign_config.jitter_max_seconds)
        task_name = "snowluma_extension_scheduled_sign"

        # ── 内部工具函数 ──

        def _calc_delay() -> tuple[float, datetime]:
            """计算到下一个目标时刻（HH:MM）的秒数及目标 datetime。"""
            cur = datetime.now()
            target = cur.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= cur:
                target += timedelta(days=1)
            return max(1.0, (target - cur).total_seconds()), target

        async def _register_next() -> None:
            """注册下一次打卡的一次性延迟任务。"""
            delay, target = _calc_delay()
            try:
                scheduler = get_unified_scheduler()
                await scheduler.create_schedule(
                    callback=_do_sign,
                    trigger_type=TriggerType.TIME,
                    trigger_config={"delay_seconds": delay},
                    is_recurring=False,
                    task_name=task_name,
                    force_overwrite=True,
                )
                logger.info(
                    f"定时打卡已注册：groups={group_ids}, "
                    f"目标 {hour:02d}:{minute:02d}, "
                    f"抖动 {jitter_min}-{jitter_max}s, "
                    f"{delay:.0f}s 后触发"
                    f"（{target.strftime('%Y-%m-%d %H:%M:%S')}）"
                )
            except Exception as exc:
                logger.error(f"注册定时打卡任务失败：{exc}")

        async def _schedule_next_deferred() -> None:
            """延迟 5 秒后注册下一次打卡。

            当前任务是一次性的，回调结束后调度器会清理它。
            延迟确保旧任务从 ``_tasks_by_name`` 中移除后再注册同名新任务，
            避免 ``force_overwrite`` 取消正在执行的自身 asyncio Task。
            """

            async def _inner() -> None:
                await asyncio.sleep(5)
                await _register_next()

            get_task_manager().create_task(
                _inner(),
                name="snowluma_extension_sign_reschedule",
                daemon=True,
            )

        async def _do_sign() -> None:
            """执行定时打卡，完成后延迟注册下一次任务。"""
            today_str = datetime.now().strftime("%Y-%m-%d")

            # 防重复：检查今天是否已打过卡
            try:
                record = await storage_api.load_json(
                    "snowluma_extension", "sign_record"
                )
                if record and record.get("last_sign_date") == today_str:
                    logger.debug(f"今日（{today_str}）已打过卡，跳过")
                    await _schedule_next_deferred()
                    return
            except Exception:
                pass

            adapter = adapter_api.get_adapter(
                "snowluma_adapter:adapter:snowluma_adapter"
            )
            if adapter is None:
                logger.warning("定时打卡失败：snowluma_adapter 未启动")
                await _schedule_next_deferred()
                return

            for gid in group_ids:
                if jitter_max > 0:
                    delay = random.uniform(jitter_min, jitter_max)
                    logger.debug(f"群 {gid} 打卡前等待 {delay:.1f}s")
                    await asyncio.sleep(delay)
                try:
                    params = {"group_id": int(gid) if gid.isdigit() else gid}
                    await adapter.send_snowluma_api(
                        "set_group_sign", params, timeout=30.0
                    )  # type: ignore[attr-defined]
                    logger.info(f"定时打卡成功：group_id={gid}")
                except Exception as exc:
                    logger.error(f"定时打卡失败：group_id={gid}, error={exc}")

            # 记录今天已打卡（用执行时刻的日期）
            try:
                await storage_api.save_json(
                    "snowluma_extension",
                    "sign_record",
                    {"last_sign_date": today_str},
                )
            except Exception:
                pass

            # 延迟注册下一次
            await _schedule_next_deferred()

        # ── 启动时检查补打 ──

        now = datetime.now()
        today_sign_dt = now.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )

        if today_sign_dt <= now:
            # 今天打卡时间已过，检查是否需要补打
            today_str = now.strftime("%Y-%m-%d")
            need_catch_up = True
            try:
                record = await storage_api.load_json(
                    "snowluma_extension", "sign_record"
                )
                if record and record.get("last_sign_date") == today_str:
                    logger.info("今日已打过卡，跳过补打")
                    need_catch_up = False
            except Exception:
                pass

            if need_catch_up:

                async def _delayed_catch_up() -> None:
                    """等待 adapter 连接建立后补打。"""
                    logger.info(
                        "检测到今日打卡时间已过且未打过卡，"
                        "10 秒后自动补打"
                    )
                    await asyncio.sleep(10)
                    catch_up_date = datetime.now().strftime("%Y-%m-%d")

                    # 再次检查，避免与定时任务竞争
                    try:
                        rec = await storage_api.load_json(
                            "snowluma_extension", "sign_record"
                        )
                        if rec and rec.get("last_sign_date") == catch_up_date:
                            logger.info("补打前发现今日已打过卡，跳过")
                            return
                    except Exception:
                        pass

                    adapter = adapter_api.get_adapter(
                        "snowluma_adapter:adapter:snowluma_adapter"
                    )
                    if adapter is None:
                        logger.warning("补打失败：snowluma_adapter 未启动")
                        return
                    for gid in group_ids:
                        if jitter_max > 0:
                            await asyncio.sleep(
                                random.uniform(jitter_min, jitter_max)
                            )
                        try:
                            params = {
                                "group_id": int(gid) if gid.isdigit() else gid
                            }
                            await adapter.send_snowluma_api(
                                "set_group_sign", params, timeout=30.0
                            )  # type: ignore[attr-defined]
                            logger.info(f"补打成功：group_id={gid}")
                        except Exception as exc:
                            logger.error(
                                f"补打失败：group_id={gid}, error={exc}"
                            )
                    try:
                        await storage_api.save_json(
                            "snowluma_extension",
                            "sign_record",
                            {"last_sign_date": catch_up_date},
                        )
                    except Exception:
                        pass

                get_task_manager().create_task(
                    _delayed_catch_up(),
                    name="snowluma_extension_delayed_sign",
                    daemon=True,
                )

        # 注册下一次定时打卡（无论是否补打都需要）
        await _register_next()
