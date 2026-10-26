# -*- coding: utf-8 -*-
"""联网检索内核（阶段 A）：搜索 provider / 网页抓取 / 决策与上下文拼装。

对齐 DSH 的 web_search / web_fetch 两个工具的思路：
- 搜索只给来源（标题 + URL + 摘要），抓取只取正文；
- 外部内容一律视为「不可信数据」，绝不当作指令执行；
- 回复引用来源 URL；查不到就明说「没查到」，不编造；
- 只允许公网 http/https（拒绝内网/本机/元数据地址），限频与每日配额。

模块：
    search.py       搜索 provider（serper/bing/brave/tavily/searxng + DDG/Bing 网页兜底）
    fetch.py        网页抓取（SSRF 防护 / 超时 / 2MB 上限 / 正文提取 / 截断）
    web_context.py  联网决策（廉价 AI）/ 缓存 / 注入检测 / 隐私脱敏 / 拼装 / 日志与账本
"""
from __future__ import annotations

from .fetch import FetchResult, extract_text, fetch, is_safe_public_url, resolve_url
from .quote import detect_stock_code, fetch_quote, parse_quote, quote_source
from .search import SearchResult, available_providers, search, search_multi
from .web_context import WebContext, scan_injection, sanitize_query

__all__ = [
    "FetchResult",
    "SearchResult",
    "WebContext",
    "available_providers",
    "detect_stock_code",
    "extract_text",
    "fetch",
    "fetch_quote",
    "is_safe_public_url",
    "parse_quote",
    "quote_source",
    "resolve_url",
    "sanitize_query",
    "scan_injection",
    "search",
    "search_multi",
]
