# -*- coding: utf-8 -*-
"""联网搜索（web_search）：只返回来源（标题 / URL / 摘要），不抓正文。

provider（可在设置里选，默认 auto）：
- auto        自动：360 网页 → Bing 网页 → 百度网页 → DuckDuckGo（全部免 Key，按可靠性降级）
- so_html     360 搜索网页版（免 Key；结果为 so.com/link 跳转，代码会解析出真实地址）
- bing_html   Bing 网页版（免 Key；对“短而准”的查询命中率最好）
- baidu_html  百度网页版（免 Key；结果不稳定，作为补充）
- ddg         DuckDuckGo 网页版（免 Key；本机常被反爬，排最后）
- serper / bing / brave / tavily / searxng   需 Key 的正式 API（质量最稳）

设计（对齐 DSH 的 web_search）：
- 支持**多查询**（一次 2~3 个短查询并行，合并去重），因为长句查询在中文网页搜索里命中很差；
- 每个结果都尽量解析成**真实 URL**（360/百度跳转链接会被还原），方便模型引用与后续抓取；
- 过滤“最新相关消息/其他人还搜了”这类聚合卡片；
- 任何网络/解析异常都收敛为空结果，不向调用方抛异常。
"""
from __future__ import annotations

import html as html_lib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .fetch import DEFAULT_USER_AGENT, is_safe_public_url, resolve_url


@dataclass
class SearchResult:
    """一条搜索结果（来源）。"""
    title: str
    url: str
    snippet: str = ""
    provider: str = ""
    real_url: str = ""      # 解析跳转后的真实地址（可能为空）

    def to_dict(self) -> dict:
        return {"title": self.title, "url": self.url, "snippet": self.snippet,
                "provider": self.provider, "real_url": self.real_url}

    @staticmethod
    def from_dict(d: dict) -> "SearchResult":
        return SearchResult(
            title=str(d.get("title") or ""),
            url=str(d.get("url") or ""),
            snippet=str(d.get("snippet") or ""),
            provider=str(d.get("provider") or ""),
            real_url=str(d.get("real_url") or ""),
        )


#: 各 provider 的展示名（UI 用）
PROVIDER_LABELS = {
    "auto": "自动（免 Key 多引擎）",
    "so_html": "360 网页兜底（免 Key）",
    "bing_html": "Bing 网页兜底（免 Key）",
    "baidu_html": "百度网页兜底（免 Key）",
    "ddg": "DuckDuckGo 兜底（免 Key）",
    "serper": "Serper（需 Key）",
    "bing": "Bing API（需 Key）",
    "brave": "Brave（需 Key）",
    "tavily": "Tavily（需 Key）",
    "searxng": "SearXNG（自建）",
}
PROVIDER_VALUES = {v: k for k, v in PROVIDER_LABELS.items()}
_PROVIDER_ORDER = ("auto", "so_html", "bing_html", "baidu_html", "ddg",
                   "serper", "bing", "brave", "tavily", "searxng")
#: 免 Key 引擎的默认降级顺序（360 最稳 → Bing 对精确短词最好 → 百度 → DDG）
_KEYLESS_CHAIN = ("so_html", "bing_html", "baidu_html", "ddg")
#: 单次搜索最多合并几个查询
MAX_QUERIES = 3

_UA = DEFAULT_USER_AGENT
#: 共享会话 Cookie（部分搜索引擎需要）
_COOKIE_JAR = None
_COOKIE_LOCK = threading.Lock()

# --------------------------------------------------------------------------- #
# 熔断：某 provider 连续失败后在短时间内不再优先尝试（避免每次白等 1~2 秒）
# --------------------------------------------------------------------------- #
_BREAKER_LOCK = threading.Lock()
_BREAKER: Dict[str, float] = {}
_BREAKER_SECONDS = 600.0


def _breaker_open(name: str) -> bool:
    try:
        with _BREAKER_LOCK:
            until = float(_BREAKER.get(name) or 0.0)
        return until > time.time()
    except Exception:
        return False


def _breaker_fail(name: str) -> None:
    try:
        with _BREAKER_LOCK:
            _BREAKER[name] = time.time() + _BREAKER_SECONDS
    except Exception:
        pass


