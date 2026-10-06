# SnowLuma Extension

QQ 平台扩展插件，为 Bot 提供群管理、消息操作和信息查询能力。

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
| `refresh_group_members` | 主动刷新当前群人数、成员缓存及近期活跃成员索引提醒 |
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
| `group_members_reminder_handler` | 自动缓存群人数和近期发言成员，在聊天上下文末尾提供人物索引 |

## 群成员索引

群成员索引默认开启，通过 `features.enable_group_members_reminder` 控制。设为 `false` 后重载插件或重启 Bot，将停止成员缓存更新、索引提醒和 `refresh_group_members` 工具，不影响原有群查询工具与 Bot 身份提醒。

开启时自动维护当前 QQ 群的成员资料：

- 每小时按需后台刷新完整成员名单和群总人数，同群并发消息不会重复查询。
- Bot 可调用无参数工具 `refresh_group_members` 主动刷新当前群，绕过一小时缓存期限。工具复用同一份缓存、JSON 快照和系统提醒；若已有后台刷新，则等待同一查询。成功返回最新人数和近期成员索引；失败会明确报告，并保留原有缓存。
- 根据有效的最后发言时间筛选近 7 天成员，按最近发言排序，最多列出 30 人，不含 Bot 自己。活跃人数表示近期发言人数，不是发言频率排名；未知时间不代表不活跃。
- 同时显示群名片、QQ 昵称和 QQ 号；收到消息时立即更新发送者资料。名称只作为转义后的资料使用，同名和外号不明确时不应猜测身份。
- 索引通过当前聊天流的 `actor` 系统提醒注入，并在请求前移至最后一条用户消息的文本末尾，不影响其他群或其他插件的提醒。聊天组件须订阅 `actor` 提醒。
- 完整资料保存在 `data/json_storage/snowluma_extension/group_members_qq_<bot_id>_<group_id>.json`，按平台、Bot 和群隔离；每分钟合并写盘，正常卸载时保存未写入数据。仅保存群概况、成员账号、两种名称和最后发言时间，不保存聊天正文或另一份活跃名单。
- 重启时读取快照并重新生成索引。资料未获取、已过期或查询失败时保留已知资料并标注可能不完整，不阻塞聊天等待全群查询。

JSON 快照属于可重建缓存，不提供异常退出时的原子写入保证；快照损坏时会记录警告并重新查询群资料。

## 群打卡与任务生命周期

手动打卡、定时打卡和启动补打共用按群保存的成功日期。同一天一个群成功后不会重复打卡，也不会阻止其他群；接口失败的群不记录完成，可重新尝试。记录保存在 `data/json_storage/snowluma_extension/sign_record.json`。

定时打卡使用 `scheduled_sign.sign_time` 指定的有效 `HH:MM` 时间。无效时间会记录错误并跳过注册，不会静默改成其他时间。插件卸载时取消启动订阅、打卡计划、延迟任务和自动身份查询。

QQ 原生表情通过 `face_intercept_handler` 将文本标记转换成消息段，不提供独立的 `send_face` 动作。Bot 身份查询使用当前消息对应的 QQ 适配器，同群、同流并发刷新会合并。

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
| `features.enable_group_members_reminder` | `true` | 群成员索引与主动刷新工具共用开关，提醒注入聊天上下文末尾 |
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

## 运行要求

- Neo-MoFox >= 1.0.0
- 需要至少启动一个支持适配器命令的 QQ 适配器，例如 `onebot_adapter` 或 `snowluma_adapter`。
- 本插件不声明对某个具体 QQ 适配器的固定依赖。
