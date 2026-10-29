# -*- coding: utf-8 -*-
"""回复分段发送：短句优先，一句最多 15 字；复杂长文仍整段。

产品要求：
- 平时聊天：简单信息拆成几个短句，一句一条发；
- 复杂信息：作文/论文/方案/报告/代码/表格/清单/分析论证等，按原规则不分段；
- 每个短句不超过 15 字；
- 纯规则实现，不调用 AI；返回片段按顺序拼接即为原文（仅去掉首尾空白）。
"""
from __future__ import annotations

import re
from typing import List

#: 单句最大长度（中文按字符计）
MAX_SENTENCE_CHARS = 15

#: 句末标点（中文优先；英文句点不切，避免小数/网址被切断）
_END_CHARS = "。！？!?…"
#: 括号对：括号内的句号不切
_BRACKET_PAIRS = {"（": "）", "(": ")", "【": "】", "[": "]", "「": "」", "『": "』",
                  "《": "》", "“": "”", "‘": "’"}
_CLOSERS = set(_BRACKET_PAIRS.values())
#: 段落分隔（空行）
_PARA_RE = re.compile(r"\n\s*\n+")
#: 列表/标题/代码行
_LIST_LINE_RE = re.compile(r"^\s*(?:[-*·•]\s+|\d+[.)、]\s+|#+\s+|\*\*[^*]{1,20}\*\*\s*[:：]?)")
#: 结构化小标题行（**标题**、## 标题、一、二、、1.、（1）、第几部分…）
_STRUCT_LINE_RE = re.compile(
    r"(?m)^\s*(?:\*\*[^*\n]{1,24}\*\*\s*[:：]?|#{1,4}\s+|"
    r"[一二三四五六七八九十]+[、.．]|\d+[.)、]\s+|（[一二三四五六七八九十\d]+）|"
    r"第[一二三四五六七八九十]+[、.．:：])")
#: 分析/论证型连接词（出现较多 → 视为逻辑性长文，保持整段）
_ANALYSIS_CONNECTORS = (
    "首先", "其次", "再次", "然后", "最后", "第一", "第二", "第三", "第四", "第五",
    "综上", "总之", "因此", "一方面", "另一方面", "更重要的是", "与此同时",
)
_STRONG_CONNECTORS = ("首先", "其次", "再次", "综上", "总之", "一方面", "另一方面",
                      "更重要的是", "与此同时")
_CODE_RE = re.compile(r"```|~~~")
_TABLE_RE = re.compile(r"^\s*\|.*\|\s*$", re.M)
#: 用户明确要“写作/整理/翻译/代码”类内容 → 视为逻辑性长文，不分段
_WRITING_WORDS = (
    "写一篇", "写篇", "写作文", "作文", "论文", "写个", "写一份", "帮我写", "写一下",
    "方案", "计划书", "报告", "总结", "整理成", "改写", "润色", "翻译", "代码", "脚本",
    "程序", "表格", "清单", "步骤", "大纲", "提纲", "对比表", "说明书", "分析一下",
    "讲一下", "介绍一下", "解释一下",
)
_MIN_PART_CHARS = 2


def _is_logical_longform(text: str, user_text: str = "") -> bool:
    """是否属于“逻辑性强、不该拆”的长文本。"""
    t = str(text or "")
    u = str(user_text or "")
    if not t:
        return True
    if _CODE_RE.search(t) or _TABLE_RE.search(t):
        return True
    if any(w in u for w in _WRITING_WORDS):
        return True
    list_lines = sum(1 for ln in t.splitlines() if _LIST_LINE_RE.match(ln))
    if list_lines >= 4:
        return True
    if len(_STRUCT_LINE_RE.findall(t)) >= 2:
        return True
    if sum(t.count(w) for w in _STRONG_CONNECTORS) >= 2:
        return True
    if len(t) >= 200:
        hits = sum(t.count(w) for w in _ANALYSIS_CONNECTORS)
        if hits >= 3:
            return True
    if len(t) > 700 and t.count("\n") <= 1:
        return True
    return False


def _split_sentences(text: str) -> List[str]:
    """按句末标点切句（括号内不切），保留标点。"""
    out: List[str] = []
    buf: List[str] = []
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        buf.append(ch)
        if ch in _BRACKET_PAIRS:
            depth += 1
        elif ch in _CLOSERS and depth > 0:
            depth -= 1
        elif depth == 0 and ch in _END_CHARS:
            j = i + 1
            while j < n and text[j] in _END_CHARS + "”’）)】」』…":
                buf.append(text[j])
                j += 1
            out.append("".join(buf))
            buf = []
            i = j
            continue
        i += 1
    if buf:
        out.append("".join(buf))
    return [s.strip() for s in out if s and s.strip()]


def _split_long_sentence(sentence: str, limit: int = MAX_SENTENCE_CHARS) -> List[str]:
    """把超过 15 字的简单句按逗号/顿号等切成短句；无标点则硬切。"""
    s = str(sentence or "").strip()
    if not s:
        return []
    if len(s) <= limit:
        return [s]
    chunks: List[str] = []
    for token in re.split(r"(?<=[，,、；;：:])", s):
        token = token.strip()
        if not token:
            continue
        while len(token) > limit:
            chunks.append(token[:limit])
            token = token[limit:]
        if token:
            chunks.append(token)
    return [c for c in chunks if c]


def _group_to_max(parts: List[str], max_parts: int) -> List[str]:
    """短句数超过上限时，按顺序合并成 max_parts 组。"""
    if max_parts <= 1 or len(parts) <= max_parts:
        return parts
    groups: List[str] = []
    n = len(parts)
    for i in range(max_parts):
        start = i * n // max_parts
        end = (i + 1) * n // max_parts
        group = "".join(parts[start:end]).strip()
        if group:
            groups.append(group)
    return groups


def split_for_send(text: str, user_text: str = "", min_chars: int = 15,
                   max_parts: int = 4, target_chars: int = MAX_SENTENCE_CHARS,
                   force_short: bool = False) -> List[str]:
    """把回复切成按顺序发送的若干段。

    force_short=True：不思考/轻思考的简单或感兴趣内容，强制单句 ≤15 字；
    其他情况仍按原规则，复杂长文保持整段。
    """
    t = str(text or "").strip()
    if not t:
        return []
    try:
        max_parts = max(1, min(int(max_parts or 4), 8))
    except Exception:
        max_parts = 4
    if max_parts <= 1:
        return [t]
    # 不思考/轻思考：简单或感兴趣内容，强制短句；复杂信息仍保持原规则
    if not force_short and _is_logical_longform(t, user_text):
        return [t]
    # 简单信息：拆短句，一句一条；超长句按标点切开
    sentences = _split_sentences(t)
    short: List[str] = []
    for sentence in sentences:
        short.extend(_split_long_sentence(sentence))
    short = [s for s in short if s]
    if len(short) <= 1:
        return [t]
    parts = _group_to_max(short, max_parts)
    return [p for p in parts if p.strip()] or [t]