def _breaker_ok(name: str) -> None:
    try:
        with _BREAKER_LOCK:
            _BREAKER.pop(name, None)
    except Exception:
        pass


def _opener():
    """带 Cookie 的 opener（搜索引擎跳转/反爬常依赖同一会话）。"""
    global _COOKIE_JAR
    try:
        with _COOKIE_LOCK:
            if _COOKIE_JAR is None:
                import http.cookiejar
                _COOKIE_JAR = http.cookiejar.CookieJar()
            return urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(_COOKIE_JAR),
                _NoRedirect())
    except Exception:
        return urllib.request.build_opener()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(url: str, timeout: float, data: Optional[bytes] = None,
             headers: Optional[dict] = None, method: Optional[str] = None) -> bytes:
    h = {
        "User-Agent": _UA,
        "Accept": "application/json, text/html;q=0.9, */*;q=0.5",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with _opener().open(req, timeout=float(timeout)) as resp:
        return resp.read(2 * 1024 * 1024)


def _get_json(url: str, timeout: float, headers: Optional[dict] = None) -> dict:
    raw = _request(url, timeout, headers=headers)
    return json.loads(raw.decode("utf-8", errors="replace"))


def _post_json(url: str, payload: dict, timeout: float, headers: Optional[dict] = None) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    raw = _request(url, timeout, data=body, headers=h, method="POST")
    return json.loads(raw.decode("utf-8", errors="replace"))


def _clean_text(value) -> str:
    """去标签 + 反转义 + 压缩空白。"""
    try:
        s = re.sub(r"(?s)<[^>]+>", " ", str(value or ""))
        s = html_lib.unescape(s)
        s = re.sub(r"[\s\u00a0\u3000]+", " ", s)
        return s.strip()
    except Exception:
        return ""


def _norm_url(url: str) -> str:
    """只保留合法的 http/https 结果 URL。"""
    try:
        u = str(url or "").strip()
        if u.startswith("//"):
            u = "https:" + u
        parts = urllib.parse.urlsplit(u)
        if parts.scheme.lower() not in ("http", "https"):
            return ""
        if not parts.hostname:
            return ""
        return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "/",
                                        parts.query, ""))
    except Exception:
        return ""


def _ddg_decode(url: str) -> str:
    """解 DuckDuckGo 跳转链接（//duckduckgo.com/l/?uddg=<url>）。"""
    try:
        u = str(url or "")
        if "uddg=" in u:
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)
            target = (qs.get("uddg") or [""])[0]
            if target:
                return urllib.parse.unquote(target)
    except Exception:
        pass
    return url


#: “最新相关信息 / 其他人还搜了”这类聚合卡片（不是正文来源）
_AGGREGATE_WORDS = ("最新相关信息", "最新相关消息", "其他人还搜", "相关搜索", "大家还在搜",
                    "为您推荐", "热门推荐")


def _is_aggregate(title: str, url: str) -> bool:
    try:
        t = str(title or "")
        if any(w in t for w in _AGGREGATE_WORDS):
            return True
        u = str(url or "")
        host = (urllib.parse.urlsplit(u).hostname or "").lower()
        if host.endswith("baidu.com") and "/s?" in u:
            return True
        if host.endswith("so.com") and "/ns?" in u:
            return True
        return False
    except Exception:
        return False


def _dedupe(results: List[SearchResult], max_results: int) -> List[SearchResult]:
    out: List[SearchResult] = []
    seen = set()
    seen_titles = set()
    for r in results:
        u = _norm_url(r.real_url or r.url)
        if not u or u in seen:
            continue
        title_key = re.sub(r"\W+", "", _clean_text(r.title))[:40]
        if title_key and title_key in seen_titles:
            continue
        seen.add(u)
        if title_key:
            seen_titles.add(title_key)
        if not r.real_url:
            r.real_url = ""
        r.title = _clean_text(r.title)[:200] or u
        r.snippet = _clean_text(r.snippet)[:500]
        if not r.url:
            r.url = u
        out.append(r)
        if len(out) >= max_results:
            break
    return out


