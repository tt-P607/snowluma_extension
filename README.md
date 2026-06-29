# SnowLuma Extension

SnowLuma 适配器扩展插件，为 Bot 提供 QQ 平台的高级操作能力。

## 功能特性

- **群管理**：禁言/解除禁言、踢出成员、修改群名/群名片/群头衔
- **消息操作**：表情回应（贴表情）、发送 QQ 原生表情、戳一戳、撤回消息
- **群公告**：发送/删除群公告
- **群打卡**：定时自动群打卡
- **信息查询**：群成员信息、群公告列表、QQ 表情列表

## 组件列表

### Actions

| 组件名 | 说明 |
|--------|------|
| `mute_group_member` | 禁言群成员 |
| `unmute_group_member` | 解除禁言 |
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

### Tools

| 组件名 | 说明 |
|--------|------|
| `get_group_member_info` | 获取群成员信息 |
| `get_group_notice` | 获取群公告列表 |
| `get_qq_face_list` | 查询 QQ 表情列表 |

## 配置说明

配置文件位于 `config/plugins/snowluma_extension/config.toml`。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `plugin.enabled` | `true` | 插件开关 |
| `features.enable_react` | `true` | QQ 表情相关功能（贴表情回应、发表情、查询表情表） |
| `scheduled_sign.enable` | `false` | 定时群打卡 |
| `scheduled_sign.group_ids` | `[]` | 打卡群列表 |
| `scheduled_sign.sign_time` | `"08:00"` | 打卡时间 |
| `scheduled_sign.jitter_min_seconds` | `0` | 随机提前最小秒数 |
| `scheduled_sign.jitter_max_seconds` | `300` | 随机提前最大秒数 |

## 依赖

- Neo-MoFox >= 1.0.0
- snowluma_adapter >= 2.0.0
