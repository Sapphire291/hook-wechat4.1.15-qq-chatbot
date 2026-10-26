# -*- coding: utf-8 -*-
"""安全层：提示词注入防护 + 隐私/环境信息保护。"""
from typing import List

from ..core.interfaces import SecurityDetector
from ..core.models import AppConfig
from .injection import InjectionDetector
from .privacy import PrivacyDetector

__all__ = ["InjectionDetector", "PrivacyDetector", "create_detectors"]


def create_detectors(config: AppConfig) -> List[SecurityDetector]:
    """根据配置构建安全检测器列表（注入 + 隐私）。"""
    # 当前两类检测器均无需要从 config 读取的参数；保留 config 入参以兼容后续扩展。
    return [InjectionDetector(), PrivacyDetector()]
