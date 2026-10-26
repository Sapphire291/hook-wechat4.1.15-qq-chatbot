# -*- coding: utf-8 -*-
"""微信接入适配器（屏幕零接触版）。

原则：**绝不控制鼠标/键盘/窗口焦点**。

- 读消息：使用 wechatauto 解密本地 SQLCipher 数据库（纯本地读盘，不碰屏幕）；
- 发消息：只走「hook 协议」通道（WeChat-Hook 的 version.dll HTTP API，默认 30001）。
  若未安装 hook，则发送直接返回 False，**不会退回 UIA/OCR 去点屏幕**。

为什么发送需要 hook：
  微信 4.1.x 的发送必须经过客户端网络协议，纯本地数据库写入不会真正发出。
  非 GUI 的唯一可靠方式是给微信进程注入 hook 后由 HTTP 接口驱动。
  注意：aixed/WeChat-Hook 目标版本为 4.1.10.27，微信 4.1.15 需要降级后使用；
  未安装 hook 时本适配器只做「读消息」，不接管键鼠。
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import threading
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

from ..core.interfaces import ChatAdapter
from ..core.models import AppConfig, Message, MessageType, Platform
from ..core.paths import data_dir
from ..core.paths import data_dir

HOOK_API = "http://127.0.0.1:30001"


def _send_log(message: str) -> None:
    try:
        with open(os.path.join(data_dir(), "send_path.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except Exception:
        pass


def _account_scan_log(message: str) -> None:
    try:
        with open(os.path.join(data_dir(), "account_scan.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except Exception:
        pass

# 方案B1 直发（exp67 mode11）：当前会话作为供体，不切换目标。
try:
    from .direct_send import (direct_available, direct_send_image, direct_send_text,
                              last_detail as direct_last_detail)
except Exception:  # pragma: no cover
    def direct_available(*a, **k):
        return False

    def direct_send_text(*a, **k):
        return False

    def direct_send_image(*a, **k):
        return False

    def direct_last_detail(*a, **k):
        return ""

# 持久化的 WeChatGUI 实例：复用它可以命中 wechatauto 的「快速路径」
# （跳过重复的窗口探测/会话重检），把单次发送从 ~130s 降到几秒。
_GUI = None
_GUI_LOCK = threading.Lock()

# 微信「动画表情」消息在数据库里是加密的，取不到图片，用标记让 AI 知道这是表情包
STICKER_MARK = "[表情包]"
_STICKER_TYPES = ("动画表情", "表情", "emotion")


def _get_gui():
    """懒创建并复用 WeChatGUI（第一次会做窗口探测与布局校准）。"""
    global _GUI
    if _GUI is not None:
        return _GUI
    with _GUI_LOCK:
        if _GUI is None:
            from wechatauto.guia import WeChatGUI
            _GUI = WeChatGUI()
    return _GUI


def _screen_send_text(who: str, text: str) -> bool:
    """屏幕自动化发送文本（复用持久 GUI 实例，命中快速路径）。"""
    try:
        gui = _get_gui()
        resp = gui.send_msg(str(text), str(who), verify=False)
        status = getattr(resp, "status", None)
        return status is not False
    except Exception:
        return False


def _screen_send_file(who: str, path: str) -> bool:
    """屏幕自动化发送图片/文件（走剪贴板 CF_HDROP + 粘贴）。"""
    try:
        gui = _get_gui()
        resp = gui.send_file(str(path), str(who), verify=False)
        status = getattr(resp, "status", None)
        return status is not False
    except Exception:
        return False

_IMAGE_TYPES = ("图片", "视频", "动画表情")


# ---------------------------------------------------------------------- #
# hook HTTP 通道（非 GUI）
# ---------------------------------------------------------------------- #
def hook_available(timeout: float = 1.5) -> bool:
    """探测 WeChat-Hook 的 HTTP 服务是否就绪。"""
    try:
        req = urllib.request.Request(HOOK_API + "/QueryDB/status", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _hook_post(path: str, payload=None, timeout: float = 10.0):
    """POST 到 hook HTTP 并返回 JSON（失败抛异常）。"""
    req = urllib.request.Request(
        HOOK_API + path,
        data=json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def hook_send_text(wxid: str, text: str, timeout: float = 5.0) -> bool:
    """通过 hook 协议发送文本（不碰屏幕）。"""
    try:
        data = json.dumps({"wxidorgid": wxid, "msg": text}).encode("utf-8")
        req = urllib.request.Request(
            HOOK_API + "/SendTextMsg", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode("utf-8"))
            return bool(out and out.get("ret") == 0)
    except Exception:
        return False


def _screen_send(who: str, text: str) -> bool:
    """屏幕自动化发送（UIA/OCR）。会短暂把微信窗口提到前台并模拟键鼠。"""
    try:
        from wechatauto.guia import quick_send
        quick_send(text, who, verify=False)
        return True
    except Exception:
        return False

def hook_send_image(wxid: str, path: str, timeout: float = 10.0) -> bool:
    try:
        data = json.dumps({"wxidorgid": wxid, "path": path}).encode("utf-8")
        req = urllib.request.Request(
            HOOK_API + "/SendImgMsg", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode("utf-8"))
            return bool(out and out.get("ret") == 0)
    except Exception:
        return False


# ---------------------------------------------------------------------- #
# 适配器
# ---------------------------------------------------------------------- #
class WeChatAdapter(ChatAdapter):
    """屏幕零接触的微信适配器：数据库读取 + hook 协议发送。"""

    platform = Platform.WECHAT

    _poll_interval = 1.0
    _reconnect_interval = 5.0
    _hook_recheck = 30.0       # hook 可用性缓存时间（秒）

    def __init__(self, config: AppConfig):
        super().__init__(config)
        self._db = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._poll_lock = threading.Lock()
        self._lock = threading.Lock()
        self._since: Dict[str, int] = {}
        self._self_wxid = ""
        self._hook_ok = False
        self._hook_ts = 0.0
        self.last_image_msg = None
        self._media = None
        self._sticker_cache: Dict[str, str] = {}
        self._sticker_original: Dict[str, str] = {}

    # ------------------------------------------------------------------ #
    def _load_db(self):
        if self._db is not None:
            return self._db
        try:
            from wechatauto import WeChatDB
            self._db = WeChatDB()
            try:
                info = self._db.get_self_info() or {}
                self._self_wxid = str(info.get("username") or "")
            except Exception:
                pass
        except Exception:
            self._db = None
        return self._db

    def available(self) -> bool:
        try:
            db = self._load_db()
            if db is None:
                return False
            db.get_self_info()
            return True
        except Exception:
            return False

    def hook_ready(self) -> bool:
        """带缓存的 hook 可用性检查（受 wechat_hook_enabled 总开关控制）。"""
        if not bool(getattr(self.config, "wechat_hook_enabled", False)):
            return False
        now = time.time()
        if (now - self._hook_ts) < self._hook_recheck:
            return self._hook_ok
        self._hook_ok = hook_available()
        self._hook_ts = now
        return self._hook_ok

    def capability(self) -> str:
        """描述当前能力，供 UI 展示。"""
        if not self.available():
            return "未连接（微信未运行或数据库不可读）"
        if self.hook_ready():
            return "已连接：可读消息 + 可发送（hook 协议，不碰屏幕）"
        return "已连接：可读消息（只读）；发送需安装 hook（version.dll / 30001）"

    # ------------------------------------------------------------------ #
    def _ensure_donor(self) -> bool:
        """确保当前打开的会话是直发供体（默认文件传输助手），不切到目标。"""
        donor = str(getattr(self.config, "wechat_donor_chat", "") or "文件传输助手")
        try:
            req = urllib.request.Request(
                HOOK_API + "/GetChat", data=b"{}",
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=3) as resp:
                current = json.loads(resp.read().decode("utf-8")).get("chat", "")
        except Exception:
            current = ""
        if current and (current == donor or current in ("文件传输助手", "filehelper")):
            return True
        # 用一条空格消息把微信切到供体（只切供体，不碰目标）
        try:
            req = urllib.request.Request(
                HOOK_API + "/SendTextTo",
                data=json.dumps({"chat": donor, "text": " "}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            _send_log("ensure_donor switch donor=%r current=%r out=%r" % (donor, current, out.get("chat")))
            return out.get("ret") == 0
        except Exception as exc:
            _send_log("ensure_donor failed %r" % (exc,))
            return False

    def _hook_watchdog(self) -> None:
        """WeChat 崩溃/重启后自动重新注入 hook。"""
        while self._running:
            time.sleep(10)
            try:
                if not hook_available():
                    _send_log("watchdog: hook down, reinjecting")
                    from .hook_injector import ensure_hook
                    r = ensure_hook()
                    _send_log("watchdog: ensure_hook %r" % (r,))
            except Exception as exc:
                _send_log("watchdog error %r" % (exc,))

    @staticmethod
    def _login_status() -> Optional[bool]:
        try:
            req = urllib.request.Request(HOOK_API + "/QueryDB/status", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return bool(int(data.get("IsLogin") or 0))
        except Exception:
            return None

    def _scan_favorites_task(self) -> None:
        try:
            from .hook_injector import ensure_hook
            ensure_hook()
        except Exception:
            pass
        try:
            from .account_store import current_wxid, scan_favorites
            wxid = current_wxid()
            if not wxid:
                _account_scan_log("login scan: no wxid")
                return
            _account_scan_log("login scan start wxid=%s" % wxid)
            r = scan_favorites(wxid, log=_account_scan_log)
            _account_scan_log("login scan done wxid=%s result=%r" % (wxid, r))
        except Exception as exc:
            _account_scan_log("login scan failed %r" % (exc,))

    def _login_watchdog(self) -> None:
        """每次微信登录（0→1，或程序启动时已登录）扫描一次收藏表情。"""
        last = None
        while self._running:
            try:
                login = self._login_status()
                if login and last is not True:
                    _account_scan_log("wechat login detected, scan favorites")
                    threading.Thread(target=self._scan_favorites_task,
                                     name="wechat-login-favorites-scan", daemon=True).start()
                last = login
            except Exception as exc:
                _account_scan_log("login watchdog error %r" % (exc,))
            time.sleep(8)

    def start(self) -> bool:
        if not self.available():
            return False
        with self._lock:
            if self._running:
                return True
            self._running = True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name="wechat-adapter", daemon=True)
            self._thread.start()
            threading.Thread(target=self._hook_watchdog, name="wechat-hook-watchdog", daemon=True).start()
            threading.Thread(target=self._login_watchdog, name="wechat-login-watchdog", daemon=True).start()
        return True

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
        self._stop_event.set()

    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        # 连接微信时：自动注入 hook（不依赖 hook 发送开关）并扫描联系人缓存
        try:
            if bool(getattr(self.config, "wechat_auto_inject", True)):
                from .hook_injector import ensure_hook
                ensure_hook()
        except Exception:
            pass
        try:
            from .wechat_contacts import scan_wechat_contacts
            scan_wechat_contacts(persist=True)
        except Exception:
            pass
        try:
            self._ensure_donor()
        except Exception as exc:
            _send_log("ensure_donor at start failed %r" % (exc,))
        db = self._load_db()
        if db is None:
            return
        self._init_watermarks(db)
        while not self._stop_event.is_set():
            try:
                with self._poll_lock:
                    self._poll_once(db)
            except Exception:
                pass
            self._stop_event.wait(self._poll_interval)

    def poll_now(self) -> None:
        """立即补拉一次新消息（pipeline 在每次回复后调用，弥补发送期间的空档）。"""
        try:
            with self._poll_lock:
                db = self._load_db()
                if db is not None:
                    self._poll_once(db)
        except Exception:
            pass

    def _init_watermarks(self, db) -> None:
        try:
            for s in (db.get_sessions(limit=200) or []):
                username = s.get("username")
                if not username:
                    continue
                try:
                    msgs = db.get_messages(username, limit=1)
                except Exception:
                    msgs = []
                if msgs:
                    try:
                        self._since[username] = int(msgs[0].get("sort_seq") or 0)
                    except Exception:
                        pass
        except Exception:
            pass

    def _poll_once(self, db) -> None:
        for s in (db.get_sessions(limit=200) or []):
            username = s.get("username")
            if not username:
                continue
            since = self._since.get(username, 0)
            try:
                msgs = db.get_new_messages(username, since)
            except Exception:
                continue
            if not msgs:
                continue
            for m in msgs:
                try:
                    seq = int(m.get("sort_seq") or 0)
                except Exception:
                    seq = 0
                if seq > self._since.get(username, 0):
                    self._since[username] = seq
                msg = self._to_message(m, username)
                if msg is not None:
                    self._dispatch(msg)

    # ------------------------------------------------------------------ #
    # 表情包读取：emoticon.db md5 -> CDN 明图（不依赖屏幕/不切聊天）
    # ------------------------------------------------------------------ #
    def _make_gif_sheet(self, path: str, md5: str) -> str:
        """GIF/WebP 取 4 帧拼成一张 2x2 图，方便多模态模型理解动态表情。"""
        try:
            from PIL import Image
            im = Image.open(path)
            n = int(getattr(im, "n_frames", 1) or 1)
            if n > 1:
                idxs = [round(i * (n - 1) / 3) for i in range(4)]
            else:
                idxs = [0, 0, 0, 0]
            frames = []
            for idx in idxs:
                try:
                    im.seek(idx)
                except Exception:
                    pass
                fr = im.convert("RGB")
                fr.thumbnail((512, 512))
                frames.append(fr)
            w = max(f.width for f in frames)
            h = max(f.height for f in frames)
            sheet = Image.new("RGB", (w * 2, h * 2), (255, 255, 255))
            for i, fr in enumerate(frames[:4]):
                x = (i % 2) * w + (w - fr.width) // 2
                y = (i // 2) * h + (h - fr.height) // 2
                sheet.paste(fr, (x, y))
            out = os.path.join(data_dir(), "wechat_media", "emoji_%s_4f.png" % md5)
            sheet.save(out, "PNG")
            self._sticker_cache[md5] = out
            return out
        except Exception:
            return ""

    def _sticker_image(self, content: str) -> str:
        try:
            md5m = re.search(r'md5\s*=\s*"([0-9a-fA-F]{32})"', content or "")
            if not md5m:
                return ""
            md5 = md5m.group(1).lower()
            if md5 in self._sticker_cache:
                return self._sticker_cache[md5]
            media_dir = os.path.join(data_dir(), "wechat_media")
            os.makedirs(media_dir, exist_ok=True)
            sheet = os.path.join(media_dir, "emoji_%s_4f.png" % md5)
            if os.path.isfile(sheet):
                for ext in ("gif", "webp", "jpg", "png"):
                    p = os.path.join(media_dir, "emoji_%s.%s" % (md5, ext))
                    if os.path.isfile(p):
                        self._sticker_original[md5] = p
                        break
                self._sticker_cache[md5] = sheet
                return sheet
            for ext in ("gif", "webp", "jpg", "png"):
                p = os.path.join(media_dir, "emoji_%s.%s" % (md5, ext))
                if os.path.isfile(p):
                    self._sticker_original[md5] = p
                    if ext in ("gif", "webp"):
                        fs = self._make_gif_sheet(p, md5)
                        if fs:
                            return fs
                    self._sticker_cache[md5] = p
                    return p
            data = b""
            # 1) 消息 XML 自带 cdnurl：未收藏/自定义表情最可靠的来源
            urlm = re.search(r'cdnurl\s*=\s*"([^"]+)"', content or "")
            if urlm:
                try:
                    direct = html.unescape(urlm.group(1))
                    with urllib.request.urlopen(urllib.request.Request(
                            direct, headers={"User-Agent": "Mozilla/5.0"}), timeout=15) as resp:
                        data = resp.read()
                except Exception:
                    data = b""
            # 2) emoticon.db（hook 只读 SQL）
            row = {}
            if not data:
                try:
                    req = urllib.request.Request(
                        HOOK_API + "/QueryDB/execute",
                        data=json.dumps({"optDbName": "emoticon.db",
                                         "SQL": "SELECT md5,aes_key,cdn_url,encrypt_url FROM kNonStoreEmoticonTable WHERE md5='%s'" % md5}).encode(),
                        headers={"Content-Type": "application/json"}, method="POST")
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        rows = json.loads(resp.read().decode("utf-8")).get("data") or []
                    if rows:
                        row = rows[0]
                except Exception:
                    row = {}
            if not data:
                cdn = str(row.get("cdn_url") or "")
                if cdn:
                    try:
                        with urllib.request.urlopen(urllib.request.Request(
                                cdn, headers={"User-Agent": "Mozilla/5.0"}), timeout=15) as resp:
                            data = resp.read()
                    except Exception:
                        data = b""
            if not data:
                enc = str(row.get("encrypt_url") or "")
                keyhex = str(row.get("aes_key") or "")
                if enc and keyhex:
                    try:
                        with urllib.request.urlopen(urllib.request.Request(
                                enc, headers={"User-Agent": "Mozilla/5.0"}), timeout=15) as resp:
                            raw = resp.read()
                        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                        key = bytes.fromhex(keyhex)
                        dec = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor().update(raw)
                        pad = dec[-1] if dec else 0
                        if 1 <= pad <= 16 and dec[-pad:] == bytes([pad]) * pad:
                            dec = dec[:-pad]
                        data = dec
                    except Exception:
                        data = b""
            if not data:
                return ""
            if data[:3] == b"\xff\xd8\xff":
                ext = "jpg"
            elif data[:4] == b"\x89PNG":
                ext = "png"
            elif data[:4] == b"GIF8":
                ext = "gif"
            elif data[:4] == b"RIFF":
                ext = "webp"
            else:
                return ""
            path = os.path.join(media_dir, "emoji_%s.%s" % (md5, ext))
            with open(path, "wb") as f:
                f.write(data)
            self._sticker_original[md5] = path
            # 多模态模型通常更稳吃 jpg/png；GIF/WebP 取 4 帧拼成 2x2
            if ext in ("gif", "webp"):
                sheet = self._make_gif_sheet(path, md5)
                if sheet:
                    return sheet
            self._sticker_cache[md5] = path
            return path
        except Exception:
            return ""

    def _save_incoming_sticker(self, md5: str, session: str) -> None:
        """收到未记录的表情时自动保存到 chat_stickers，不改微信收藏。"""
        if not bool(getattr(self.config, "wechat_auto_save_stickers", True)):
            return
        try:
            from .account_store import account_emoji_dir, current_wxid
            wxid = self._self_wxid or current_wxid()
            if not wxid:
                return
            base = os.path.join(account_emoji_dir(wxid), "chat_stickers")
            os.makedirs(base, exist_ok=True)
            for ext in (".gif", ".webp", ".jpg", ".jpeg", ".png"):
                if os.path.isfile(os.path.join(base, md5 + ext)):
                    return
            orig = self._sticker_original.get(md5) or ""
            if not orig or not os.path.isfile(orig):
                media_dir = os.path.join(data_dir(), "wechat_media")
                for ext in (".gif", ".webp", ".jpg", ".jpeg", ".png"):
                    p = os.path.join(media_dir, "emoji_%s%s" % (md5, ext))
                    if os.path.isfile(p):
                        orig = p
                        break
            if not orig or not os.path.isfile(orig):
                return
            ext = os.path.splitext(orig)[1].lower()
            if ext not in (".gif", ".webp", ".jpg", ".jpeg", ".png"):
                return
            dst = os.path.join(base, md5 + ext)
            shutil.copy2(orig, dst)
            catalog_path = os.path.join(base, "catalog.json")
            data = {}
            try:
                with open(catalog_path, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except Exception:
                data = {}
            items = data.get("items") or []
            if not any(str(it.get("id") or "") == md5 for it in items):
                items.append({
                    "id": md5, "file": dst, "ext": ext, "cdn_url": "",
                    "source": "auto_receive", "session": str(session or ""),
                    "desc": "", "tags": [],
                    "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
            data["items"] = items
            data["updated_text"] = time.strftime("%Y-%m-%d %H:%M:%S")
            data["total"] = len(items)
            tmp = catalog_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, catalog_path)
            _send_log("auto_save_sticker md5=%s file=%s" % (md5, dst))
        except Exception as exc:
            _send_log("auto_save_sticker failed md5=%s %r" % (md5, exc))

    # ------------------------------------------------------------------ #
    def _to_message(self, m, username: str) -> Optional[Message]:
        try:
            sender_username = str(m.get("sender_username") or "")
            if sender_username and self._self_wxid and sender_username == self._self_wxid:
                return None

            db = self._db
            name = username
            try:
                name = (db.get_nickname(username) or username) if db is not None else username
            except Exception:
                name = username

            content = str(m.get("content") or "")
            mtype = str(m.get("type") or "")

            mt = MessageType.TEXT
            image_paths: List[str] = []
            raw_content = content
            if mtype in _STICKER_TYPES:
                mt = MessageType.OTHER
                content = STICKER_MARK
                self.last_image_msg = m
                # 动画表情：按 md5 从 emoticon.db 取 CDN 明图
                sp = self._sticker_image(raw_content)
                if sp:
                    image_paths.append(sp)
                    try:
                        md5m = re.search(r'md5\s*=\s*"([0-9a-fA-F]{32})"', raw_content or "")
                        if md5m:
                            mid = md5m.group(1).lower()
                            orig = self._sticker_original.get(mid, "")
                            if orig.lower().endswith((".gif", ".webp")):
                                content = ("[GIF表情包：下面是同一动画的4帧拼图"
                                           "（左上、右上、左下、右下），不是普通四格图片]")
                            self._save_incoming_sticker(mid, username)
                    except Exception:
                        pass
            elif mtype in _IMAGE_TYPES:
                mt = MessageType.IMAGE
                content = "[图片]"
                self.last_image_msg = m
            elif mtype not in ("文本",):
                mt = MessageType.OTHER

            # 微信图片：用 wechatauto 从本地 .dat 解密落地，交给多模态 AI
            if db is not None and mtype == "图片":
                try:
                    from wechatauto import MediaDownloader
                    if self._media is None:
                        media_dir = os.path.join(data_dir(), "wechat_media")
                        os.makedirs(media_dir, exist_ok=True)
                        self._media = MediaDownloader(db, save_dir=media_dir)
                    lid = int(m.get("local_id") or 0)
                    if lid:
                        path = self._media.download_media(username, lid)
                        if path and os.path.isfile(path):
                            image_paths.append(path)
                except Exception:
                    image_paths = []

            sender_name = sender_username or name
            try:
                if db is not None and sender_username:
                    sender_name = db.get_nickname(sender_username) or sender_username
            except Exception:
                pass

            return Message(
                platform=Platform.WECHAT,
                chat_id=str(username),   # 白名单/发送统一使用 wxid / @chatroom
                chat_name=str(name),
                is_group=username.endswith("@chatroom"),
                sender_id=sender_username,
                sender_name=str(sender_name),
                content=content,
                mentions_me=("@" in content),
                timestamp=time.time(),
                message_type=mt,
                image_paths=image_paths,
            )
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # 发送：只走 hook，绝不退回 UIA/OCR
    # ------------------------------------------------------------------ #
    def send_text(self, chat_id: str, text: str) -> bool:
        if not text:
            return False
        # 1) 优先 hook 协议（完全非 GUI）
        if self.hook_ready() and self._send_via_hook(chat_id, text):
            return True
        # 2) 屏幕自动化（复用持久 WeChatGUI，命中快速路径）
        if getattr(self.config, "wechat_screen_send", True):
            return _screen_send_text(str(chat_id), str(text))
        return False

    def _send_via_hook(self, chat_id: str, text: str) -> bool:
        # 文字也走直发，不解禁已隔离的微信窗口，避免回复瞬间抢输入
        db = self._load_db()
        wxid = ""
        if db is not None:
            try:
                hits = db.search_contact(chat_id) or []
                if hits:
                    wxid = str(hits[0].get("username") or "")
            except Exception:
                wxid = ""
        if not wxid:
            wxid = chat_id
        # 方案B1：强制当前会话作为供体直发，不切换目标
        try:
            self._ensure_donor()
        except Exception:
            pass
        try:
            from .wechat_isolator import begin_send
            begin_send(12)
        except Exception:
            pass
        try:
            if direct_available():
                ok = direct_send_text(wxid, text)
                _send_log("text direct ok=%s target=%s detail=%s" % (
                    ok, wxid, direct_last_detail("text")))
                return ok
        except Exception as exc:
            _send_log("text direct exception %r" % (exc,))
        finally:
            try:
                from .wechat_isolator import clear_wechat_focus, end_send
                end_send()
                clear_wechat_focus()
            except Exception:
                pass
        _send_log("text direct unavailable target=%s" % wxid)
        return False

    def send_image(self, chat_id: str, image_path: str,
                   route: str = "/SendImageVirtual2") -> bool:
        if not image_path:
            return False
        # 图片/贴纸走虚拟剪贴板，不需要解禁微信窗口，保持输入隔离
        # 1) hook 协议
        if self.hook_ready():
            db = self._load_db()
            wxid = ""
            if db is not None:
                try:
                    hits = db.search_contact(chat_id) or []
                    if hits:
                        wxid = str(hits[0].get("username") or "")
                except Exception:
                    wxid = ""
            if not wxid:
                wxid = chat_id
            # 方案B1：强制当前会话作为供体直发图片，不切换目标
            try:
                self._ensure_donor()
            except Exception:
                pass
            try:
                from .wechat_isolator import begin_send
                begin_send(15)
            except Exception:
                pass
            try:
                if direct_available():
                    ok = direct_send_image(wxid, image_path, route=route)
                    _send_log("image direct route=%s ok=%s target=%s detail=%s" % (
                        route, ok, wxid, direct_last_detail("image")))
                    return ok
            except Exception as exc:
                _send_log("image direct exception %r" % (exc,))
            finally:
                try:
                    from .wechat_isolator import clear_wechat_focus, end_send
                    end_send()
                    clear_wechat_focus()
                except Exception:
                    pass
            _send_log("image direct unavailable target=%s" % wxid)
            return False
        # 2) 屏幕自动化发送图片/文件
        if getattr(self.config, "wechat_screen_send", True):
            return _screen_send_file(str(chat_id), str(image_path))
        return False

    def _prepare_sticker_gif(self, path: str) -> str:
        """把任意表情图转成 ≤1MB 的 GIF；微信虚拟剪贴板会把 GIF 识别成原生贴纸。"""
        try:
            if not path or not os.path.isfile(path):
                return ""
            ext = os.path.splitext(path)[1].lower()
            size = os.path.getsize(path)
            if ext == ".gif" and size <= 1000000:
                return path
            with open(path, "rb") as f:
                digest = hashlib.md5(f.read()).hexdigest()
            outdir = os.path.join(data_dir(), "wechat_media", "sticker_send")
            os.makedirs(outdir, exist_ok=True)
            out = os.path.join(outdir, digest + ".gif")
            if os.path.isfile(out) and os.path.getsize(out) <= 1000000:
                return out
            from PIL import Image
            im = Image.open(path)
            frames = []
            n = int(getattr(im, "n_frames", 1) or 1)
            for i in range(min(n, 60)):
                try:
                    im.seek(i)
                except Exception:
                    break
                fr = im.convert("RGBA")
                bg = Image.new("RGB", fr.size, (255, 255, 255))
                bg.paste(fr, mask=fr.split()[-1])
                frames.append(bg)
            if not frames:
                return ""
            # 逐步缩小，直到 GIF ≤1MB
            for max_dim in (420, 320, 240, 180):
                scaled = []
                for fr in frames:
                    w, h = fr.size
                    scale = min(1.0, float(max_dim) / float(max(w, h)))
                    if scale < 1.0:
                        fr = fr.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                                       Image.Resampling.LANCZOS)
                    scaled.append(fr.convert("P", palette=Image.ADAPTIVE, colors=128))
                try:
                    if len(scaled) == 1:
                        scaled[0].save(out, "GIF", optimize=True)
                    else:
                        scaled[0].save(out, "GIF", save_all=True, append_images=scaled[1:],
                                       optimize=True, duration=100, loop=0)
                except Exception:
                    continue
                if os.path.isfile(out) and os.path.getsize(out) <= 1000000:
                    return out
            return ""
        except Exception as exc:
            _send_log("prepare_sticker_gif failed %r" % (exc,))
            return ""

    def send_sticker(self, chat_id: str, image_path: str) -> bool:
        """发送原生贴纸：优先 B2（无种子/无面板/无点击），失败回退种子直发。

        两条路径都不写剪贴板、不注入键鼠、**不切换当前会话**：
          - B2 /EmojiSendDataTo ：复用进程内 EmojiData 模板 + 自建 EmojiItem，
                                  按 md5 patch 后走供体直发（首选，无需种子）；
          - 种子 /EmojiSendMd5To ：复用已 pin 住的“种子”item（微信重启后需一次点击）。
        模板存在条件：进程内有聊天历史贴纸渲染 / 收到过贴纸 / 点击过收藏表情；
        三者都没有时 B2 返回 -3（no-template），此时若已有种子则回退种子发送。
        """
        try:
            if not image_path:
                return False
            base = os.path.splitext(os.path.basename(str(image_path)))[0].lower()
            if len(base) != 32 or any(c not in "0123456789abcdef" for c in base):
                _send_log("sticker native: bad md5 from path=%r" % (image_path,))
                return False
            if not self.hook_ready():
                _send_log("sticker native: hook not ready")
                return False
            target = str(chat_id)
            db = self._load_db()
            if db is not None:
                try:
                    hits = db.search_contact(chat_id) or []
                    if hits and hits[0].get("username"):
                        target = str(hits[0]["username"])
                except Exception:
                    pass
            if not target:
                _send_log("sticker native: cannot resolve target chat=%r" % (chat_id,))
                return False

            # 路径 1：B2（首选，无需种子/面板）
            b2 = _hook_post("/EmojiSendDataTo", {"md5": base, "to": target}, timeout=40.0)
            b2_ret = int((b2 or {}).get("ret") or 0) if b2 else -999
            if b2 and b2_ret == 0:
                _send_log("sticker native B2 md5=%s target=%r ok=True resp=%r"
                          % (base, target, b2))
                return True
            _send_log("sticker native B2 md5=%s target=%r ret=%s resp=%r -> fallback seed"
                      % (base, target, b2_ret, b2))

            # 路径 2：种子直发（B2 无模板 / 失败时回退）
            pin = _hook_post("/EmojiPinned", timeout=5.0)
            if not pin or int(pin.get("patchAddr") or 0) <= 0:
                _send_log("sticker native: no template and no seed item "
                          "(需要进程内有贴纸历史/收到贴纸，或人工发一张收藏贴纸)")
                return False
            res = _hook_post("/EmojiSendMd5To", {"md5": base, "to": target}, timeout=40.0)
            ok = bool(res and res.get("ret") == 0)
            _send_log("sticker native seed md5=%s target=%r ok=%s resp=%r"
                      % (base, target, ok, res))
            return ok
        except Exception as exc:
            _send_log("sticker native failed %r" % (exc,))
            return False

    def mark_last_favorite(self) -> bool:
        return False


# ---------------------------------------------------------------------- #
def fetch_wechat_chats() -> List[Tuple[str, str, bool]]:
    """独立扫描全部好友/群，返回 [(id, 显示名, 是否群)]；同时写入联系人缓存。"""
    try:
        from .wechat_contacts import contact_tuples
        return contact_tuples(force=True)
    except Exception:
        return []


def fetch_wechat_faces() -> List[dict]:
    return []
