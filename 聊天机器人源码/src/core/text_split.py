# -*- coding: utf-8 -*-
"""回复分段发送：把较长、有自然分段的回复拆成 2~4 条连续消息。

产品要求（用户口径）：
- 文本本身有分段 → 按分段切成几条连续发送；
- 没有分段 → 在句号/问号/感叹号后按语义分几次发，但**不必每个句号都切**（合并成 2~4 段）；
- **逻辑性强的长文本一律不分段**：作文/论文/方案/报告/代码/表格/清单，
  以及带小标题、分点、“首先/其次/综上”这类分析结构的回答。

纯规则实现，不调用 AI；返回的片段按顺序拼接即为原文（去掉首尾空白与分段处换行差异）。
"""
from __future__ import annotations

import re
from typing import List

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
#: 强结构连接词：只要出现 ≥2 个，基本可判定是分析/论证型文本
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
#: 最短片段（低于此长度就并到相邻段）
_MIN_PART_CHARS = 12


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
    # 结构化回答：带小标题/分点（≥2 处）→ 逻辑性强，保持整段
    if len(_STRUCT_LINE_RE.findall(t)) >= 2:
        return True
    # 分析/论证型长文：“首先/其次/综上/一方面…”出现 ≥2 次即视为逻辑性文本
    if sum(t.count(w) for w in _STRONG_CONNECTORS) >= 2:
        return True
    if len(t) >= 200:
        hits = sum(t.count(w) for w in _ANALYSIS_CONNECTORS)
        if hits >= 3:
            return True
    # 很长的单块文字（没有分段、没有换行）像作文/长文，保持整段
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
            # 把连续的句末标点/右引号并进本句
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


def _merge_to_max(parts: List[str], max_parts: int, joiner: str = "") -> List[str]:
    """段数超过上限时，反复合并相邻最短的两段。"""
    parts = [p for p in parts if p]
    while len(parts) > max(1, int(max_parts)):
        idx = min(range(len(parts) - 1), key=lambda i: len(parts[i]) + len(parts[i + 1]))
        parts[idx] = parts[idx] + joiner + parts[idx + 1]
        parts.pop(idx + 1)
    return parts


def _group_sentences(sentences: List[str], target_chars: int, max_parts: int) -> List[str]:
    """把句子合并成若干段：每段尽量接近 target_chars，段数不超过 max_parts。"""
    if not sentences:
        return []
    parts: List[str] = []
    cur = ""
    for sent in sentences:
        if cur and len(cur) + len(sent) > target_chars:
            parts.append(cur)
            cur = sent
        else:
            cur += sent
    if cur:
        parts.append(cur)
    # 太短的尾段并到上一段
    while len(parts) > 1 and len(parts[-1]) < _MIN_PART_CHARS:
        parts[-2] += parts[-1]
        parts.pop()
    # 段数超上限 → 合并
    parts = _merge_to_max(parts, max_parts)
    # 每段再兜底：太短的段合并
    merged: List[str] = []
    for part in parts:
        if merged and len(part) < _MIN_PART_CHARS:
            merged[-1] += part
        else:
            merged.append(part)
    return [p.strip() for p in merged if p.strip()]


def split_for_send(text: str, user_text: str = "", min_chars: int = 80,
                   max_parts: int = 4, target_chars: int = 80) -> List[str]:
    """把回复切成按顺序发送的若干段；不需要/不适合分段时返回 [原文]。

    参数：
        text        回复正文（已去掉表情标记）
        user_text   触发这条回复的对方消息（用于判断“写作文/方案”这类请求）
        min_chars   短于该长度不分段
        max_parts   最多分几段
        target_chars 每段目标长度（按句合并时的软阈值）
    """
    t = str(text or "").strip()
    if not t:
        return []
    try:
        min_chars = max(20, int(min_chars or 80))
        max_parts = max(1, min(int(max_parts or 4), 6))
    except Exception:
        min_chars, max_parts = 80, 4
    if max_parts <= 1:
        return [t]
    # 逻辑性强的长文本一律整段发（作文/方案/代码/表格/清单/分析论证等）
    if _is_logical_longform(t, user_text):
        return [t]
    # 1) 有自然分段 → 按分段切（**不受“最少字数”限制**：两小段闲聊也要分开发）
    paras = [p.strip() for p in _PARA_RE.split(t) if p.strip()]
    if len(paras) >= 2:
        total = sum(len(p) for p in paras)
        if total >= 20 and all(len(p) >= 3 for p in paras):
            parts = _merge_to_max(paras, max_parts, joiner="\n")
            parts = [p for p in parts if p]
            if len(parts) >= 2:
                return parts
    # 2) 没有分段：短于 min_chars 就不拆（避免把一句短回复硬切成两条）
    if len(t) < min_chars:
        return [t]
    sentences = _split_sentences(t)
    if len(sentences) < 2:
        return [t]
    parts = _group_sentences(sentences, target_chars, max_parts)
    if len(parts) >= 2:
        return parts
    return [t]
