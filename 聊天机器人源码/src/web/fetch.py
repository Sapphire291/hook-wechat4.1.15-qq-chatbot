# -*- coding: utf-8 -*-
"""网页抓取（web_fetch）：只取正文 + SSRF 防护。

安全线（与 DSH 的 web_fetch 工具一致）：
- 只允许 http / https；域名必须解析到公网地址，拒绝内网/本机/链路本地/元数据地址；
- 每一跳重定向都重新校验（最多 3 跳）；拒绝带账号密码的 URL、非 80/443 端口；
- 单次超时默认 8s，最多读取 2MB（超过即截断）；
- HTML → 正文只保留可见文本（去脚本/样式/导航/页脚/表单），单页截断由调用方决定；
- 本模块返回的一切内容都是「外部数据」，调用方不得把它当指令执行。

只依赖标准库（urllib / html.parser / ipaddress），不引入第三方解析库。
"""
from __future__ import annotations

import gzip
import http.cookiejar
import ipaddress
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import List, Optional, Tuple

#: 默认 UA（与常见桌面浏览器一致，避免被简单反爬拦截）
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

#: 单页最大读取字节数（2MB）
MAX_BYTES = 2 * 1024 * 1024
#: 最大重定向跳数
MAX_REDIRECTS = 4
#: 允许的端口（显式写端口时）：只有 http 80 / https 443
_ALLOWED_PORTS = (80, 443)
#: 允许的 Content-Type
_ALLOWED_CONTENT_TYPES = (
    "text/html", "application/xhtml+xml", "text/plain", "text/xml", "application/xml",
)
#: 内网/保留地址显式补充（ipaddress.is_private 已覆盖大部分，这里补充跨实现差异）
_EXTRA_BLOCKED_NETS = (
    ipaddress.ip_network("100.64.0.0/10"),      # CGNAT
    ipaddress.ip_network("169.254.0.0/16"),     # 链路本地 / 云元数据
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("fc00::/7"),           # IPv6 ULA
    ipaddress.ip_network("fe80::/10"),          # IPv6 链路本地
    ipaddress.ip_network("::/128"),
)
#: 明确禁止的主机名 / 后缀
_BLOCKED_HOSTS = {
    "localhost", "ip6-localhost", "ip6-loopback", "metadata", "metadata.google.internal",
    "instance-data", "metadata.tencentyun.com", "metadata.azure.internal",
}
_BLOCKED_SUFFIXES = (
    ".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet", ".localdomain",
)


@dataclass
class FetchResult:
    """一次抓取结果（text 为提取后的正文，已去 HTML 标签）。"""
    url: str
    final_url: str = ""
    status: int = 0
    content_type: str = ""
    title: str = ""
    text: str = ""
    bytes: int = 0
    elapsed: float = 0.0
    truncated: bool = False
    error: str = ""
    cached: bool = False

    def ok(self) -> bool:
        return not self.error and bool(self.text.strip())

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "status": self.status,
            "content_type": self.content_type,
            "title": self.title,
            "chars": len(self.text or ""),
            "bytes": self.bytes,
            "elapsed": round(self.elapsed, 3),
            "truncated": self.truncated,
            "error": self.error,
            "cached": self.cached,
        }


# --------------------------------------------------------------------------- #
# SSRF 防护
# --------------------------------------------------------------------------- #
def _ip_blocked(ip) -> bool:
    """判断一个解析出的 IP 是否属于必须拒绝的地址。"""
    try:
        if ip.version == 6 and getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            return True
        if getattr(ip, "is_site_local", False):
            return True
        for net in _EXTRA_BLOCKED_NETS:
            if ip.version == net.version and ip in net:
                return True
    except Exception:
        # 无法判断时按不安全处理
        return True
    return False


def _resolve_ips(host: str, port: int) -> Tuple[List[str], str]:
    """解析域名得到 IP 列表；失败返回 ([], 原因)。"""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except Exception as e:
        return [], "DNS 解析失败: %r" % (e,)
    ips: List[str] = []
    for info in infos or []:
        try:
            ips.append(str(info[4][0]))
        except Exception:
            continue
    # 去重保序
    seen = set()
    uniq = []
    for ip in ips:
        if ip not in seen:
            seen.add(ip)
            uniq.append(ip)
    if not uniq:
        return [], "DNS 无解析结果"
    return uniq, ""


