"""snowluma_extension 插件配置。

配置文件默认路径：config/plugins/snowluma_extension/config.toml

说明：
- 本插件依赖 `snowluma_adapter` 适配器。
- 所有功能默认关闭，需显式在配置中开启。
"""

from __future__ import annotations

from typing import ClassVar

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


class SnowLumaExtensionConfig(BaseConfig):
    """snowluma_extension 插件配置。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = "SnowLuma 扩展能力与通知收集插件配置"

    @config_section("plugin")
    class PluginSection(SectionBase):
        """插件总体配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用 snowluma_extension 插件（关闭则所有 Action 不激活）",
        )
        error_hint: str = Field(
            default="操作失败时，请如实告知用户失败原因，不要假装操作成功。如果提示权限不足，说明你不是群主或管理员。",
            description="操作失败时附加给 LLM 的提示词，指导 bot 如何向用户反馈错误",
        )

    @config_section("features")
    class FeaturesSection(SectionBase):
        """功能开关（默认全部关闭）。"""

        # --- 群管理 Action ---
        enable_mute: bool = Field(default=False, description="是否启用：群成员禁言/解禁/查看禁言列表")
        enable_set_group_whole_ban: bool = Field(default=False, description="是否启用：全群禁言开关")
        enable_react: bool = Field(default=True, description="是否启用：QQ 表情相关功能（贴表情回应、发表情、查询表情表）")
        enable_poke: bool = Field(default=True, description="是否启用：戳一戳群成员")
        enable_recall: bool = Field(default=False, description="是否启用：撤回指定消息")
        enable_group_sign: bool = Field(default=True, description="是否启用：群打卡")
        enable_kick: bool = Field(default=False, description="是否启用：踢出群成员")

        # --- 群管理权限 ---
        enable_set_group_name: bool = Field(default=False, description="是否启用：修改群名")
        enable_set_group_card: bool = Field(default=False, description="是否启用：修改群名片")
        enable_set_group_special_title: bool = Field(default=False, description="是否启用：修改群头衔")
        enable_set_group_admin: bool = Field(default=False, description="是否启用：设置管理员")
        enable_set_group_leave: bool = Field(default=False, description="是否启用：退出群聊")
        enable_get_group_member_info: bool = Field(default=True, description="是否启用：获取群成员信息")
        enable_send_group_notice: bool = Field(default=False, description="是否启用：发送群公告")
        enable_delete_group_notice: bool = Field(default=False, description="是否启用：删除群公告")
        enable_get_group_notice: bool = Field(default=False, description="是否启用：获取群公告列表")
        enable_send_forward_msg: bool = Field(default=False, description="是否启用：发送群合并转发消息")
        enable_essence_msg: bool = Field(default=False, description="是否启用：设置/移除精华消息")
        enable_get_essence_msg: bool = Field(default=True, description="是否启用：获取群精华消息列表")
        enable_get_group_honor: bool = Field(default=True, description="是否启用：获取群荣誉信息")
        enable_send_like: bool = Field(default=True, description="是否启用：给他人主页点赞")
        send_like_times: int = Field(default=10, description="每次点赞的数量（非 SVIP 上限 10，SVIP 上限 20）")
        enable_send_share_card: bool = Field(default=True, description="是否启用：发送群名片/个人名片分享")

    @config_section("bot_role")
    class BotRoleSection(SectionBase):
        """Bot 身份/群权限自动注入配置。

        群消息到达时自动查询 bot 在当前群的身份资料与荣誉，
        通过 system reminder 注入 LLM 上下文；TTL 内不重复查询。
        """

        enable: bool = Field(
            default=True,
            description="是否启用 bot 身份/群权限自动注入",
        )
        ttl_seconds: int = Field(
            default=28800,
            description="每群身份/荣誉查询的刷新间隔（秒），默认 8 小时（28800）",
        )

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
            default=1,
            description="群与群之间打卡的最小随机抖动（秒）",
        )
        jitter_max_seconds: int = Field(
            default=2,
            description="群与群之间打卡的最大随机抖动（秒）",
        )

    @config_section("join_request")
    class JoinRequestSection(SectionBase):
        """加群请求审批管理配置。

        启用后提供通过/拒绝加群申请的 Action 与查询待审批列表的 Tool。
        """

        enable: bool = Field(
            default=False,
            description="是否启用加群请求审批管理功能（审批 Action 与查询 Tool）",
        )
        group_list_type: str = Field(
            default="white",
            description="加群审批群名单模式：white（白名单，仅列表中的群启用）/ black（黑名单，列表中的群禁用）",
        )
        group_list: list[str | int] = Field(
            default_factory=list,
            description="加群审批群号列表（配合 group_list_type 决定启用或禁用特定群）",
        )
        error_hint: str = Field(
            default=(
                "【入群审核规则】1. QQ等级必须高于20级；"
                "2. 验证留言必须明确说明入群意图或按要求作答；"
                "3. 遇到广告、推销或回答与问题无关、莫名极妙的直接拒绝；"
                "4. 如果通过沟通确认是正常的真人，但QQ等级不达标，可以酌情考虑通过。"
            ),
            description="注入给 LLM 的入群审核规则提示词，可按需自定义",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    features: FeaturesSection = Field(default_factory=FeaturesSection)
    bot_role: BotRoleSection = Field(default_factory=BotRoleSection)
    scheduled_sign: ScheduledSignSection = Field(default_factory=ScheduledSignSection)
    join_request: JoinRequestSection = Field(default_factory=JoinRequestSection)


__all__ = ["SnowLumaExtensionConfig"]