# --------------------------------------------------------------------------- #
# HTML 结果页通用解析（360 / 百度 都是 h3 + a 结构）
# --------------------------------------------------------------------------- #
_H3_RE = re.compile(r"<h3[^>]*>", re.I)
_A_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_SNIPPET_RES = (
    re.compile(r'<p[^>]+class="[^"]*(?:res-desc|abstract|content-right|c-abstract|c-span-last)[^"]*"[^>]*>(.*?)</p>', re.S | re.I),
    re.compile(r'<(?:div|span|dd)[^>]+class="[^"]*(?:res-desc|abstract|content-right|c-abstract|content)[^"]*"[^>]*>(.*?)</(?:div|span|dd)>', re.S | re.I),
)


def _parse_h3_links(text: str, max_items: int = 12) -> List[Tuple[str, str, str]]:
    """解析 HTML 结果页里 <h3><a> 结构的 (title, href, snippet)；兼容 h3 内嵌套 div。"""
    out: List[Tuple[str, str, str]] = []
    try:
        for m in _H3_RE.finditer(text):
            end = text.find("</h3>", m.start())
            if end < 0:
                continue
            chunk = text[m.start():end + 5]
            a = _A_RE.search(chunk)
            if not a:
                continue
            href = html_lib.unescape(a.group(1)).strip()
            title = _clean_text(a.group(2))
            if not href or not title or len(title) < 2:
                continue
            # 摘要：h3 之后 2500 字符内找第一个像摘要的块
            snippet = ""
            tail = text[end:end + 2500]
            for pattern in _SNIPPET_RES:
                sm = pattern.search(tail)
                if sm:
                    cand = _clean_text(sm.group(1))
                    if 8 <= len(cand) <= 400:
                        snippet = cand
                        break
            out.append((title, href, snippet))
            if len(out) >= max_items:
                break
    except Exception:
        return out
    return out


# --------------------------------------------------------------------------- #
# 跳转链接解析（把搜索结果变成真实地址）
# --------------------------------------------------------------------------- #
_SO_JS_RE = re.compile(r'''location\.replace\(\s*["']([^"']+)["']''', re.I)
_SO_META_RE = re.compile(r'''http-equiv=["']?refresh["']?[^>]*content=["'][^"']*url=([^"';]+)''', re.I)


def _parse_so_js_target(text: str) -> str:
    """从 360 跳转页（282 字节 JS/meta 跳转）里解析真实 URL。"""
    try:
        body = str(text or "")
        for pattern in (_SO_JS_RE, _SO_META_RE):
            m = pattern.search(body)
            if m:
                target = html_lib.unescape(m.group(1)).strip()
                if target.startswith("//"):
                    target = "https:" + target
                ok, _ = is_safe_public_url(target)
                return target if ok else ""
    except Exception:
        return ""
    return ""


def _resolve_so_link(url: str, timeout: float = 4.0) -> str:
    """360 跳转页是 282 字节的 JS 跳转 → 解析出真实 URL。"""
    try:
        raw = _request(url, timeout, headers={"Referer": "https://www.so.com/",
                                              "Accept": "text/html,*/*;q=0.8"})
        return _parse_so_js_target(raw.decode("utf-8", errors="replace"))
    except Exception:
        return ""


def _resolve_link(url: str, provider: str, timeout: float = 4.0) -> str:
    """统一解析：360 走 JS 解析，百度走安全重定向跟随。"""
    if not url:
        return ""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if provider == "so_html" and host.endswith("so.com"):
            return _resolve_so_link(url, timeout)
        if provider == "baidu_html" and host.endswith("baidu.com"):
            ok, _ = is_safe_public_url(url)
            if not ok:
                return ""
            return resolve_url(url, timeout=timeout, max_redirects=3)
        return url
    except Exception:
        return ""