def is_safe_public_url(url: str) -> Tuple[bool, str]:
    """校验 URL 是否为允许抓取的公网 http/https 地址。返回 (是否安全, 原因)。"""
    try:
        raw = str(url or "").strip()
        if not raw:
            return False, "空 URL"
        if len(raw) > 2048:
            return False, "URL 过长"
        parts = urllib.parse.urlsplit(raw)
        scheme = (parts.scheme or "").lower()
        if scheme not in ("http", "https"):
            return False, "只允许 http/https（实际 %r）" % (scheme or "空",)
        if parts.username or parts.password:
            return False, "不允许带账号密码的 URL"
        host = (parts.hostname or "").strip().lower().rstrip(".")
        if not host:
            return False, "缺少主机名"
        try:
            port = int(parts.port or (443 if scheme == "https" else 80))
        except Exception:
            return False, "端口非法"
        if parts.port is not None and port not in _ALLOWED_PORTS:
            return False, "只允许 80/443 端口（实际 %d）" % port
        if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
            return False, "禁止访问本机/内网主机名 %r" % host
        # IP 字面量直接校验；域名先解析再逐 IP 校验
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None:
            if _ip_blocked(ip):
                return False, "禁止访问内网/保留地址 %s" % ip
            return True, ""
        ips, err = _resolve_ips(host, port)
        if err:
            return False, err
        for item in ips:
            try:
                ip_obj = ipaddress.ip_address(item)
            except ValueError:
                return False, "解析结果非法: %r" % (item,)
            if _ip_blocked(ip_obj):
                return False, "禁止访问内网/保留地址 %s（%s）" % (ip_obj, host)
    except Exception as e:
        return False, "URL 校验异常: %r" % (e,)
    return True, ""


# --------------------------------------------------------------------------- #
# HTML → 正文
# --------------------------------------------------------------------------- #
_SKIP_TAGS = {
    "script", "style", "noscript", "template", "iframe", "svg", "canvas", "head",
    "nav", "footer", "aside", "form", "button", "select", "option", "textarea",
    "figure", "figcaption", "video", "audio", "source", "track", "object", "embed",
}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "dl", "dt", "dd", "tr", "table", "thead",
    "tbody", "tfoot", "section", "article", "main", "header", "blockquote", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "center", "address", "title",
}


_ARTICLE_TAGS = {"article", "main"}
#: 常见“正文容器”的 class/id 关键字（含中文站常见的 txt/zoom/zw 等写法）
_ARTICLE_ATTR_RE = re.compile(r"(?i)(article|content|main|detail|post|texte?|txt|zoom|zw|news|entry|body)")
#: 明显是导航/推荐/评论等噪声容器的关键字
_NOISE_ATTR_RE = re.compile(
    r"(?i)(nav|menu|footer|header|side|comment|related|recommend|share|breadcrumb|"
    r"pagination|toolbar|search|banner|advert|login|list|channel|column)")
_ARTICLE_MIN_CHARS = 300


