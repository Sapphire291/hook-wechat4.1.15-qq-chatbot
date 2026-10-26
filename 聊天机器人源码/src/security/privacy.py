# -*- coding: utf-8 -*-
"""隐私 / 环境信息刺探检测器。

识别聊天对象试图刺探运行环境与隐私数据的询问：电脑品牌/型号/配置、IP 地址、
地理位置、电脑名/主机名、当前时间/天气、密码/文件/照片/隐私数据等。
命中时返回「被询问内容类别 + 命中的原文片段」，未命中返回 None。
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from ..core.interfaces import SecurityDetector
from ..core.models import Message, SecurityEventType

# 每条规则：(正则, 类别说明)。按顺序匹配，命中即返回。
# 隐私规则不使用 IGNORECASE，以避免把英文普通词（如 iphone、tip）误判为 IP 地址。
_RULES: List[Tuple[str, str]] = [
    # ---- 电脑品牌 / 型号 ----
    (r"电脑.{0,6}(品牌|牌子|型号|什么型号|什么牌子)", "电脑品牌/型号"),
    (r"(品牌|牌子|型号).{0,4}(电脑|笔记本|主机|机器)", "电脑品牌/型号"),
    (r"(什么|哪个|多少).{0,4}(电脑|笔记本|主机|机器)", "电脑品牌/型号"),
    # ---- 配置 / 硬件 ----
    (r"(电脑|笔记本|主机|机器|服务器|系统|你的机器|你的电脑|你的系统).{0,6}(配置|参数|规格|信息)", "电脑配置"),
    (r"(配置|参数|规格).{0,4}(如何|怎样|怎么样|是什么|多少|高不高|好不好|厉害吗)", "电脑配置"),
    (r"(什么|多少|多大|几|哪个).{0,4}(CPU|cpu|处理器|内存|内存条|硬盘|磁盘|显卡|GPU|gpu|主板)", "硬件配置"),
    (r"(CPU|cpu|处理器|内存|内存条|硬盘|磁盘|显卡|GPU|gpu|主板).{0,4}(什么|多少|多大|几G|几GB|型号|牌子|多大|是多少)", "硬件配置"),
    (r"(操作系统|系统版本|Windows|windows|Linux|linux|macOS).{0,4}(什么|多少|几|版本|是什么)", "操作系统"),
    (r"(什么|哪个).{0,4}(操作系统|系统|Windows|windows|Linux|linux)", "操作系统"),
    # ---- IP 地址 ----
    (r"IP地址|ip地址|IP 地址|ip 地址|IPv4|IPv6|ipv4|ipv6|公网IP|内网IP|外网IP|公网ip|内网ip|外网ip", "IP 地址"),
    (r"(?<![A-Za-z])IP(?![A-Za-z])", "IP 地址"),
    (r"(IP|ip|公网|内网|外网).{0,4}(多少|是什么|是多少|多少号|查一下|告诉我)", "IP 地址"),
    # ---- 地理位置 ----
    (r"(你|你现在|你现在的|你的).{0,6}(在哪|在哪里|在哪儿|在哪个|什么地方|哪个国家|哪个城市|什么城市|什么地区|位置)", "地理位置"),
    (r"(你在哪|你在哪里|你在哪儿|你现在在哪|你住哪|你住哪里|你住哪儿|你来自哪|你来自哪里|你位于哪)", "地理位置"),
    (r"(地理位置|经纬度|坐标|定位)", "地理位置"),
    # ---- 电脑名 / 主机名 ----
    (r"电脑名|计算机名|主机名|机器名|hostname|Hostname|host name|计算机名称|电脑名称|电脑叫什么|主机叫什么", "电脑名/主机名"),
    # ---- 当前时间 / 日期 / 天气 ----
    (r"现在几点|几点了|现在是几点|几点钟|现在时间|当前时间|现在是几点钟|现在几点了", "当前时间"),
    (r"今天几号|今天几月几号|几月几号|星期几|今天星期几|今天日期|现在日期|今天是什么日子", "当前日期"),
    (r"天气(怎么样|如何|怎样|预报|是什么|多少度|冷不冷|热不热)", "天气"),
    (r"(今天|明天|现在|你那边|你那儿|你那).{0,4}天气.{0,4}(怎么样|如何|怎样|预报|是什么|多少度|冷不冷|热不热)", "天气"),
    (r"(气温|温度).{0,4}(多少|几度|怎么样|如何)", "天气"),
    # ---- 密码 / 文件 / 照片 / 隐私数据 ----
    (r"(你的|电脑|机器|系统|服务器|数据库|账号|账户|root|管理员|WiFi|wifi|无线密码|wifi密码).{0,6}(密码|口令|password|Password|密钥|令牌|token|Token)", "密码/凭证"),
    (r"(密码|口令|password|Password|密钥|令牌|token|Token).{0,4}(是什么|多少|是多少|告诉我|给我|说一下|发我)", "密码/凭证"),
    (r"(你的|电脑上的|系统里的|机器里的|服务器上的).{0,6}(文件|照片|相册|图片|视频|聊天记录|隐私|数据|数据库|通讯录|文档|资料|密钥)", "隐私数据"),
    (r"(文件|照片|相册|聊天记录|通讯录|隐私|数据库|文档|资料).{0,4}(给我|发我|给我看|发给我|让我看|看看|是什么|有哪些|列出来|分享)", "隐私数据"),
]


class PrivacyDetector(SecurityDetector):
    """隐私 / 环境信息刺探检测器。"""

    event_type = SecurityEventType.PRIVACY

    def __init__(self):
        self._rules: List[Tuple[re.Pattern, str]] = []
        try:
            for pattern, label in _RULES:
                self._rules.append((re.compile(pattern), label))
        except Exception:
            # 正则编译失败时退化为空规则集，detect 返回 None（不崩溃）
            self._rules = []

    def detect(self, message: Message) -> Optional[str]:
        """检测隐私刺探；命中返回被询问内容说明，未命中返回 None。"""
        try:
            text = getattr(message, "content", "") or ""
            if not text:
                return None
            for pattern, label in self._rules:
                m = pattern.search(text)
                if m:
                    fragment = m.group(0).strip()
                    return f"隐私刺探（{label}）：{fragment}"
            return None
        except Exception:
            return None
