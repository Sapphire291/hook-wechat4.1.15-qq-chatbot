# -*- coding: utf-8 -*-
"""核心接口契约：接入层、AI、安全检测、策略、表情包。

后续任务（integration / ai / security / policy / emoji）都实现这里定义的抽象接口，
主流程（pipeline）只依赖这些抽象，保证各模块可独立开发与替换。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, List, Optional

from .models import (
    AppConfig,
    Message,
    Platform,
    SecurityEventType,
)


# --------------------------------------------------------------------------- #
# 接入层
# --------------------------------------------------------------------------- #
class ChatAdapter(ABC):
    """QQ / 微信 聊天接入适配器。"""

    platform: Platform

    def __init__(self, config: AppConfig):
        self.config = config
        self._on_message: Optional[Callable[[Message], None]] = None

    def set_on_message(self, callback: Callable[[Message], None]) -> None:
        self._on_message = callback

    def _dispatch(self, msg: Message) -> None:
        if self._on_message:
            self._on_message(msg)

    @abstractmethod
    def start(self) -> bool:
        """启动监听，成功返回 True。"""

    @abstractmethod
    def stop(self) -> None:
        """停止监听。"""

    @abstractmethod
    def available(self) -> bool:
        """检测依赖是否可用（库已安装 / 客户端已登录）。"""

    @abstractmethod
    def send_text(self, chat_id: str, text: str) -> bool:
        """向指定会话发送文本。"""

    @abstractmethod
    def send_image(self, chat_id: str, image_path: str) -> bool:
        """向指定会话发送图片（表情包贴图）。"""


# --------------------------------------------------------------------------- #
# AI 回复
# --------------------------------------------------------------------------- #
class ReplyGenerator(ABC):
    """AI 回复生成器（DeepSeek）。"""

    @abstractmethod
    def generate(
        self,
        system_prompt: str,
        style_instruction: str,
        history: List[dict],
        user_message: str,
        images: Optional[List[str]] = None,
    ) -> str:
        """根据系统提示词、风格、历史与用户消息（可含图片）生成回复文本。"""


# --------------------------------------------------------------------------- #
# 安全检测
# --------------------------------------------------------------------------- #
class SecurityDetector(ABC):
    """安全检测器，返回命中内容；未命中返回 None。"""

    event_type: SecurityEventType

    @abstractmethod
    def detect(self, message: Message) -> Optional[str]:
        """检测消息，命中则返回告警内容，否则 None。"""


# --------------------------------------------------------------------------- #
# 策略
# --------------------------------------------------------------------------- #
class WhitelistPolicy(ABC):
    """白名单策略（硬性规则）。"""

    @abstractmethod
    def is_allowed(self, message: Message) -> bool:
        """消息所在会话是否在白名单内。"""


class FrequencyPolicy(ABC):
    """回复频率策略（可切换、可配参）。"""

    @abstractmethod
    def should_reply(self, message: Message) -> bool:
        """判断是否允许对该消息回复。"""

    @abstractmethod
    def on_reply_sent(self, message: Message) -> None:
        """回复发送后回填状态。"""

    @abstractmethod
    def reset(self) -> None:
        """重置计数/时间状态。"""


# --------------------------------------------------------------------------- #
# 表情包
# --------------------------------------------------------------------------- #
class EmojiProvider(ABC):
    """表情包提供器（emoji 字符 + 本地图片库 + 平台原生表情）。"""

    @abstractmethod
    def pick_emoji(self, text: str) -> str:
        """根据语境选择合适 emoji 字符，返回 emoji 或空串。"""

    @abstractmethod
    def pick_image(self, text: str, platform: Optional["Platform"] = None) -> Optional[str]:
        """根据语境选择本地表情图片路径（可按平台选择，如微信收藏表情包），无合适则 None。"""

    @abstractmethod
    def pick_sticker(self, platform: Platform, text: str) -> str:
        """返回平台原生表情（QQ 用 CQ face、微信用表情文字），无匹配返回空串。"""
