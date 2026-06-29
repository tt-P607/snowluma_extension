"""snowluma_extension 插件配置。

配置文件默认路径：config/plugins/snowluma_extension/config.toml

说明：
- 本插件依赖 `snowluma_adapter` 适配器。
- 所有功能默认关闭，需显式在配置中开启。
"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class SnowLumaExtensionConfig(BaseConfig):
    """snowluma_extension 插件配置。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "SnowLuma 扩展能力与通知收集插件配置"

    @config_section("plugin")
    class PluginSection(SectionBase):
        """插件总体配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用 snowluma_extension 插件（关闭则所有 Action 不激活）",
        )
        config_version: str = Field(
            default="1.0.0",
            description="配置版本号",
        )
        error_hint: str = Field(
            default="操作失败时，请如实告知用户失败原因，不要假装操作成功。如果提示权限不足，说明你不是群主或管理员。",
            description="操作失败时附加给 LLM 的提示词，指导 bot 如何向用户反馈错误",
        )

    @config_section("features")
    class FeaturesSection(SectionBase):
        """功能开关（默认全部关闭）。"""

        # --- 群管理 Action ---
        enable_mute: bool = Field(default=False, description="是否启用：群成员禁言")
        enable_unmute: bool = Field(default=False, description="是否启用：群成员解除禁言")
        enable_react: bool = Field(default=True, description="是否启用：QQ 表情相关功能（贴表情回应、发表情、查询表情表）")
        enable_poke: bool = Field(default=True, description="是否启用：戳一戳群成员")
        enable_recall: bool = Field(default=False, description="是否启用：撤回指定消息")
        enable_group_sign: bool = Field(default=True, description="是否启用：群打卡")
        enable_kick: bool = Field(default=False, description="是否启用：踢出群成员")

        # --- 新增群管理权限 ---
        enable_set_group_name: bool = Field(default=False, description="是否启用：修改群名")
        enable_set_group_card: bool = Field(default=False, description="是否启用：修改群名片")
        enable_set_group_special_title: bool = Field(default=False, description="是否启用：修改群头衔")
        enable_set_group_admin: bool = Field(default=False, description="是否启用：设置管理员")
        enable_set_group_leave: bool = Field(default=False, description="是否启用：退出群聊")
        enable_get_group_member_info: bool = Field(default=True, description="是否启用：获取群成员信息")
        enable_send_group_notice: bool = Field(default=False, description="是否启用：发送群公告")
        enable_delete_group_notice: bool = Field(default=False, description="是否启用：删除群公告")
        enable_get_group_notice: bool = Field(default=False, description="是否启用：获取群公告列表")

    @config_section("scheduled_sign")
    class ScheduledSignSection(SectionBase):
        """定时群打卡配置。

        使用独立的群号列表，不依赖 adapter 的黑白名单配置。
        每个群打卡之间会有随机抖动延迟，避免同时打卡触发风控。
        """

        enable: bool = Field(
            default=False,
            description="是否启用定时群打卡",
        )
        group_ids: list[str | int] = Field(
            default=[],
            description="需要定时打卡的群号列表",
        )
        sign_time: str = Field(
            default="08:00",
            description="每天打卡的时间点（24小时制 HH:MM 格式，如 08:00）",
        )
        jitter_min_seconds: int = Field(
            default=10,
            description="群与群之间打卡的最小随机抖动（秒）",
        )
        jitter_max_seconds: int = Field(
            default=120,
            description="群与群之间打卡的最大随机抖动（秒）",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    features: FeaturesSection = Field(default_factory=FeaturesSection)
    scheduled_sign: ScheduledSignSection = Field(default_factory=ScheduledSignSection)


__all__ = ["SnowLumaExtensionConfig"]
