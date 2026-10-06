"""snowluma_extension 插件入口。

提供 SnowLuma 的高级 Action/Tool 能力。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta
from typing import Any, cast

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BasePlugin, register_plugin
from src.app.plugin_system.types import EventType
from src.kernel.concurrency import get_task_manager
from src.kernel.event import EventDecision, get_event_bus
from src.kernel.scheduler import TriggerType, get_unified_scheduler

from .config import SnowLumaExtensionConfig
from .src.actions import (
    DeleteEssenceMsgAction,
    DeleteGroupNoticeAction,
    ForwardFriendSingleMsgAction,
    ForwardGroupSingleMsgAction,
    GroupSignAction,
    HandleGroupJoinRequestAction,
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
    _sign_group,
)
from .src.bot_role_reminder import BotRoleReminderHandler
from .src.face_intercept_handler import FaceInterceptHandler
from .src.group_members_reminder import GroupMemberIndex, GroupMembersReminderHandler
from .src.tools import (
    GetBotMessagesTool,
    GetEssenceMsgListTool,
    GetGroupHonorInfoTool,
    GetGroupInfoTool,
    GetGroupJoinRequestsTool,
    GetGroupMemberInfoTool,
    GetGroupMemberListTool,
    GetGroupNoticeTool,
    GetGroupShutListTool,
    GetQQFaceListTool,
    RefreshGroupMembersTool,
)

logger = get_logger("snowluma_extension")

@register_plugin
class SnowLumaExtensionPlugin(BasePlugin):
    """SnowLuma Extension 插件。

    整合大模型主动触发的 Actions 与查询 Tools。
    """

    plugin_name = "snowluma_extension"
    configs: list[type] = [SnowLumaExtensionConfig]

    def __init__(self, config: SnowLumaExtensionConfig | None = None) -> None:
        """初始化插件配置和群成员缓存。"""
        super().__init__(config)
        self.group_member_index: GroupMemberIndex = GroupMemberIndex()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._start_unsubscribe: Callable[[], None] | None = None
        self._sign_schedule_id: str | None = None
        self._loaded = False

    def start_background_task(self, coroutine: Coroutine[Any, Any, Any], name: str) -> None:
        """运行随插件卸载而停止的后台任务。"""
        if not self._loaded:
            coroutine.close()
            return
        task = get_task_manager().create_task(coroutine, name=name, daemon=True).task
        assert task is not None
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

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

        if config.features.enable_group_members_reminder:
            components.append(GroupMembersReminderHandler)
            components.append(RefreshGroupMembersTool)

        # 加群请求审批（按配置开关注册）
        if config.join_request.enable:
            components.append(HandleGroupJoinRequestAction)
            components.append(GetGroupJoinRequestsTool)

        # Tool 组件按需注册
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
        if not self.config or not cast(SnowLumaExtensionConfig, self.config).plugin.enabled:
            return
        self._loaded = True

        config: SnowLumaExtensionConfig = self.config  # type: ignore[union-attr]

        # 定时群打卡：订阅 ON_START 事件，等调度器启动后再注册
        if config.scheduled_sign.enable:
            bus = get_event_bus()

            async def _on_start_callback(
                event_name: str, params: dict[str, object]
            ) -> tuple[EventDecision, dict[str, object]]:
                """ON_START 回调：注册定时打卡并检查补打。"""
                if self._loaded:
                    await self._setup_scheduled_sign()
                return EventDecision.SUCCESS, params

            self._start_unsubscribe = bus.subscribe(EventType.ON_START, _on_start_callback, priority=10)
            logger.debug("已订阅 ON_START 事件，等待调度器启动后注册定时打卡")

    async def on_plugin_unloaded(self) -> None:
        """取消事件订阅、调度和后台查询，并保存群成员缓存。"""
        self._loaded = False
        if self._start_unsubscribe is not None:
            self._start_unsubscribe()
            self._start_unsubscribe = None
        if self._sign_schedule_id is not None:
            await get_unified_scheduler().remove_schedule(self._sign_schedule_id)
            self._sign_schedule_id = None
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.group_member_index.close()

    async def _setup_scheduled_sign(self) -> None:
        """注册定时群打卡调度任务。

        使用一次性 ``delay_seconds`` 延迟任务，每次打卡完成后延迟重新注册
        下一天的延迟任务，使触发时间对齐配置的目标时刻。
        """
        sign_config = self.config.scheduled_sign  # type: ignore[union-attr]

        if not sign_config.group_ids:
            logger.warning("定时打卡已启用但未配置 group_ids，跳过")
            return

        group_ids = [str(g) for g in sign_config.group_ids]

        sign_time = sign_config.sign_time
        try:
            hour, minute = map(int, sign_time.split(":"))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError("时间超出有效范围")
        except (ValueError, AttributeError):
            logger.error(f"定时打卡时间无效：{sign_time}（应为有效的 HH:MM），跳过注册")
            return

        jitter_min = max(0, sign_config.jitter_min_seconds)
        jitter_max = max(jitter_min, sign_config.jitter_max_seconds)
        task_name = "snowluma_extension_scheduled_sign"

        def _calc_delay() -> tuple[float, datetime]:
            """计算到下一个目标时刻（HH:MM）的秒数及目标 datetime。"""
            cur = datetime.now()
            target = cur.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= cur:
                target += timedelta(days=1)
            return max(1.0, (target - cur).total_seconds()), target

        async def _register_next() -> None:
            """注册下一次打卡的一次性延迟任务。"""
            if not self._loaded:
                return
            delay, target = _calc_delay()
            try:
                scheduler = get_unified_scheduler()
                schedule_id = await scheduler.create_schedule(
                    callback=_do_sign,
                    trigger_type=TriggerType.TIME,
                    trigger_config={"delay_seconds": delay},
                    is_recurring=False,
                    task_name=task_name,
                    force_overwrite=True,
                )
                if not self._loaded:
                    await scheduler.remove_schedule(schedule_id)
                    return
                self._sign_schedule_id = schedule_id
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

            当前任务是一次性的，回调结束后调度器会清理它。延迟确保旧任务
            从调度器中移除后再注册同名新任务，避免覆盖正在执行的自身任务。
            """

            async def _inner() -> None:
                await asyncio.sleep(5)
                await _register_next()

            self.start_background_task(
                _inner(),
                name="snowluma_extension_sign_reschedule",
            )

        async def _sign_groups() -> None:
            """逐群打卡，共用手动打卡的成功记录。"""
            for gid in group_ids:
                if not self._loaded:
                    return
                if jitter_max > 0:
                    delay = random.uniform(jitter_min, jitter_max)
                    logger.debug(f"群 {gid} 打卡前等待 {delay:.1f}s")
                    await asyncio.sleep(delay)
                try:
                    ok, result = await _sign_group(gid)
                    if ok:
                        logger.info(f"群打卡完成：group_id={gid}, result={result}")
                    else:
                        logger.warning(f"群打卡失败：group_id={gid}, result={result}")
                except Exception as exc:
                    logger.error(f"定时打卡失败：group_id={gid}, error={exc}")

        async def _do_sign() -> None:
            """执行定时打卡，完成后延迟注册下一次任务。"""
            try:
                await _sign_groups()
            finally:
                await _schedule_next_deferred()

        now = datetime.now()
        today_sign_dt = now.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )

        # 今天打卡时间已过，检查是否需要补打
        if today_sign_dt <= now:
            async def _delayed_catch_up() -> None:
                """等待 adapter 建立连接后，补打尚未成功的群。"""
                logger.info("今日打卡时间已过，10 秒后检查各群是否需要补打")
                await asyncio.sleep(10)
                await _sign_groups()

            self.start_background_task(_delayed_catch_up(), name="snowluma_extension_delayed_sign")

        # 注册下一次定时打卡（无论是否补打都需要）
        await _register_next()