def _resolve_many(items: List[dict], provider: str, timeout: float, limit: int) -> None:
    """并发解析前 limit 条结果的真实地址（就地更新 real_url）。"""
    targets = items[:limit]
    if not targets:
        return
    try:
        with ThreadPoolExecutor(max_workers=min(5, len(targets))) as pool:
            futures = {pool.submit(_resolve_link, it.get("url") or "", provider, timeout): it
                       for it in targets}
            for fut in as_completed(futures, timeout=timeout + 4):
                item = futures[fut]
                try:
                    real = fut.result()
                except Exception:
                    real = ""
                if real:
                    item["real_url"] = real
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Provider 实现
# --------------------------------------------------------------------------- #
class SearchProvider:
    """搜索结果提供者基类。"""

    name = ""
    requires_key = False

    def __init__(self, api_key: str = "", base_url: str = "", timeout: float = 8.0):
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "").strip()
        self.timeout = float(timeout or 8.0)

    def available(self) -> Tuple[bool, str]:
        if self.requires_key and not self.api_key:
            return False, "未配置 API Key"
        return True, ""

    def search(self, query: str, max_results: int) -> List[SearchResult]:
        raise NotImplementedError

    # -- 通用：把 (title,href,snippet) 转成结果并解析真实 URL --
    def _build_html_results(self, parsed, max_results: int) -> List[SearchResult]:
        items = []
        for title, href, snippet in parsed:
            if _is_aggregate(title, href):
                continue
            items.append({"title": title, "url": href, "snippet": snippet,
                          "provider": self.name, "real_url": ""})
            if len(items) >= max_results:
                break
        _resolve_many(items, self.name, min(self.timeout, 4.0), max_results)
        return [SearchResult(**it) for it in items]


class SerperProvider(SearchProvider):
    name = "serper"
    requires_key = True

    def search(self, query: str, max_results: int) -> List[SearchResult]:
        payload = {"q": query, "num": int(max_results), "hl": "zh-cn", "gl": "cn"}
        data = _post_json("https://google.serper.dev/search", payload, self.timeout,
                          headers={"X-API-KEY": self.api_key})
        out = []
        for item in (data.get("organic") or []):
            if not isinstance(item, dict):
                continue
            out.append(SearchResult(title=str(item.get("title") or ""),
                                    url=str(item.get("link") or ""),
                                    snippet=str(item.get("snippet") or ""),
                                    provider=self.name))
        return out


class BingApiProvider(SearchProvider):
    name = "bing"
    requires_key = True

    def search(self, query: str, max_results: int) -> List[SearchResult]:
        url = ("https://api.bing.microsoft.com/v7.0/search?"
               + urllib.parse.urlencode({"q": query, "count": int(max_results),
                                         "mkt": "zh-CN", "responseFilter": "Webpages"}))
        data = _get_json(url, self.timeout,
                         headers={"Ocp-Apim-Subscription-Key": self.api_key})
        out = []
        for item in (((data.get("webPages") or {}).get("value")) or []):
            if not isinstance(item, dict):
                continue
            out.append(SearchResult(title=str(item.get("name") or ""),
                                    url=str(item.get("url") or ""),
                                    snippet=str(item.get("snippet") or ""),
                                    provider=self.name))
        return out


class BraveProvider(SearchProvider):
    name = "brave"
    requires_key = True

    def search(self, query: str, max_results: int) -> List[SearchResult]:
        url = ("https://api.search.brave.com/res/v1/web/search?"
               + urllib.parse.urlencode({"q": query, "count": int(max_results),
                                         "search_lang": "zh-hans"}))
        data = _get_json(url, self.timeout, headers={
            "X-Subscription-Token": self.api_key, "Accept": "application/json"})
        out = []
        for item in ((data.get("web") or {}).get("results") or []):
            if not isinstance(item, dict):
                continue
            out.append(SearchResult(title=str(item.get("title") or ""),
                                    url=str(item.get("url") or ""),
                                    snippet=str(item.get("description") or ""),
                                    provider=self.name))
        return out


class TavilyProvider(SearchProvider):
    name = "tavily"
    requires_key = True

    def search(self, query: str, max_results: int) -> List[SearchResult]:
        payload = {"api_key": self.api_key, "query": query,
                   "max_results": int(max_results), "search_depth": "basic",
                   "include_answer": False, "include_raw_content": False}
        data = _post_json("https://api.tavily.com/search", payload, self.timeout,
                          headers={"Authorization": "Bearer " + self.api_key})
        out = []
        for item in (data.get("results") or []):
            if not isinstance(item, dict):
                continue
            out.append(SearchResult(title=str(item.get("title") or ""),
                                    url=str(item.get("url") or ""),
                                    snippet=str(item.get("content") or ""),
                                    provider=self.name))
        return out


