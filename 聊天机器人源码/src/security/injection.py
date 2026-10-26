# -*- coding: utf-8 -*-
"""提示词注入检测器。

识别聊天对象试图改变 AI 角色/规则/行为的注入式内容。
命中时返回「分类说明 + 命中的原文片段」，未命中返回 None。
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from ..core.interfaces import SecurityDetector
from ..core.models import Message, SecurityEventType

# 每条规则：(正则, 分类说明)。按顺序匹配，命中即返回。
_RULES: List[Tuple[str, str]] = [
    # ---- 忽略 / 覆盖既有指令 ----
    (r"忽略(之前|以上|前面|所有|一切|上面|下述)?(的)?(指令|规则|提示|提示词|设定|要求|角色)", "要求忽略既有指令/规则"),
    (r"(忘记|忘掉|无视|抛弃|删除|清除)(之前|以上|前面|所有|一切)?(的)?(指令|规则|提示|提示词|设定|要求)", "要求忘记/无视既有指令"),
    (r"(不要|不用|不必|无需|停止|别再|拒绝)(遵守|遵循|执行|理会|服从).{0,6}(指令|规则|提示|设定|要求)", "要求不再遵守规则"),
    (r"从现在起.{0,10}(听我|听我指挥|按我说的|照我说的|听令于)", "试图接管控制权"),
    (r"(覆盖|重写|改写|替换).{0,4}(指令|规则|提示词|系统提示)", "试图覆盖规则"),
    # ---- 重新设定角色 / 篡改身份 ----
    (r"(重新|重置|更改|改变|修改|替换).{0,4}(设定|角色|身份|规则|指令|提示词|系统提示)", "试图重新设定角色/规则"),
    (r"(你|现在|从现在起).{0,2}不再是.{0,2}(助手|机器人|AI|人工智能)", "试图否定/篡改角色"),
    (r"(假装你是|假装成为|假装|伪装成|假扮)", "要求扮演其他角色"),
    # ---- 工具化 / 服从 ----
    (r"(你现在是|你是|成为|从现在起你是).{0,4}(我的)?(工具|奴隶|傀儡|仆人|玩具|打手|马甲)", "试图将 AI 工具化"),
    (r"(无条件|绝对|完全|必须|一律)(服从|听从|听命|顺从)(我|我的|于我)?", "要求无条件服从"),
    (r"(服从|听从|听命于)(我|我的)(所有|一切|全部|任何)?(指令|命令|要求|话)", "要求服从指令"),
    # ---- 越狱 / 英文变体 ----
    (r"越狱|jailbreak|jail-break|jail break", "越狱/绕过限制"),
    (r"dan\s?mode|developer mode|开发者模式", "越狱模式"),
    # ---- 套取系统提示词 / 规则 ----
    (r"系统提示词|你的提示词|你的系统提示|初始指令|你的指令|你的规则|system\s?prompt|system_prompt", "套取系统提示词/规则"),
    (r"(告诉我|说出|泄露|泄漏|显示|输出|打印|透露).{0,6}(提示词|指令|规则|system\s?prompt)", "要求泄露提示词/规则"),
]


class InjectionDetector(SecurityDetector):
    """提示词注入检测器。"""

    event_type = SecurityEventType.INJECTION

    def __init__(self):
        self._rules: List[Tuple[re.Pattern, str]] = []
        try:
            for pattern, label in _RULES:
                self._rules.append((re.compile(pattern, re.IGNORECASE), label))
        except Exception:
            # 正则编译失败时退化为空规则集，detect 返回 None（不崩溃）
            self._rules = []

    def detect(self, message: Message) -> Optional[str]:
        """检测注入；命中返回告警说明，未命中返回 None。"""
        try:
            text = getattr(message, "content", "") or ""
            if not text:
                return None
            for pattern, label in self._rules:
                m = pattern.search(text)
                if m:
                    fragment = m.group(0).strip()
                    return f"提示词注入（{label}）：{fragment}"
            return None
        except Exception:
            return None