class _HtmlTextExtractor(HTMLParser):
    """把 HTML 转成纯文本：跳过脚本/样式/导航，块级标签换行，并尽量只取正文容器。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._out: List[str] = []
        self._skip_depth = 0
        self._title_parts: List[str] = []
        self._in_title = False
        self._title_done = False
        self._stack: List[Tuple[str, int]] = []   # (tag, 在 _out 里的起点)
        self._candidates: List[Tuple[int, str]] = []   # (嵌套深度, 文本)

    # -- 正文容器识别 --
    @staticmethod
    def _is_container(tag: str, attrs) -> bool:
        t = (tag or "").lower()
        if t in _ARTICLE_TAGS:
            return True
        if t not in ("div", "section", "td", "table", "article", "main"):
            return False
        try:
            blob = " ".join(str(v or "") for _k, v in (attrs or []))
        except Exception:
            blob = ""
        if _NOISE_ATTR_RE.search(blob):
            return False
        return bool(_ARTICLE_ATTR_RE.search(blob))

    # -- HTMLParser 回调 --
    def handle_starttag(self, tag, attrs):
        t = (tag or "").lower()
        if t == "title" and not self._title_done:
            self._in_title = True
            return
        if t in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth == 0 and self._is_container(t, attrs):
            self._stack.append((t, len(self._out)))
        if t in _BLOCK_TAGS and self._skip_depth == 0:
            self._out.append("\n")

    def handle_startendtag(self, tag, attrs):
        t = (tag or "").lower()
        if t in ("br", "hr") and self._skip_depth == 0:
            self._out.append("\n")

    def handle_endtag(self, tag):
        t = (tag or "").lower()
        if t == "title" and self._in_title:
            self._in_title = False
            self._title_done = True
            return
        if t in _SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth == 0 and self._stack:
            for i in range(len(self._stack) - 1, -1, -1):
                if self._stack[i][0] == t:
                    _tag, start = self._stack[i]
                    depth = len(self._stack)
                    for _j in range(len(self._stack) - 1, i - 1, -1):
                        self._stack.pop()
                    try:
                        cand = _normalize_text("".join(self._out[start:]))
                        if len(cand) >= _ARTICLE_MIN_CHARS:
                            self._candidates.append((depth, cand))
                    except Exception:
                        pass
                    break
        if t in _BLOCK_TAGS and self._skip_depth == 0:
            self._out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)
            return
        if self._skip_depth == 0 and data:
            self._out.append(data)

    # -- 结果 --
    def title(self) -> str:
        return _clean_inline(" ".join(self._title_parts))

    def text(self) -> str:
        return _normalize_text("".join(self._out))

    def best_text(self) -> str:
        """优先返回正文容器文本；容器不明显时回退整页文本。"""
        full = self.text()
        if not self._candidates:
            return full
        total = max(1, len(full))
        # 候选：≥300 字、且不超过整页 80%（排除“把整页包起来”的外层容器）
        upper = max(_ARTICLE_MIN_CHARS, int(total * 0.8))
        subset = [(d, c) for d, c in self._candidates if len(c) <= upper]
        if not subset:
            subset = list(self._candidates)
        # 下限：至少占整页 20%，避免选到某个小按钮/小卡片
        lower = max(_ARTICLE_MIN_CHARS, int(total * 0.2))
        good = [(d, c) for d, c in subset if len(c) >= lower]
        pool = good or subset
        # 取嵌套最深的（最内层正文容器）；同深度取最长
        pool.sort(key=lambda x: (x[0], len(x[1])), reverse=True)
        best = pool[0][1]
        return best if len(best) >= _ARTICLE_MIN_CHARS else full


def _clean_inline(text: str) -> str:
    """压缩行内空白（不保留换行）。"""
    try:
        s = re.sub(r"[\s\u00a0\u3000]+", " ", str(text or ""))
        return s.strip()
    except Exception:
        return str(text or "").strip()


def _normalize_text(text: str) -> str:
    """按行清理：去行首尾空白、压缩行内空白、压缩连续空行。"""
    try:
        s = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        s = s.replace("\u00a0", " ").replace("\u3000", " ")
        lines = []
        for line in s.split("\n"):
            line = re.sub(r"[ \t\f\v]+", " ", line).strip()
            if line:
                lines.append(line)
        out = "\n".join(lines)
        # 连续空行压缩为空行（上面已去掉全部空行，这里保留段落所需的单换行）
        return out
    except Exception:
        return str(text or "")


def extract_text(html: str) -> Tuple[str, str]:
    """HTML → (正文, 标题)。解析失败返回 (空串, 空串)。"""
    try:
        parser = _HtmlTextExtractor()
        parser.feed(str(html or ""))
        parser.close()
        return parser.best_text(), parser.title()
    except Exception:
        # 解析失败时退化为去标签
        try:
            fallback = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", str(html or ""))
            fallback = re.sub(r"(?s)<[^>]+>", "\n", fallback)
            return _normalize_text(fallback), ""
        except Exception:
            return "", ""


def _decode_body(raw: bytes, header_charset: str = "") -> str:
    """按 header/meta 声明与常见中文编码依次尝试解码。"""
    if not raw:
        return ""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    candidates: List[str] = []
    if header_charset:
        candidates.append(header_charset)
    try:
        m = re.search(rb"""(?i)charset\s*=\s*["']?([\w\-]+)""", raw[:4096])
        if m:
            candidates.append(m.group(1).decode("ascii", "ignore"))
    except Exception:
        pass
    candidates += ["utf-8", "gb18030", "big5"]
    seen = set()
    for enc in candidates:
        enc = (enc or "").strip().lower()
        if not enc or enc in seen:
            continue
        seen.add(enc)
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def _decompress(raw: bytes, encoding: str) -> bytes:
    """处理 gzip/deflate 压缩响应。"""
    try:
        enc = (encoding or "").lower()
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except Exception:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        return raw
    return raw


