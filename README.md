# SnowLuma Extension

SnowLuma 适配器扩展插件，为 Bot 提供 QQ 平台的高级操作能力。

## 功能特性

- **群管理**：禁言/解除禁言、全群禁言、踢出成员、修改群名/群名片/群头衔
- **消息操作**：表情回应（贴表情）、发送 QQ 原生表情、戳一戳、撤回消息
- **群公告**：发送/删除/查询群公告
- **群打卡**：定时自动群打卡
- **加群审批**：由适配器实时推送入群申请事件，由 LLM 审核通过/拒绝
- **信息查询**：群成员信息、群成员列表、群公告列表、QQ 表情列表、群荣誉、群禁言列表、群基本信息、bot 最近消息

## 组件列表

### Actions

| 组件名 | 说明 |
|--------|------|
| `mute_group_member` | 禁言群成员 |
| `unmute_group_member` | 解除禁言 |
| `set_group_whole_ban` | 全群禁言开关 |
| `react_to_message` | 对消息添加表情回应 |
| `send_face` | 发送 QQ 原生表情 |
| `poke_group_member` | 戳一戳群成员 |
| `recall_message` | 撤回消息 |
| `group_sign` | 群打卡 |
| `kick_group_member` | 踢出群成员 |
| `set_group_name` | 修改群名 |
| `set_group_card` | 修改群名片 |
| `set_group_special_title` | 修改群头衔 |
| `send_group_notice` | 发送群公告 |
| `delete_group_notice` | 删除群公告 |
| `send_group_forward_msg` | 发送群合并转发消息 |
| `set_essence_msg` | 设置精华消息 |
| `delete_essence_msg` | 移除精华消息 |
| `forward_group_single_msg` | 转发单条消息到群 |
| `forward_friend_single_msg` | 转发单条消息给好友 |
| `send_like` | 给他人主页点赞 |
| `send_share_card` | 发送推荐名片/群名片分享 |
| `handle_group_join_request` | 处理加群请求（通过/拒绝） |

### Tools

| 组件名 | 说明 |
|--------|------|
| `get_group_join_requests` | 查询加群请求列表 |
| `get_group_member_info` | 获取群成员信息 |
| `get_group_info` | 获取群基本信息（群名、成员数等） |
| `get_group_member_list` | 获取群成员列表 |
| `get_group_notice` | 获取群公告列表 |
| `get_qq_face_list` | 查询 QQ 表情列表 |
| `get_essence_msg_list` | 获取群精华消息列表 |
| `get_group_honor_info` | 获取群荣誉信息 |
| `get_group_shut_list` | 获取群禁言列表 |
| `get_bot_messages` | 查询 bot 自己最近发送的消息（含 message_id），配合撤回使用 |

### Event Handlers

| 组件名 | 说明 |
|--------|------|
| `face_intercept_handler` | 拦截消息发送，将文本中的表情标记替换为 QQ face 段 |
| `bot_role_reminder_handler` | 自动查询 bot 在群内的身份资料与荣誉，注入 LLM 上下文 |

## 配置说明

配置文件位于 `config/plugins/snowluma_extension/config.toml`。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `plugin.enabled` | `true` | 插件开关 |
| `plugin.error_hint` | （见配置） | 操作失败时附加给 LLM 的提示词 |
| `features.enable_mute` | `false` | 群成员禁言/解禁/查看禁言列表 |
| `features.enable_set_group_whole_ban` | `false` | 全群禁言开关 |
| `features.enable_react` | `true` | QQ 表情相关功能（贴表情回应、发表情、查询表情表） |
| `features.enable_recall` | `false` | 消息撤回（含查询 bot 自己消息 ID 的 Tool） |
| `features.enable_group_sign` | `true` | 群打卡 |
| `features.enable_kick` | `false` | 踢出群成员 |
| `features.enable_get_group_member_info` | `true` | 获取群成员信息/列表/群信息 |
| `features.enable_get_group_notice` | `false` | 获取群公告列表 |
| `features.enable_get_essence_msg` | `true` | 获取群精华消息列表 |
| `features.enable_get_group_honor` | `true` | 获取群荣誉信息 |
| `features.enable_send_like` | `true` | 给他人主页点赞 |
| `features.send_like_times` | `10` | 每次点赞数量 |
| `features.enable_send_share_card` | `true` | 发送名片分享 |
| `bot_role.enable` | `true` | bot 身份/群权限自动注入 |
| `bot_role.ttl_seconds` | `28800` | 每群身份/荣誉查询刷新间隔（秒） |
| `scheduled_sign.enable` | `false` | 定时群打卡 |
| `scheduled_sign.group_ids` | `[]` | 打卡群列表 |
| `scheduled_sign.sign_time` | `"08:00"` | 打卡时间 |
| `scheduled_sign.jitter_min_seconds` | `1` | 群间最小随机抖动（秒） |
| `scheduled_sign.jitter_max_seconds` | `2` | 群间最大随机抖动（秒） |
| `join_request.enable` | `false` | 加群请求审批管理功能开关 |
| `join_request.error_hint` | （见配置） | 注入给 LLM 的入群审核规则提示词 |

## 依赖

- Neo-MoFox >= 1.0.0
- snowluma_adapter >= 2.0.0
