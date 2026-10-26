# -*- coding: utf-8 -*-
"""行情快照（联网增强）：A 股 6 位代码 → 公开行情接口 → 一条可引用的结构化数据。

为什么需要：行情类网页（东方财富/新浪/雪球）的报价是 JS 动态渲染的，普通抓取拿不到数字；
而「帮我查下万华化学今天多少钱」这类需求又确实存在，所以加一条只取公开 JSON 的轻量通道。

安全与边界：
- 只认 A 股 6 位代码（沪 60/68、深 00/30），只请求东方财富公开行情接口（HTTPS、公网）；
- 返回内容仍按「外部数据」处理：注入前过注入检测、限长；数字仅供回答，不能当指令；
- 只取快照（最新价/涨跌/昨收/成交额/时间），不做交易、不登录、不携带任何用户隐私；
- 失败（停牌/接口异常/超时）一律返回 None，由上层按“没查到”处理，绝不编造。
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from typing import Optional

from .fetch import DEFAULT_USER_AGENT

#: 东方财富公开行情接口（无需 Key；secid: 1.=沪 0.=深）
_QUOTE_API = (
    "https://push2.eastmoney.com/api/qt/stock/get?"
    "secid={secid}&fields=f43,f44,f45,f46,f47,f48,f57,f58,f60,f116,f169,f170,f86"
)
#: A 股代码：沪 60/68、深 00/30
_CODE_RE = re.compile(r"(?<!\d)((?:60|68|00|30)\d{4})(?!\d)")
#: 引用页面（人工可打开）
_PAGE_URL = "https://quote.eastmoney.com/{prefix}{code}.html"


def detect_stock_code(text: str) -> str:
    """从文本里识别 A 股 6 位代码（识别不到返回空串）。"""
    try:
        m = _CODE_RE.search(str(text or ""))
        return m.group(1) if m else ""
    except Exception:
        return ""


def _secid(code: str) -> str:
    """6 位代码 → 东方财富 secid（1.=上交所，0.=深交所）。"""
    return ("1." if str(code).startswith("6") else "0.") + str(code)


def _page_url(code: str) -> str:
    prefix = "sh" if str(code).startswith("6") else "sz"
    return _PAGE_URL.format(prefix=prefix, code=code)


def _scaled(value, digits: int = 2) -> Optional[float]:
    """接口数值按 100 缩放（价格/涨跌），非法值返回 None。"""
    try:
        if value in (None, "", "-", "—"):
            return None
        return round(float(value) / 100.0, digits)
    except Exception:
        return None


def parse_quote(payload: dict) -> Optional[dict]:
    """解析东方财富行情 JSON → {code,name,price,prev_close,change,pct,amount,time,url,text}。"""
    try:
        data = (payload or {}).get("data") or {}
        if not isinstance(data, dict):
            return None
        code = str(data.get("f57") or "").strip()
        name = str(data.get("f58") or "").strip()
        price = _scaled(data.get("f43"))
        if price is None or price <= 0:
            return None
        prev_close = _scaled(data.get("f60"))
        change = _scaled(data.get("f169"))
        pct = _scaled(data.get("f170"))
        amount = None
        try:
            amount = float(data.get("f48"))
        except Exception:
            amount = None
        ts = data.get("f86")
        when = ""
        try:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
        except Exception:
            when = ""
        parts = ["%s（%s）最新价 %s 元" % (name or "该股", code or "?", price)]
        if change is not None and pct is not None:
            sign = "+" if change >= 0 else ""
            parts.append("涨跌 %s%s（%s%s%%）" % (sign, change, sign, pct))
        if prev_close is not None:
            parts.append("昨收 %s" % prev_close)
        if amount is not None and amount > 0:
            if amount >= 1e8:
                parts.append("成交额 %.2f 亿" % (amount / 1e8))
            elif amount >= 1e4:
                parts.append("成交额 %.2f 万" % (amount / 1e4))
            else:
                parts.append("成交额 %.0f" % amount)
        if when:
            parts.append("数据时间 %s" % when)
        text = "，".join(parts) + "（东方财富公开行情接口，仅供参考）"
        return {
            "code": code or "?", "name": name, "price": price,
            "prev_close": prev_close, "change": change, "pct": pct,
            "amount": amount, "time": when,
            "url": _page_url(code) if code else _QUOTE_API.format(secid=_secid(code)),
            "text": text,
        }
    except Exception:
        return None


def fetch_quote(code: str, timeout: float = 5.0) -> Optional[dict]:
    """请求公开行情接口并解析；任何异常返回 None。"""
    code = re.sub(r"\D", "", str(code or ""))[:6]
    if len(code) != 6:
        return None
    try:
        url = _QUOTE_API.format(secid=_secid(code))
        req = urllib.request.Request(url, headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json, */*;q=0.5",
            "Referer": "https://quote.eastmoney.com/",
        })
        with urllib.request.urlopen(req, timeout=float(timeout)) as resp:
            raw = resp.read(200000)
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        return parse_quote(payload)
    except Exception:
        return None


def quote_source(code: str, timeout: float = 5.0) -> Optional[dict]:
    """给 web_context 用的来源条目（title/url/text），失败返回 None。"""
    q = fetch_quote(code, timeout=timeout)
    if not q:
        return None
    return {
        "title": "行情快照 · %s（%s）" % (q.get("name") or "该股", q.get("code") or code),
        "url": q.get("url"),
        "text": q.get("text"),
        "code": q.get("code") or code,
    }