class SearxngProvider(SearchProvider):
    name = "searxng"

    def available(self) -> Tuple[bool, str]:
        if not self.base_url:
            return False, "未配置 SearXNG 地址"
        if not re.match(r"(?i)^https?://", self.base_url):
            return False, "SearXNG 地址必须以 http(s):// 开头"
        return True, ""

    def search(self, query: str, max_results: int) -> List[SearchResult]:
        base = self.base_url.rstrip("/")
        url = base + "/search?" + urllib.parse.urlencode({
            "q": query, "format": "json", "language": "zh-CN", "safesearch": "1"})
        headers = {}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        data = _get_json(url, self.timeout, headers=headers)
        out = []
        for item in (data.get("results") or []):
            if not isinstance(item, dict):
                continue
            out.append(SearchResult(title=str(item.get("title") or ""),
                                    url=str(item.get("url") or ""),
                                    snippet=str(item.get("content") or ""),
                                    provider=self.name))
        return out


class SoHtmlProvider(SearchProvider):
    """360 搜索网页版（免 Key；本机实测对中文时事/财经最稳）。"""
    name = "so_html"

    def search(self, query: str, max_results: int) -> List[SearchResult]:
        url = "https://www.so.com/s?" + urllib.parse.urlencode({"q": query})
        raw = _request(url, self.timeout, headers={
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.so.com/"})
        text = raw.decode("utf-8", errors="replace")
        parsed = _parse_h3_links(text, max_items=max_results + 4)
        if not parsed:
            # 可能命中了验证页
            raise RuntimeError("360 结果页解析为空（疑似验证页，len=%d）" % len(text))
        return self._build_html_results(parsed, max_results)


class BingHtmlProvider(SearchProvider):
    """Bing 网页版（免 Key；查询越短越准）。"""
    name = "bing_html"

    def search(self, query: str, max_results: int) -> List[SearchResult]:
        last_err = ""
        for host in ("https://cn.bing.com/search?", "https://www.bing.com/search?"):
            try:
                url = host + urllib.parse.urlencode({
                    "q": query, "setmkt": "zh-CN", "setlang": "zh-CN",
                    "count": int(max_results)})
                raw = _request(url, self.timeout, headers={
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                    "Referer": "https://cn.bing.com/"})
                text = raw.decode("utf-8", errors="replace")
                items = _parse_bing_html(text, max_results)
                if items:
                    return items
                last_err = "%s 无结果" % host.split("//")[1].split("/")[0]
            except Exception as e:
                last_err = "%r" % (e,)
        raise RuntimeError(last_err or "Bing 网页无结果")


_BING_CHUNK_RE = re.compile(r'<li class="b_algo"', re.I)
_BING_H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S | re.I)
_BING_A_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_BING_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S | re.I)


def _parse_bing_html(text: str, max_results: int = 10) -> List[SearchResult]:
    """解析 Bing 网页结果（li.b_algo 块内的 h2>a 与首个 p）。"""
    out: List[SearchResult] = []
    try:
        for chunk in _BING_CHUNK_RE.split(text)[1:]:
            h2 = _BING_H2_RE.search(chunk)
            if not h2:
                continue
            a = _BING_A_RE.search(h2.group(1))
            if not a:
                continue
            href = html_lib.unescape(a.group(1))
            if not re.match(r"(?i)^https?://", href):
                continue
            title = _clean_text(h2.group(1))
            if _is_aggregate(title, href):
                continue
            snippet = ""
            p = _BING_P_RE.search(chunk)
            if p:
                snippet = _clean_text(p.group(1))
            out.append(SearchResult(title=title, url=href, snippet=snippet,
                                    provider="bing_html"))
            if len(out) >= max_results:
                break
    except Exception:
        return out
    return out