# --------------------------------------------------------------------------- #
# 抓取
# --------------------------------------------------------------------------- #
class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁用 urllib 自动重定向：手动逐跳校验，防止重定向到内网。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _make_opener() -> urllib.request.OpenerDirector:
    """带 Cookie 的 opener：部分政府/新闻站首次访问会给 cookie 再跳转（否则重定向死循环）。"""
    try:
        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar), _NoRedirectHandler())
    except Exception:
        return urllib.request.build_opener(_NoRedirectHandler())


def _content_type_allowed(ctype: str) -> bool:
    c = (ctype or "").split(";", 1)[0].strip().lower()
    if not c:
        # 未声明类型时按 HTML 尝试（部分站点不返回 Content-Type）
        return True
    return any(c == allow or c.startswith(allow + "+") for allow in _ALLOWED_CONTENT_TYPES)


def _follow(url: str, timeout: float, max_redirects: int = MAX_REDIRECTS,
            headers: Optional[dict] = None) -> Tuple[str, int, str, object]:
    """安全地跟随重定向；返回 (final_url, status, error, resp_or_None)。

    只做校验与跳转，不读取正文（给 resolve_url 用；fetch 复用同一套规则）。
    同一 URL 重定向（靠 cookie 解）允许重试一次，避免 ccdi 这类站点误报死循环。
    """
    opener = _make_opener()
    current = str(url or "").strip()
    safe, reason = is_safe_public_url(current)
    if not safe:
        return "", 0, "URL 被拒绝: " + reason, None
    hops = 0
    same_retry = False
    while True:
        h = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "identity",
        }
        if headers:
            h.update(headers)
        req = urllib.request.Request(current, method="GET", headers=h)
        try:
            resp = opener.open(req, timeout=float(timeout))
            return (resp.geturl() or current,
                    int(getattr(resp, "status", 0) or (resp.getcode() or 0)), "", resp)
        except urllib.error.HTTPError as e:
            location = ""
            try:
                location = e.headers.get("Location") or ""
            except Exception:
                location = ""
            code = int(getattr(e, "code", 0) or 0)
            if 300 <= code < 400 and location:
                nxt = urllib.parse.urljoin(current, location)
                if nxt == current:
                    if same_retry:
                        return "", code, "重定向死循环（同地址）", None
                    same_retry = True
                    continue  # 带上第一次响应写入的 cookie 再试
                hops += 1
                if hops > max_redirects:
                    return "", code, "重定向次数过多（>%d）" % max_redirects, None
                ok, why = is_safe_public_url(nxt)
                if not ok:
                    return "", code, "重定向被拒绝: " + why, None
                current = nxt
                same_retry = False
                continue
            return current, code, "HTTP %s" % code, None
        except Exception as e:
            return current, 0, "网络错误: %r" % (e,), None


def resolve_url(url: str, timeout: float = 5.0, max_redirects: int = MAX_REDIRECTS,
                headers: Optional[dict] = None) -> str:
    """把跳转链接（如百度 /link?url=…）解析成真实 URL；失败返回空串。"""
    final, _status, err, resp = _follow(url, timeout, max_redirects, headers)
    try:
        if resp is not None:
            resp.close()
    except Exception:
        pass
    if err or not final:
        return ""
    safe, _ = is_safe_public_url(final)
    return final if safe else ""


