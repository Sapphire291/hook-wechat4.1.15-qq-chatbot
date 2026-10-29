# -*- coding: utf-8 -*-
"""微信接入适配器（屏幕零接触版）。

原则：**绝不控制鼠标/键盘/窗口焦点**。

- 读消息：使用 wechatauto 解密本地 SQLCipher 数据库（纯本地读盘，不碰屏幕）；
- 发消息：只走本机注入 hook 的 HTTP API（默认 30001）。
  若未安装 hook，则发送直接返回 False，**不会退回 UIA/OCR 去点屏幕**。

为什么发送需要 hook：
  微信 4.1.x 的发送必须经过客户端网络协议，纯本地数据库写入不会真正发出。
  非 GUI 的唯一可靠方式是给微信进程注入 hook 后由 HTTP 接口驱动。
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
from .send_probe import probe

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
    from .direct_send import (current_chat as direct_current_chat,
                              direct_available, direct_send_image, direct_send_text,
                              last_detail as direct_last_detail)
except Exception:  # pragma: no cover
    def direct_current_chat(*a, **k):
        return ""
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
GIF_SHEET_NOTE = ("同一段动画的4帧合成图（左上、右上、左下、右下），"
                  "不是四张独立图片，请按一个连续动作理解")
_STICKER_TYPES = ("动画表情", "表情", "emotion")
_FILE_TYPES = ("文件", "文件消息", "file", "File")
_FILE_EXTS = (".docx", ".doc", ".pdf", ".txt", ".md", ".xlsx", ".xls", ".pptx", ".ppt",
              ".csv", ".rtf")
_TEXT_DEDUP_LOCK = threading.Lock()
_TEXT_DEDUP = {}   # (chat_id, text_hash) -> last_sent_ts，防止 500 重试/并发导致重复气泡

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
    """探测本机 hook 的 HTTP 服务是否就绪。"""
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
        probe("ensure_donor_check", donor=donor, current=current)
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
            probe("ensure_donor_switched", donor=donor, before=current,
                  out_chat=out.get("chat"), ret=out.get("ret"))
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

    @staticmethod
    def _incoming_sticker_text(desc: str, tags=None, animated: bool = False) -> str:
        """已有标签时，用一条文字消息代替图片送给主模型（不再走视觉）。"""
        parts = ["对方发来表情包"]
        if animated:
            parts.append("动画（4帧合成：左上→右上→左下→右下）")
        if desc:
            parts.append("描述：" + str(desc).strip())
        if tags:
            parts.append("标签：" + "、".join([str(x) for x in tags]))
        return "[" + "｜".join(parts) + "]"

    def _describe_incoming_async(self, md5: str, path: str, animated: bool) -> None:
        """后台给首次收到的表情打详细标签，不阻塞本轮回复。"""
        def worker():
            try:
                from .chat_stickers import describe_incoming_sticker
                r = describe_incoming_sticker(md5, path, animated=animated, config=self.config)
                _send_log("incoming_sticker_desc md5=%s ok=%s %r" % (md5, r.get("ok"), r))
            except Exception as exc:
                _send_log("incoming_sticker_desc failed md5=%s %r" % (md5, exc))
        threading.Thread(target=worker, name="describe-incoming-sticker", daemon=True).start()

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
            file_paths: List[str] = []
            file_lid = 0
            file_name = ""
            raw_content = content
            if mtype in _STICKER_TYPES:
                mt = MessageType.OTHER
                self.last_image_msg = m
                md5m = re.search(r'md5\s*=\s*"([0-9a-fA-F]{32})"', raw_content or "")
                mid = md5m.group(1).lower() if md5m else ""
                cached = {}
                if mid:
                    try:
                        from .chat_stickers import find_chat_sticker
                        cached = find_chat_sticker(mid) or {}
                    except Exception:
                        cached = {}
                desc = str(cached.get("desc") or "").strip()
                tags = [str(x).strip() for x in (cached.get("tags") or []) if str(x).strip()]
                animated = str(cached.get("kind") or "") == "animated"
                if desc:
                    # 已有详细标签：不再下载/不再附图，主模型只读文字标签
                    content = self._incoming_sticker_text(desc, tags, animated)
                else:
                    # 首次/未标注：取图并交给多模态；后台识图打详细标签
                    sp = self._sticker_image(raw_content)
                    if sp:
                        image_paths.append(sp)
                        if mid:
                            try:
                                orig = self._sticker_original.get(mid, "")
                                if orig.lower().endswith((".gif", ".webp")):
                                    animated = True
                                self._save_incoming_sticker(mid, username)
                                self._describe_incoming_async(mid, sp, animated)
                            except Exception:
                                pass
                    if animated:
                        content = "[GIF表情包：" + GIF_SHEET_NOTE + "]"
                    else:
                        content = STICKER_MARK
            elif mtype in _IMAGE_TYPES:
                mt = MessageType.IMAGE
                content = "[图片]"
                self.last_image_msg = m
            elif (mtype in _FILE_TYPES
                  or ("<appmsg" in (raw_content or "")
                      and (("<type>6</type>" in (raw_content or ""))
                           or any(ext in (raw_content or "").lower() for ext in _FILE_EXTS)))):
                mt = MessageType.OTHER
                mm = re.search(r"<title>(.*?)</title>", raw_content or "", re.S)
                file_name = re.sub(r"<[^>]+>", "", mm.group(1)).strip() if mm else ""
                if not file_name:
                    mm = re.search(r"<des>(.*?)</des>", raw_content or "", re.S)
                    file_name = re.sub(r"<[^>]+>", "", mm.group(1)).strip() if mm else ""
                file_lid = int(m.get("local_id") or 0)
                content = "[文件] " + (file_name or "未命名文档")
                _send_log("file message detected mtype=%r lid=%s name=%r raw=%r" % (
                    mtype, file_lid, file_name, (raw_content or "")[:500]))
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

            message = Message(
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
                file_paths=file_paths,
            )
            if file_lid or file_name:
                try:
                    setattr(message, "_file_local_id", file_lid)
                    setattr(message, "_file_name", file_name)
                except Exception:
                    pass
            return message
        except Exception:
            return None

    def download_document(self, chat_id: str, local_id: int, file_name: str = "") -> str:
        """后台下载微信文件消息，返回本地路径；失败返回空串。"""
        try:
            db = self._load_db()
            if db is None:
                _send_log("download_document no db chat=%s lid=%s name=%r" % (chat_id, local_id, file_name))
                return ""
            from wechatauto import MediaDownloader
            from ..core.document_store import documents_dir
            out_dir = documents_dir("wechat", str(chat_id))
            os.makedirs(out_dir, exist_ok=True)
            path = ""
            if local_id:
                media = MediaDownloader(db, save_dir=out_dir)
                try:
                    path = media.download_media(str(chat_id), int(local_id)) or ""
                except Exception as exc:
                    _send_log("download_document media failed lid=%s %r" % (local_id, exc))
                if not path:
                    try:
                        path = media.download_file(str(chat_id), int(local_id), out_dir) or ""
                    except Exception as exc:
                        _send_log("download_document file failed lid=%s %r" % (local_id, exc))
            # 兜底：微信已落盘的 msg/file 目录里按原文件名找
            if not path and file_name:
                try:
                    base = os.path.join(str(getattr(db, "account_dir", "") or ""), "msg", "file")
                    target = os.path.basename(file_name).strip().lower()
                    stem = os.path.splitext(target)[0]
                    exact = []
                    fuzzy = []
                    for root, _dirs, files in os.walk(base):
                        for fn in files:
                            low = fn.lower()
                            if low == target:
                                exact.append(os.path.join(root, fn))
                            elif stem and len(stem) >= 3 and (stem in low or low.startswith(stem[:12])):
                                fuzzy.append(os.path.join(root, fn))
                    src = ""
                    if exact:
                        src = exact[0]
                    elif fuzzy:
                        src = max(fuzzy, key=lambda p: os.path.getmtime(p))
                    if src:
                        dst = os.path.join(out_dir, "%s_%s" % (
                            int(local_id or 0), os.path.basename(src)))
                        shutil.copy2(src, dst)
                        path = dst
                        _send_log("download_document fuzzy name=%r -> %s" % (file_name, src))
                except Exception as exc:
                    _send_log("download_document search failed name=%r %r" % (file_name, exc))
            # 文件名/资源缺失时：尝试取 msg/file 下最近 10 分钟内最新的文件
            if not path and not file_name:
                try:
                    base = os.path.join(str(getattr(db, "account_dir", "") or ""), "msg", "file")
                    newest = None
                    newest_t = time.time() - 600
                    for root, _dirs, files in os.walk(base):
                        for fn in files:
                            fp = os.path.join(root, fn)
                            try:
                                mt = os.path.getmtime(fp)
                            except Exception:
                                continue
                            if mt >= newest_t and (newest is None or mt > newest[0]):
                                newest = (mt, fp)
                    if newest:
                        dst = os.path.join(out_dir, "%s_%s" % (
                            int(local_id or 0), os.path.basename(newest[1])))
                        shutil.copy2(newest[1], dst)
                        path = dst
                        _send_log("download_document fallback newest file=%s" % dst)
                except Exception as exc:
                    _send_log("download_document newest fallback failed %r" % (exc,))
            # 分渠道下载：只扫描微信本地落盘，不再点击 UI
            if path and os.path.isfile(path):
                _send_log("download_document ok name=%r lid=%s -> %s" % (file_name, local_id, path))
                return path
            _send_log("download_document not_found name=%r lid=%s" % (file_name, local_id))
        except Exception as exc:
            _send_log("download_document failed chat=%s lid=%s %r" % (chat_id, local_id, exc))
        return ""

    # ------------------------------------------------------------------ #
    # 发送：只走 hook，绝不退回 UIA/OCR
    # ------------------------------------------------------------------ #
    def _probe_current_chat(self) -> str:
        try:
            return str(direct_current_chat() or "")
        except Exception:
            return ""

    def send_text(self, chat_id: str, text: str) -> bool:
        if not text:
            return False
        # 去重：同一会话 5 秒内重复的完全相同文本只发一次（防 hook 500 重试/并发重复）
        dkey = (str(chat_id), hashlib.sha1(str(text).encode("utf-8")).hexdigest())
        now = time.time()
        with _TEXT_DEDUP_LOCK:
            last = float(_TEXT_DEDUP.get(dkey) or 0.0)
            if now - last < 5.0:
                _send_log("text duplicate suppressed target=%s age=%.1fs" % (chat_id, now - last))
                return True
        probe("send_text_begin", target=chat_id, text=text,
              current=self._probe_current_chat())
        # 1) 优先 hook 协议（完全非 GUI）
        if self.hook_ready() and self._send_via_hook(chat_id, text):
            with _TEXT_DEDUP_LOCK:
                _TEXT_DEDUP[dkey] = time.time()
            probe("send_text_done", target=chat_id, text=text,
                  current=self._probe_current_chat(), ok=True)
            return True
        detail = ""
        try:
            detail = str(direct_last_detail("text") or "")
        except Exception:
            detail = ""
        if "ambiguous" in detail or "500" in detail:
            # 结果不确定：可能已经发送成功；绝不重试/屏幕兜底，避免重复气泡
            _send_log("text ambiguous skip retry target=%s detail=%s" % (chat_id, detail[:160]))
            return False
        # 1b) 冗余一次：hook 首次明确失败时重试
        _send_log("text hook failed, retry once target=%s" % chat_id)
        try:
            self._ensure_donor()
        except Exception:
            pass
        time.sleep(0.8)
        if self.hook_ready() and self._send_via_hook(chat_id, text):
            with _TEXT_DEDUP_LOCK:
                _TEXT_DEDUP[dkey] = time.time()
            _send_log("text retry ok target=%s" % chat_id)
            return True
        # 2) 屏幕自动化（复用持久 WeChatGUI，命中快速路径）
        if getattr(self.config, "wechat_screen_send", True):
            ok = _screen_send_text(str(chat_id), str(text))
            if ok:
                with _TEXT_DEDUP_LOCK:
                    _TEXT_DEDUP[dkey] = time.time()
            return ok
        _send_log("text send failed after retry target=%s" % chat_id)
        return False

    def _send_via_hook(self, chat_id: str, text: str) -> bool:
        # 文字也走直发，不解禁已隔离的微信窗口，避免回复瞬间抢输入
        probe("hook_send_begin", target=chat_id, text=text,
              current=self._probe_current_chat())
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
        probe("hook_after_ensure_donor", target=wxid,
              current=self._probe_current_chat())
        try:
            from .wechat_isolator import begin_send
            begin_send(12)
        except Exception:
            pass
        probe("hook_after_begin_send", target=wxid,
              current=self._probe_current_chat())
        try:
            if direct_available():
                ok = direct_send_text(wxid, text)
                detail = direct_last_detail("text")
                _send_log("text direct ok=%s target=%s detail=%s" % (ok, wxid, detail))
                probe("hook_direct_result", target=wxid, ok=ok, detail=detail,
                      current=self._probe_current_chat())
                return ok
        except Exception as exc:
            _send_log("text direct exception %r" % (exc,))
            probe("hook_direct_exception", target=wxid, error=repr(exc),
                  current=self._probe_current_chat())
        finally:
            try:
                from .wechat_isolator import clear_wechat_focus, end_send
                end_send()
                clear_wechat_focus()
            except Exception:
                pass
        _send_log("text direct unavailable target=%s" % wxid)
        probe("hook_direct_unavailable", target=wxid,
              current=self._probe_current_chat())
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
        """发送原生贴纸（供体发送）：只走 B2 /EmojiSendDataTo。

        策略（按用户要求）：
          - B2 第一次失败 -> 等 0.8 秒，立刻再试一次 B2；
          - 两次都失败 -> 终止这张表情包的发送（不再回退种子/面板/图片）；
          - 不再设 40s 自定义超时（沿用 _hook_post 默认）。
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

            # 只走 B2（供体直发）；失败 0.8s 后再试一次，两次失败即终止
            b2 = None
            b2_ret = -999
            for attempt in (1, 2):
                try:
                    b2 = _hook_post("/EmojiSendDataTo", {"md5": base, "to": target})
                except Exception as e:
                    b2 = None
                    _send_log("sticker native B2 attempt %d exception: %r" % (attempt, e))
                b2_ret = int((b2 or {}).get("ret") or 0) if b2 else -999
                if b2 and b2_ret == 0:
                    _send_log("sticker native B2 md5=%s target=%r ok=True attempt=%d resp=%r"
                              % (base, target, attempt, b2))
                    return True
                _send_log("sticker native B2 md5=%s target=%r attempt=%d ret=%s resp=%r"
                          % (base, target, attempt, b2_ret, b2))
                if attempt == 1:
                    time.sleep(0.8)
            _send_log("sticker native B2 failed twice, abort sticker md5=%s target=%r"
                      % (base, target))
            return False
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