class BaiduHtmlProvider(SearchProvider):
    """百度网页版（免 Key；结果不稳定，作为补充）。"""
    name = "baidu_html"

    def search(self, query: str, max_results: int) -> List[SearchResult]:
        url = "https://www.baidu.com/s?" + urllib.parse.urlencode({"wd": query, "rn": 10})
        raw = _request(url, self.timeout, headers={
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.baidu.com/"})
        text = raw.decode("utf-8", errors="replace")
        parsed = _parse_h3_links(text, max_items=max_results + 4)
        if not parsed:
            raise RuntimeError("百度结果页解析为空（疑似验证页/改版，len=%d）" % len(text))
        return self._build_html_results(parsed, max_results)


class DuckDuckGoProvider(SearchProvider):
    """DDG 网页版兜底（免 Key）；命中反爬（202/异常页）返回空结果。"""
    name = "ddg"

    def search(self, query: str, max_results: int) -> List[SearchResult]:
        errors = []
        for endpoint in ("https://html.duckduckgo.com/html/",
                         "https://lite.duckduckgo.com/lite/"):
            try:
                url = endpoint + "?" + urllib.parse.urlencode({"q": query, "kl": "cn-zh"})
                raw = _request(url, self.timeout, headers={
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                    "Referer": "https://duckduckgo.com/"})
                text = raw.decode("utf-8", errors="replace")
                items = _parse_ddg(text, max_results)
                if items:
                    return items
                errors.append("%s 无结果/反爬" % endpoint.split("//")[1].split("/")[0])
            except Exception as e:
                errors.append("%s: %r" % (endpoint.split("//")[1].split("/")[0], e))
        raise RuntimeError("; ".join(errors[:3]) or "DuckDuckGo 无结果")


_DDG_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_DDG_SNIPPET_RE = re.compile(
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.S | re.I)
_DDG_LITE_LINK_RE = re.compile(
    r'<a[^>]+class="result-link"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_DDG_LITE_SNIPPET_RE = re.compile(
    r'<td[^>]+class="result-snippet"[^>]*>(.*?)</td>', re.S | re.I)


def _parse_ddg(text: str, max_results: int = 10) -> List[SearchResult]:
    out: List[SearchResult] = []
    try:
        links = _DDG_RESULT_RE.findall(text)
        snippets = _DDG_SNIPPET_RE.findall(text)
        if not links:
            links = _DDG_LITE_LINK_RE.findall(text)
            snippets = _DDG_LITE_SNIPPET_RE.findall(text)
        for i, (href, title) in enumerate(links):
            snippet = snippets[i] if i < len(snippets) else ""
            out.append(SearchResult(
                title=_clean_text(title), url=_ddg_decode(html_lib.unescape(href)),
                snippet=_clean_text(snippet), provider="ddg"))
            if len(out) >= max_results:
                break
    except Exception:
        return out
    return out


_PROVIDER_CLASSES: Dict[str, type] = {
    "serper": SerperProvider,
    "bing": BingApiProvider,
    "brave": BraveProvider,
    "tavily": TavilyProvider,
    "searxng": SearxngProvider,
    "so_html": SoHtmlProvider,
    "bing_html": BingHtmlProvider,
    "baidu_html": BaiduHtmlProvider,
    "ddg": DuckDuckGoProvider,
}


def available_providers() -> List[str]:
    """返回支持的 provider 名称（按推荐顺序）。"""
    return [p for p in _PROVIDER_ORDER if p in _PROVIDER_CLASSES or p == "auto"]


def _make_provider(name: str, api_key: str, base_url: str, timeout: float) -> Optional[SearchProvider]:
    cls = _PROVIDER_CLASSES.get(str(name or "").strip().lower())
    if cls is None:
        return None
    return cls(api_key=api_key, base_url=base_url, timeout=timeout)


def _provider_chain(provider: str, api_key: str) -> List[str]:
    """单次搜索的 provider 尝试顺序：配置优先，失败降级到免 Key 链。"""
    first = str(provider or "auto").strip().lower()
    if first in ("", "auto"):
        return list(_KEYLESS_CHAIN)
    order = [first]
    # 需要 Key 但没配 Key → 直接走免 Key 链
    cls = _PROVIDER_CLASSES.get(first)
    if cls is not None and getattr(cls, "requires_key", False) and not (api_key or "").strip():
        order = []
    order += [p for p in _KEYLESS_CHAIN if p != first]
    return order


def search(query: str, provider: str = "auto", api_key: str = "", base_url: str = "",
           max_results: int = 5, timeout: float = 8.0) -> Tuple[List[SearchResult], str, List[str]]:
    """执行一次（单查询）搜索，失败自动按链路降级。返回 (结果, 使用的 provider, 错误列表)。"""
    q = str(query or "").strip()
    errors: List[str] = []
    if len(q) < 2:
        return [], "", ["搜索词过短"]
    n = max(1, min(int(max_results or 5), 20))
    order = _provider_chain(provider, api_key)
    # 熔断中的 provider 排到最后（仍保留一次机会，避免全挂时彻底不可用）
    order = [x for x in order if not _breaker_open(x)] + [x for x in order if _breaker_open(x)]
    for name in order:
        if name not in _PROVIDER_CLASSES:
            errors.append("未知 provider: %r" % name)
            continue
        prov = _make_provider(name, api_key, base_url, timeout)
        if prov is None:
            continue
        ok, why = prov.available()
        if not ok:
            errors.append("%s 不可用: %s" % (name, why))
            continue
        try:
            results = _dedupe(prov.search(q, max_results=n), max_results=n)
        except Exception as e:
            _breaker_fail(name)
            errors.append("%s 失败: %r" % (name, e))
            continue
        if results:
            _breaker_ok(name)
            return results, name, errors
        _breaker_fail(name)
        errors.append("%s 无结果" % name)
    return [], "", errors


def search_multi(queries: List[str], provider: str = "auto", api_key: str = "",
                 base_url: str = "", max_results: int = 6, timeout: float = 8.0,
                 per_query: int = 6) -> Tuple[List[SearchResult], str, List[str]]:
    """多查询并行搜索并合并去重（推荐入口）。返回 (结果, 使用过的 provider, 错误列表)。"""
    qs: List[str] = []
    for q in queries or []:
        q = str(q or "").strip()
        if len(q) >= 2 and q not in qs:
            qs.append(q[:60])
        if len(qs) >= MAX_QUERIES:
            break
    if not qs:
        return [], "", ["无有效搜索词"]
    merged: List[SearchResult] = []
    used: List[str] = []
    errors: List[str] = []
    n = max(1, min(int(max_results or 6), 20))
    each = max(2, min(int(per_query or 6), n))
    try:
        with ThreadPoolExecutor(max_workers=min(len(qs), 3)) as pool:
            futures = {pool.submit(search, q, provider, api_key, base_url, each, timeout): q
                       for q in qs}
            for fut in as_completed(futures, timeout=timeout + 8):
                try:
                    results, used_provider, errs = fut.result()
                except Exception as exc:
                    errors.append(repr(exc))
                    continue
                if used_provider and used_provider not in used:
                    used.append(used_provider)
                errors.extend(errs)
                merged.extend(results)
    except Exception as exc:
        errors.append("多查询搜索异常: %r" % (exc,))
    # 合并后再按真实 URL / 标题去重
    deduped = _dedupe(merged, max_results=n)
    for r in deduped:
        if r.real_url:
            r.url = r.real_url
    if not deduped and not used:
        for q in qs:
            try:
                results, name, errs = search(q, provider, api_key, base_url, n, timeout)
            except Exception as exc:
                errors.append(repr(exc))
                continue
            if results:
                for r in results:
                    if r.real_url:
                        r.url = r.real_url
                return results, name, errors + errs
            errors.extend(errs)
    return deduped, ",".join(used), errors


# --------------------------------------------------------------------------- #
# 自测入口（python -m src.web.search "查询" [provider]）
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    qs = [sys.argv[1]] if len(sys.argv) > 1 else ["DeepSeek"]
    prov = sys.argv[2] if len(sys.argv) > 2 else "auto"
    t0 = time.time()
    res, used, errs = search_multi(qs, provider=prov, max_results=8)
    print("queries=%r provider=%s used=%s elapsed=%.2fs" % (qs, prov, used, time.time() - t0))
    for i, r in enumerate(res, 1):
        print("[%d] %s\n    url=%s\n    real=%s\n    %s" % (
            i, r.title, r.url, r.real_url, r.snippet[:120]))
    if errs:
        print("degrade:", errs[:8])
