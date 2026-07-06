"""QQ 原生表情拦截器。

监听 ON_MESSAGE_SENT 事件，在消息发送前扫描文本段中的表情标记，
自动替换为 QQ face 消息段。

支持的标记格式：
- [face:ID]  如 [face:297]
- [表情：名称]  如 [表情：拜谢]
"""

from __future__ import annotations

import re
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base.event_handler import BaseEventHandler
from src.core.components.types import EventType
from src.kernel.event import EventDecision

logger = get_logger("snowluma_extension")

# 匹配 【face:数字ID】 格式（中文方括号，推荐格式，不会被 Rich 吞掉）
_FACE_ID_PATTERN = re.compile(r"【face:(\d+)】")
# 兼容旧格式 [face:数字ID]（英文方括号，会被 Rich markup 吞掉，不推荐）
_FACE_ID_PATTERN_LEGACY = re.compile(r"\[face:(\d+)\]")
# 匹配 【表情：名称】 或 [表情：名称] 格式
_FACE_NAME_PATTERN = re.compile(r"[【\[]表情：([^\]】]+)[】\]]")

# 反向映射：表情名称 → ID（懒加载）
_FACE_NAME_TO_ID: dict[str, str] | None = None


def _get_name_to_id_map() -> dict[str, str]:
    """构建表情名称到 ID 的反向映射表（懒加载）。"""
    global _FACE_NAME_TO_ID
    if _FACE_NAME_TO_ID is not None:
        return _FACE_NAME_TO_ID

    from plugins.snowluma_adapter.src.event_models import QQ_FACE

    _FACE_NAME_TO_ID = {}
    for fid, fname in QQ_FACE.items():
        # fname 格式为 "[表情：拜谢]"，提取其中的名称部分
        if fname.startswith("[表情：") and fname.endswith("]"):
            name = fname[4:-1]
            _FACE_NAME_TO_ID[name] = fid
        else:
            _FACE_NAME_TO_ID[fname] = fid
    return _FACE_NAME_TO_ID


def _resolve_face_name(name: str) -> str | None:
    """将表情名称解析为 ID，支持精确匹配和模糊匹配。"""
    name_map = _get_name_to_id_map()

    # 精确匹配
    if name in name_map:
        return name_map[name]

    # 模糊匹配：名称包含在 key 中
    for key, fid in name_map.items():
        if name in key:
            return fid

    return None


def _process_text_segment(text: str) -> list[dict[str, Any]]:
    """处理文本段，将表情标记替换为 face 段。

    Args:
        text: 原始文本

    Returns:
        list[dict]: 处理后的段列表，可能包含 text 和 face 段
    """
    # 合并三种 pattern 为统一的匹配：
    # 1. 【face:ID】（中文方括号，推荐）
    # 2. [face:ID]（英文方括号，兼容旧格式）
    # 3. 【表情：名称】或[表情：名称]
    combined = re.compile(r"【face:(\d+)】|\[face:(\d+)\]|[【\[]表情：([^\]】]+)[】\]]")

    result: list[dict[str, Any]] = []
    last_end = 0

    for match in combined.finditer(text):
        # 匹配前的普通文本
        if match.start() > last_end:
            plain = text[last_end:match.start()]
            if plain:
                result.append({"type": "text", "data": plain})

        # 提取 face ID
        face_id = None
        if match.group(1) is not None:
            # 【face:ID】 格式（推荐）
            face_id = match.group(1)
        elif match.group(2) is not None:
            # [face:ID] 格式（兼容旧格式）
            face_id = match.group(2)
        elif match.group(3) is not None:
            # 【表情：名称】或[表情：名称] 格式
            name = match.group(3).strip()
            face_id = _resolve_face_name(name)
            if face_id is None:
                # 无法识别的名称，保留原始文本
                logger.debug(f"无法识别的表情名称: {name}，保留原始文本")
                if not result or result[-1]["type"] != "text":
                    result.append({"type": "text", "data": match.group(0)})
                else:
                    result[-1]["data"] += match.group(0)
                last_end = match.end()
                continue

        if face_id is not None:
            result.append({"type": "face", "data": face_id})

        last_end = match.end()

    # 末尾剩余文本
    if last_end < len(text):
        plain = text[last_end:]
        if plain:
            result.append({"type": "text", "data": plain})

    return result


class FaceInterceptHandler(BaseEventHandler):
    """QQ 原生表情拦截器。

    监听消息发送事件，扫描文本中的 [face:ID] 和 [表情：名称] 标记，
    自动替换为 QQ face 消息段，使模型可以像写文本一样发送 QQ 表情。
    """

    handler_name: str = "face_intercept_handler"
    handler_description: str = "拦截消息发送，将文本中的表情标记替换为 QQ face 消息段"

    weight: int = 50
    intercept_message: bool = False
    init_subscribe: list[EventType | str] = [EventType.ON_MESSAGE_SENT]

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理消息发送事件，替换表情标记。

        Args:
            event_name: 事件名称
            params: 事件参数，包含 message、envelope、adapter_signature

        Returns:
            tuple[EventDecision, dict]: 事件决策和参数
        """
        envelope = params.get("envelope")
        if envelope is None:
            return EventDecision.SUCCESS, params

        # 只处理 QQ 平台的消息
        message_info = envelope.get("message_info", {})
        platform = message_info.get("platform", "")
        if platform != "qq":
            return EventDecision.SUCCESS, params

        # 检查功能开关
        config = getattr(self.plugin, "config", None)
        if config:
            features = getattr(config, "features", None)
            if features and not getattr(features, "enable_react", False):
                return EventDecision.SUCCESS, params

        message_segment = envelope.get("message_segment")
        if not message_segment or not isinstance(message_segment, list):
            return EventDecision.SUCCESS, params

        # 扫描所有 text 段，替换表情标记
        new_segments: list[dict[str, Any]] = []
        has_face = False

        for seg in message_segment:
            if not isinstance(seg, dict):
                new_segments.append(seg)
                continue

            seg_type = seg.get("type")
            if seg_type != "text":
                new_segments.append(seg)
                continue

            text_data = seg.get("data", "")
            if not isinstance(text_data, str) or not text_data:
                new_segments.append(seg)
                continue

            # 检查是否包含表情标记（支持中文方括号和英文方括号两种格式）
            if "【face:" not in text_data and "[face:" not in text_data and "表情：" not in text_data:
                new_segments.append(seg)
                continue

            # 处理文本，拆分为 text + face 段
            processed = _process_text_segment(text_data)
            if any(s.get("type") == "face" for s in processed):
                has_face = True
                new_segments.extend(processed)
                # 提取被替换的 face 标记，用于日志展示
                face_ids = [s["data"] for s in processed if s.get("type") == "face"]
                from rich.markup import escape
                logger.debug(
                    f"表情拦截器检测到 face 标记，原始文本: {escape(text_data)} → "
                    f"替换为 face 段: {face_ids}"
                )
            else:
                new_segments.append(seg)

        if has_face:
            envelope["message_segment"] = new_segments
            face_count = sum(1 for s in new_segments if s.get("type") == "face")
            logger.debug(f"表情拦截器已完成替换，共 {face_count} 个 face 段")

        return EventDecision.SUCCESS, params
