# -*- coding: utf-8 -*-
"""Built-in native DSH-compatible core.

This is a pure-Python, in-exe implementation of the DSH behaviors the bot needs:
session/memory ownership, model calls, built-in web_search/web_fetch, and a
single AI entry point for every task (chat, sticker decisions, summaries,
image descriptions). It does not read or launch the external DSH runtime.
"""
from __future__ import annotations

import atexit
import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.request
from typing import Dict, List, Optional

from ..core.models import Platform
from ..core.paths import data_home
from .base import HarnessReply, LocalHarness

_SEARCH_ENDPOINT = "https://api.deepseek.com/anthropic/v1/messages"
_SEARCH_MODEL = "deepseek-v4-flash"
_UA = "Mozilla/5.0 (compatible; ChatBotNativeDSH/1.0)"


def _platform_of(value) -> Platform:
    if isinstance(value, Platform):
        return value
    try:
        return Platform(str(value or "wechat"))
    except Exception:
        return Platform.WECHAT


class NativeHarness(LocalHarness):
    """In-exe DSH-compatible harness; no external DSH dependency."""

    def __init__(self, config):
        super().__init__(config)
        self._bridge_lock = threading.RLock()
        try:
            from . import ai_bridge
            ai_bridge.set_core(self)
            atexit.register(ai_bridge.clear_core)
        except Exception:
            pass
        try:
            from .agent_team import AgentTeam
            self.team = AgentTeam(self)
            try:
                from . import ai_bridge
                ai_bridge.set_team(self.team)
            except Exception:
                pass
        except Exception:
            self.team = None
        try:
            from ..memory.memory_agent import MemoryAgent
            self.memory_agent = MemoryAgent(self)
        except Exception:
            self.memory_agent = None

    # ---------------- availability / config ----------------
    def available(self) -> bool:
        return bool(getattr(self.config, "harness_enabled", True)) and bool(self._key(Platform.WECHAT))

    def warmup(self) -> None:
        # Native core has no child process to start; nothing to warm.
        return None

    def _ai(self, platform: Platform):
        return self.config.ai_for(platform)

    def _key(self, platform: Platform) -> str:
        try:
            ai = self._ai(platform)
            key = str(getattr(ai, "api_key", "") or "").strip()
            if key and not key.startswith("sk-***"):
                return key
        except Exception:
            pass
        return str(os.environ.get("DEEPSEEK_API_KEY", "") or "").strip()

    def _base_model(self, platform: Platform):
        ai = self._ai(platform)
        base = str(getattr(ai, "base_url", "") or "https://api.deepseek.com").rstrip("/")
        model = str(getattr(ai, "model", "") or "deepseek-flash").strip()
        return base, model

    # ---------------- low-level model calls ----------------
    def _post_json(self, url: str, key: str, body: dict, timeout: float = 60.0,
                   headers: Optional[dict] = None) -> dict:
        h = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def _chat_once(self, platform: Platform, messages: List[dict], system_prompt: str = "",
                   max_tokens: int = 600, tools: Optional[list] = None,
                   thinking_mode: str = "disabled",
                   reasoning_effort: Optional[str] = None) -> dict:
        key = self._key(platform)
        if not key:
            raise RuntimeError("no DeepSeek API key configured")
        base, model = self._base_model(platform)
        msgs = list(messages)
        if system_prompt:
            msgs = [{"role": "system", "content": system_prompt}] + msgs
        body = {"model": model, "messages": msgs, "stream": False,
                "thinking": {"type": thinking_mode if thinking_mode in ("enabled", "disabled") else "disabled"}}
        if max_tokens:
            body["max_tokens"] = int(max_tokens)
        if reasoning_effort:
            body["reasoning_effort"] = str(reasoning_effort)
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        try:
            payload = self._post_json(base + "/chat/completions", key, body)
        except Exception as exc:
            code = getattr(exc, "code", 0)
            if code == 400 and (thinking_mode in ("enabled", "disabled") or reasoning_effort):
                # 兼容不支持 thinking/reasoning_effort 的模型：去掉后重试一次
                body.pop("thinking", None)
                body.pop("reasoning_effort", None)
                payload = self._post_json(base + "/chat/completions", key, body)
            else:
                raise
        try:
            from ..core import token_stats
            token_stats.record(platform=platform.value, model=model,
                               usage=payload.get("usage") or {}, kind="chat")
        except Exception:
            pass
        return payload

    @staticmethod
    def _message_text(payload: dict) -> str:
        choices = payload.get("choices") or []
        if not choices:
            return ""
        msg = (choices[0] or {}).get("message") or {}
        return str(msg.get("content") or msg.get("reasoning_content") or "").strip()

    # ---------------- built-in web ----------------
    def web_search(self, queries: List[str], platform: Platform = Platform.WECHAT,
                   max_results: int = 8) -> dict:
        key = self._key(platform)
        if not key:
            return {"ok": False, "error": "no api key"}
        sources = []
        for query in [str(q or "").strip() for q in (queries or []) if str(q or "").strip()][:4]:
            body = {
                "model": _SEARCH_MODEL,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": "Perform a web search for the query: " + query}],
                "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
                "thinking": {"type": "disabled"},
            }
            try:
                payload = self._post_json(_SEARCH_ENDPOINT, key, body, timeout=60.0,
                                          headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
            except Exception as exc:
                return {"ok": False, "error": "web_search failed: %r" % (exc,)}
            for block in (payload.get("content") or []):
                if not isinstance(block, dict) or block.get("type") != "web_search_tool_result":
                    continue
                for item in (block.get("content") or []):
                    if not isinstance(item, dict) or item.get("type") != "web_search_result":
                        continue
                    url = str(item.get("url") or "")
                    title = str(item.get("title") or url)
                    snippet = str(item.get("cited_text") or item.get("snippet") or "").strip()
                    if url:
                        sources.append({"query": query, "url": url, "title": title,
                                        "snippet": snippet[:300],
                                        "published": str(item.get("page_age") or "")})
        # dedupe by URL, cap
        seen = set()
        out = []
        for s in sources:
            if s["url"] in seen:
                continue
            seen.add(s["url"])
            out.append(s)
            if len(out) >= max(1, int(max_results or 8)):
                break
        if not out:
            return {"ok": False, "error": "no web results"}
        lines = []
        for i, s in enumerate(out, 1):
            lines.append("%d. [%s](%s)%s" % (
                i, s["title"], s["url"],
                ("\n   " + s["snippet"]) if s["snippet"] else ""))
        return {"ok": True, "sources": out, "text": "Sources:\n" + "\n".join(lines)}

    def web_fetch(self, url: str, platform: Platform = Platform.WECHAT, max_chars: int = 12000) -> dict:
        try:
            if not re.match(r"^https?://", str(url or ""), re.I):
                return {"ok": False, "error": "invalid url"}
            req = urllib.request.Request(str(url), headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read(2_000_000)
                ctype = str(resp.headers.get("Content-Type") or "")
            if "html" in ctype.lower():
                text = raw.decode("utf-8", errors="replace")
                text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
                text = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>", "\n", text)
                text = re.sub(r"(?is)<[^>]+>", " ", text)
                text = re.sub(r"&nbsp;", " ", text)
                text = re.sub(r"&amp;", "&", text)
                text = re.sub(r"&lt;", "<", text)
                text = re.sub(r"&gt;", ">", text)
            else:
                text = raw.decode("utf-8", errors="replace")
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            truncated = len(text) > max_chars
            return {"ok": True, "url": url, "text": text[:max_chars], "truncated": truncated}
        except Exception as exc:
            return {"ok": False, "error": "web_fetch failed: %r" % (exc,)}

    # ---------------- generic AI entry points ----------------
    def complete(self, system_prompt: str, user_message: str, platform=Platform.WECHAT,
                 max_tokens: int = 600, images: Optional[List[str]] = None,
                 thinking=None, reasoning_effort=None) -> str:
        p = _platform_of(platform)
        if images:
            return self.vision(system_prompt, images[0], p)
        try:
            payload = self._chat_once(p, [{"role": "user", "content": str(user_message or "")}],
                                      system_prompt=str(system_prompt or ""),
                                      max_tokens=max_tokens,
                                      thinking_mode=str(thinking or "disabled"),
                                      reasoning_effort=reasoning_effort)
            return self._message_text(payload)
        except Exception as exc:
            print("[native] complete failed: %r" % (exc,), flush=True)
            return ""

    def vision(self, system_prompt: str, image_path: str, platform=Platform.WECHAT,
               max_tokens: int = 500) -> str:
        p = _platform_of(platform)
        try:
            data = open(str(image_path), "rb").read()
            b64 = base64.b64encode(data).decode("ascii")
            ext = os.path.splitext(str(image_path))[1].lower()
            mime = "image/png" if ext == ".png" else "image/gif" if ext == ".gif" else "image/jpeg"
            payload = self._chat_once(p, [{"role": "user", "content": [
                {"type": "text", "text": "描述这张图片"},
                {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, b64)}},
            ]}], system_prompt=str(system_prompt or ""), max_tokens=max_tokens)
            return self._message_text(payload)
        except Exception as exc:
            print("[native] vision failed: %r" % (exc,), flush=True)
            return ""

    def generator_for(self, platform=Platform.WECHAT):
        return _NativeGenerator(self, _platform_of(platform))

    # ---------------- main chat with web tools ----------------
    def _self_prompt(self, platform: Platform) -> str:
        """底层提示词：memory/<平台>MM/SELF.md；不存在时退回 config.system_prompt。"""
        try:
            mem = self._mem(platform.value)
            path = os.path.join(mem.root, "SELF.md")
            if os.path.isfile(path):
                text = open(path, "r", encoding="utf-8").read().strip()
                if text:
                    return text
        except Exception:
            pass
        try:
            return str(getattr(self._ai(platform), "system_prompt", "") or "").strip()
        except Exception:
            return ""

    def _read_memory_file(self, path: str, file_root: str = "") -> dict:
        """Read a file under memory/<platform>MM...; used by the read_file tool."""
        try:
            from ..core.document_store import memory_platform_dir
            path = str(path or "").strip().strip('"')
            if not path:
                return {"ok": False, "error": "empty path"}
            candidates = []
            if os.path.isabs(path):
                candidates.append(path)
            else:
                if file_root:
                    candidates.append(os.path.join(file_root, path))
                candidates.append(os.path.join(data_home(), path))
                candidates.append(os.path.join(memory_platform_dir("wechat"), path))
                candidates.append(os.path.join(memory_platform_dir("qq"), path))
            # 只传文件名时，在记忆目录下按文件名找
            for base in (file_root, memory_platform_dir("wechat"), memory_platform_dir("qq")):
                if not base or not os.path.isdir(base):
                    continue
                for root, _dirs, files in os.walk(base):
                    if os.path.basename(path) in files:
                        candidates.append(os.path.join(root, os.path.basename(path)))
                        break
            mem_root = os.path.realpath(os.path.join(data_home(), "memory"))
            for cand in candidates:
                real = os.path.realpath(cand)
                if not (real == mem_root or real.startswith(mem_root + os.sep)):
                    continue
                if not os.path.isfile(real):
                    continue
                text = ""
                if getattr(self, "team", None) is not None:
                    text = self.team.extract_document_text(real)
                if not text:
                    try:
                        text = open(real, "r", encoding="utf-8", errors="replace").read()
                    except Exception:
                        text = ""
                if not text:
                    return {"ok": False, "error": "无法解析文件：" + os.path.basename(real)}
                return {"ok": True, "text": text[:30000],
                        "path": os.path.relpath(real, data_home())}
            return {"ok": False, "error": "记忆目录中没有找到该文件"}
        except Exception as exc:
            return {"ok": False, "error": "read_file failed: %r" % (exc,)}

    def _chat_with_tools(self, platform: Platform, content, system_prompt: str = "",
                         max_tokens: Optional[int] = 600,
                         thinking_mode: str = "disabled",
                         reasoning_effort: Optional[str] = None,
                         file_root: str = "") -> str:
        tools = [
            {"type": "function", "function": {
                "name": "web_search",
                "description": "Search the web for current information. Returns source URLs.",
                "parameters": {"type": "object", "properties": {
                    "queries": {"type": "array", "items": {"type": "string"}}},
                    "required": ["queries"]}}},
            {"type": "function", "function": {
                "name": "web_fetch",
                "description": "Fetch one HTTP(S) URL and return readable text.",
                "parameters": {"type": "object", "properties": {
                    "url": {"type": "string"}}, "required": ["url"]}}},
            {"type": "function", "function": {
                "name": "read_file",
                "description": "读取记忆目录下的文件内容（docx/pdf/txt/md/xlsx/pptx）。"
                               "用户可以让你查看已下载到 memory/<平台>MM/<会话>/documents 的文件。",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string",
                             "description": "文件路径或文件名；相对路径按当前会话文档目录解析"}},
                    "required": ["path"]}}},
        ]
        messages = [{"role": "user", "content": content}]
        for _ in range(4):
            payload = self._chat_once(platform, messages, system_prompt=system_prompt,
                                      max_tokens=max_tokens, tools=tools,
                                      thinking_mode=thinking_mode,
                                      reasoning_effort=reasoning_effort)
            choices = payload.get("choices") or []
            if not choices:
                return ""
            msg = (choices[0] or {}).get("message") or {}
            calls = msg.get("tool_calls") or []
            if not calls:
                return str(msg.get("content") or msg.get("reasoning_content") or "").strip()
            # append assistant tool-call message exactly, then tool results
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             "tool_calls": calls})
            for call in calls:
                fn = (call.get("function") or {})
                name = str(fn.get("name") or "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                if name == "web_search":
                    queries = args.get("queries")
                    if isinstance(queries, str):
                        queries = [queries]
                    r = self.web_search(list(queries or []), platform)
                elif name == "web_fetch":
                    r = self.web_fetch(str(args.get("url") or ""), platform)
                elif name == "read_file":
                    r = self._read_memory_file(str(args.get("path") or ""), file_root)
                else:
                    r = {"ok": False, "error": "unknown tool " + name}
                messages.append({"role": "tool", "tool_call_id": str(call.get("id") or ""),
                                 "content": r.get("text") or ("Error: " + str(r.get("error") or "failed"))})
        return ""

    # ---------------- HarnessKernel interface ----------------
    @staticmethod
    def _thinking_policy(thinking, reasoning_effort, max_tokens):
        """按用户要求返回 (thinking_type, effort, max_tokens, 句数提示)。"""
        th = str(thinking or "").lower()
        eff = str(reasoning_effort or "").lower()
        if th == "disabled":
            return "disabled", None, 200, "简单信息：不思考，1~2 句，每句≤15字。"
        if eff == "high" or (th == "enabled" and max_tokens is None):
            return "enabled", "high", None, "重要/具体：认真思考，按原分段规则。"
        return "enabled", "low", 1200, "感兴趣内容：轻思考，1~3 句，每句≤15字。"

    def reply(self, platform: str, chat_id: str, chat_name: str, user_text: str,
              system_prompt: str, style_instruction: str, history: Optional[List[dict]],
              thinking=None, reasoning_effort=None, max_tokens=None,
              images=None, generator=None) -> HarnessReply:
        p = _platform_of(platform)
        mem, key, name, _group = self._target(platform, chat_id)
        summary = ""
        try:
            summary = mem.summary(key)
        except Exception:
            summary = ""
        policy_thinking, policy_effort, policy_max, sentence_hint = self._thinking_policy(
            thinking, reasoning_effort, max_tokens)
        content = ""
        # 高速模式常开；思考强度按消息类型分级
        content += ("【回复】不思考/轻思考一句≤15；重要内容按原规则。\n"
                    "【思考】" + sentence_hint + "\n\n")
        # 原本每轮由 bot 拼进 system 的提示词，改为由 native 核以“上下文”注入
        context_prompt = str(system_prompt or "").strip()
        if not context_prompt:
            try:
                context_prompt = str(getattr(self._ai(p), "system_prompt", "") or "").strip()
            except Exception:
                context_prompt = ""
        if context_prompt:
            content += "【DSH 上下文注入（每轮提示词）】\n" + context_prompt + "\n\n"
        if summary:
            content += ("【长期记忆（是否使用由你判断；需要回忆时参考）】\n"
                        "【关于「%s」的记忆】\n%s\n\n" % (name, summary))
        # 最近对话权重最高：短句指代/“你都忘了/刚才”必须优先指向这里
        recent_lines = []
        try:
            for h in list(history or [])[-8:]:
                if not isinstance(h, dict):
                    continue
                role = str(h.get("role") or "user")
                txt = str(h.get("content") or "").strip()
                if not txt:
                    continue
                if role == "assistant":
                    recent_lines.append("我: " + txt[:800])
                else:
                    recent_lines.append("对方: " + txt[:800])
        except Exception:
            recent_lines = []
        if recent_lines:
            content += ("【最近对话（优先参考）】\n"
                        "同一话题的短句指代、“你都忘了/刚才说的”优先参考这里；"
                        "但如果你判断近期对话没有关联、或需要回忆更早的事，可以自行查看长期记忆/旧文档，由你决定。\n"
                        + "\n".join(recent_lines) + "\n\n")
        self_prompt = self._self_prompt(p)
        file_root = ""
        available_files = []
        try:
            from ..core.document_store import documents_dir
            file_root = documents_dir(p.value, str(chat_id))
            for root, _dirs, files in os.walk(file_root):
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        available_files.append((os.path.getmtime(fp), fp, os.path.getsize(fp)))
                    except Exception:
                        pass
            available_files = sorted(available_files, reverse=True)[:10]
        except Exception:
            available_files = []
        if available_files:
            content += ("【可用记忆文件（你可以调用 read_file 工具读取）】\n"
                        "用户提到文件/文档时，先用 read_file 读取对应文件再回答；不要凭记忆猜。\n")
            for _mt, fp, sz in available_files:
                content += "- %s（%d 字节）\n" % (fp, sz)
            content += "\n"
        content += "【对方消息】\n" + str(user_text or "")
        # 图片和文字一起交给同一个聊天主 AI（不是单独 vision 回复）
        payload = content
        if images:
            blocks = [{"type": "text", "text": content}]
            for path in images:
                try:
                    data = open(str(path), "rb").read()
                    b64 = base64.b64encode(data).decode("ascii")
                    ext = os.path.splitext(str(path))[1].lower()
                    mime = "image/png" if ext == ".png" else "image/gif" if ext == ".gif" else "image/jpeg"
                    blocks.append({"type": "image_url", "image_url": {
                        "url": "data:%s;base64,%s" % (mime, b64)}})
                except Exception:
                    pass
            payload = blocks
        text = self._chat_with_tools(p, payload, system_prompt=self_prompt,
                                     max_tokens=policy_max,
                                     thinking_mode=policy_thinking,
                                     reasoning_effort=policy_effort,
                                     file_root=file_root)
        try:
            with open(os.path.join(data_home(), "last_system_prompt.txt"), "w", encoding="utf-8") as f:
                f.write("【SELF.md（每轮单独 system 注入）】\n" + self_prompt +
                        "\n\n【DSH 上下文注入（原本每轮提示词）】\n" + context_prompt +
                        "\n\n【思考策略】thinking=%s effort=%s max_tokens=%s %s" % (
                            policy_thinking, policy_effort, policy_max, sentence_hint) +
                        "\n\n【记忆上下文】\n" + (summary or "（无）") +
                        "\n\n" + content)
        except Exception:
            pass
        return HarnessReply(text=text or "", system_prompt="", injected=bool(summary),
                            memory_key=key, meta={"backend": "native"})

    def browse_document(self, path: str, name: str = "", platform="wechat") -> str:
        """后台文档浏览：优先交给智能体团队，失败时返回错误说明。"""
        try:
            if getattr(self, "team", None) is not None:
                return self.team.browse_document(path, name=name, platform=platform)
        except Exception as exc:
            print("[native] browse_document failed: %r" % (exc,), flush=True)
        return "（文档浏览失败：%s）" % (name or path)

    def record_memory_event(self, action: str, target: str, detail=None) -> None:
        try:
            self._mem(Platform.WECHAT.value).record(action, target, detail or {})
        except Exception:
            pass

    def record_turn(self, platform, chat_id, chat_name, user_text, reply_text,
                    sender="", generator=None, image_note="", sticker_note="",
                    web_note="", max_chars=8000, compress_to=4000,
                    is_group: bool = False) -> None:
        p = _platform_of(platform)
        mem, key, name, _group = self._target(platform, chat_id)
        now = time.time()
        if not sender:
            sender = str(chat_name or name or "对方")
        try:
            mem.append(key, "user", user_text, sender=sender, ts=now)
            if image_note:
                mem.append(key, "user", "[图片] " + image_note, sender="图片", ts=now)
            if sticker_note:
                mem.append(key, "assistant", "[表情包] " + sticker_note, sender="我", ts=now)
            mem.append(key, "assistant", reply_text, sender="我", ts=now)
            mem.touch_turn()
            mem.record("reply.text", str(chat_id), {"chars": len(reply_text or ""), "chat": chat_name})
        except Exception as exc:
            print("[native] record_turn failed: %r" % (exc,), flush=True)
            return
        # ECHO summary update is a background subagent, never inline with the main chat AI.
        if getattr(self, "memory_agent", None) is not None:
            self.memory_agent.schedule(
                platform=p.value, chat_id=str(chat_id), chat_name=str(chat_name or ""),
                sender=str(sender or ""), user_text=str(user_text or ""),
                reply_text=str(reply_text or ""), image_note=str(image_note or ""),
                sticker_note=str(sticker_note or ""), is_group=bool(is_group))


class _NativeGenerator:
    """DeepSeekClient-compatible adapter so all legacy AI call sites use the native core."""

    def __init__(self, core: NativeHarness, platform: Platform):
        self.core = core
        self.platform = platform

    def generate(self, system_prompt: str = "", style_instruction: str = "", history=None,
                 user_message: str = "", images=None, thinking=None,
                 reasoning_effort=None, max_tokens=None) -> str:
        return self.core.complete(system_prompt, user_message,
                                  platform=self.platform,
                                  max_tokens=int(max_tokens or 600),
                                  images=images)