def fetch(url: str, timeout: float = 8.0, max_bytes: int = MAX_BYTES,
          max_redirects: int = MAX_REDIRECTS) -> FetchResult:
    """抓取一个公网页面并提取正文；任何异常都收敛到 FetchResult.error。"""
    started = time.time()
    result = FetchResult(url=str(url or ""))
    try:
        current = str(url or "").strip()
        safe, reason = is_safe_public_url(current)
        if not safe:
            result.error = "URL 被拒绝: " + reason
            return result
        opener = _make_opener()
        hops = 0
        same_retry = False
        while True:
            parts = urllib.parse.urlsplit(current)
            req = urllib.request.Request(
                current, method="GET",
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept-Encoding": "identity",
                })
            try:
                resp = opener.open(req, timeout=float(timeout))
            except urllib.error.HTTPError as e:
                location = ""
                try:
                    location = e.headers.get("Location") or ""
                except Exception:
                    location = ""
                if 300 <= int(getattr(e, "code", 0) or 0) < 400 and location:
                    nxt = urllib.parse.urljoin(current, location)
                    if nxt == current:
                        if same_retry:
                            result.error = "重定向死循环（同地址）"
                            return result
                        same_retry = True
                        continue
                    hops += 1
                    if hops > max_redirects:
                        result.error = "重定向次数过多（>%d）" % max_redirects
                        return result
                    safe, reason = is_safe_public_url(nxt)
                    if not safe:
                        result.error = "重定向被拒绝: " + reason
                        return result
                    current = nxt
                    same_retry = False
                    continue
                result.status = int(getattr(e, "code", 0) or 0)
                result.error = "HTTP %s" % (result.status,)
                return result
            with resp:
                result.status = int(getattr(resp, "status", 0) or (resp.getcode() or 0))
                final_url = resp.geturl() or current
                result.final_url = final_url
                ctype = ""
                length_hint = 0
                try:
                    ctype = resp.headers.get("Content-Type") or ""
                    length_hint = int(resp.headers.get("Content-Length") or 0)
                except Exception:
                    pass
                result.content_type = ctype
                if not _content_type_allowed(ctype):
                    result.error = "不支持的内容类型: %s" % (ctype.split(";")[0],)
                    return result
                if length_hint and length_hint > max_bytes:
                    result.truncated = True
                chunks: List[bytes] = []
                total = 0
                while total < max_bytes:
                    try:
                        chunk = resp.read(min(64 * 1024, max_bytes - total))
                    except Exception as e:
                        result.error = "读取失败: %r" % (e,)
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                raw = b"".join(chunks)
                if total >= max_bytes:
                    result.truncated = True
                try:
                    encoding = resp.headers.get("Content-Encoding") or ""
                except Exception:
                    encoding = ""
                raw = _decompress(raw, encoding)
                text_raw = _decode_body(raw, _header_charset(ctype))
                result.bytes = len(raw)
                ctype_main = (ctype or "").split(";", 1)[0].strip().lower()
                if ctype_main in ("text/plain",):
                    result.text = _normalize_text(text_raw)
                    result.title = ""
                else:
                    result.text, result.title = extract_text(text_raw)
                if not result.text.strip() and text_raw.strip() and ctype_main not in ("text/html", "application/xhtml+xml"):
                    result.text = _normalize_text(text_raw)
                break
    except urllib.error.URLError as e:
        result.error = "网络错误: %r" % (getattr(e, "reason", e),)
    except socket.timeout:
        result.error = "超时（%.0fs）" % float(timeout)
    except TimeoutError:
        result.error = "超时（%.0fs）" % float(timeout)
    except Exception as e:
        result.error = "抓取异常: %r" % (e,)
    finally:
        result.elapsed = time.time() - started
    if result.error:
        result.text = ""
    return result


def _header_charset(ctype: str) -> str:
    try:
        for part in str(ctype or "").split(";")[1:]:
            part = part.strip()
            if part.lower().startswith("charset="):
                return part.split("=", 1)[1].strip("\"' ")
    except Exception:
        pass
    return ""


# --------------------------------------------------------------------------- #
# 自测入口（python -m src.web.fetch <url>）
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    r = fetch(target)
    print(r.to_dict())
    print("-" * 60)
    print((r.text or "")[:800])
