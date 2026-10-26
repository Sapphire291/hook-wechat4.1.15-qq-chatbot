# -*- coding: utf-8 -*-
"""手动扫描聊天记录里的动画表情，保存到账号本地表情库。

范围：最近 N 个会话 × 每个会话最近 M 条消息（默认 5 × 120）。
输出：<数据根>/EMOJE/<wxid>/chat_stickers/<md5>.<ext> + catalog.json

只做本地下载/缓存，不调用微信收藏接口，不会改变微信账号收藏。
"""
from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import urllib.request
from typing import Dict, List, Optional

from .account_store import account_emoji_dir, current_wxid

HOOK_API = "http://127.0.0.1:30001"
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def _hook_db(sql: str) -> List[dict]:
    try:
        req = urllib.request.Request(
            HOOK_API + "/QueryDB/execute",
            data=json.dumps({"optDbName": "emoticon.db", "SQL": sql}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8")).get("data") or []
    except Exception:
        return []


def _ext_from_bytes(data: bytes) -> Optional[str]:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:4] == b"GIF8":
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _download(url: str) -> bytes:
    if not url:
        return b""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read()
    except Exception:
        return b""


def _load_catalog(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def scan_chat_stickers(session_limit: int = 5, msg_limit: int = 120,
                       progress=None) -> dict:
    """扫描最近会话中的动画表情并保存到本地 chat_stickers 库。"""
    wxid = current_wxid()
    if not wxid:
        return {"ret": -1, "error": "无法获取当前 wxid"}
    try:
        from wechatauto import WeChatDB
        db = WeChatDB()
    except Exception as exc:
        return {"ret": -2, "error": "无法打开微信数据库: %r" % (exc,)}

    base = os.path.join(account_emoji_dir(wxid), "chat_stickers")
    os.makedirs(base, exist_ok=True)
    catalog_path = os.path.join(base, "catalog.json")
    old = _load_catalog(catalog_path)
    items: Dict[str, dict] = {}
    for it in old.get("items") or []:
        if it.get("id"):
            items[str(it["id"])] = it

    try:
        sessions = db.get_sessions(limit=max(1, int(session_limit))) or []
    except Exception:
        sessions = []
    found: Dict[str, dict] = {}
    scanned = 0
    for si, s in enumerate(sessions):
        user = str(s.get("username") or "")
        if not user:
            continue
        try:
            msgs = db.get_messages(user, limit=max(1, int(msg_limit))) or []
        except Exception:
            msgs = []
        scanned += len(msgs)
        for m in msgs:
            if m.get("type") not in ("动画表情", "表情"):
                continue
            content = str(m.get("content") or "")
            md5m = re.search(r'md5\s*=\s*"([0-9a-fA-F]{32})"', content)
            if not md5m:
                continue
            md5 = md5m.group(1).lower()
            urlm = re.search(r'cdnurl\s*=\s*"([^"]+)"', content)
            found.setdefault(md5, {
                "md5": md5,
                "cdnurl": html.unescape(urlm.group(1)) if urlm else "",
                "session": user,
                "local_id": m.get("local_id"),
            })
        if progress:
            try:
                progress(si + 1, len(sessions), len(found))
            except Exception:
                pass

    downloaded = 0
    existing = 0
    failed = 0
    for md5, info in found.items():
        old_item = items.get(md5) or {}
        old_file = str(old_item.get("file") or "")
        if old_file and not os.path.isfile(old_file):
            from ..core.paths import resolve_stored_file
            old_file = resolve_stored_file(old_file, [base])
        if old_file and os.path.isfile(old_file):
            info["file"] = old_file
            existing += 1
            continue
        # 已有同 md5 缓存
        cached = None
        for ext in _IMG_EXTS:
            p = os.path.join(base, md5 + ext)
            if os.path.isfile(p):
                cached = p
                break
        if cached:
            info["file"] = cached
            existing += 1
            continue
        url = str(info.get("cdnurl") or "")
        if not url:
            rows = _hook_db(
                "SELECT cdn_url,thumb_url FROM kNonStoreEmoticonTable WHERE md5='%s'" % md5)
            if rows:
                url = str(rows[0].get("cdn_url") or rows[0].get("thumb_url") or "")
        data = _download(url)
        ext = _ext_from_bytes(data) if data else None
        if not data or not ext:
            failed += 1
            continue
        path = os.path.join(base, md5 + ext)
        try:
            with open(path, "wb") as f:
                f.write(data)
        except Exception:
            failed += 1
            continue
        info["file"] = path
        info["ext"] = ext
        downloaded += 1

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for md5, info in found.items():
        f = str(info.get("file") or "")
        if not f:
            continue
        prev = items.get(md5) or {}
        items[md5] = {
            "id": md5,
            "file": f,
            "ext": os.path.splitext(f)[1],
            "cdn_url": str(info.get("cdnurl") or prev.get("cdn_url") or ""),
            "source": "chat_scan",
            "session": str(info.get("session") or prev.get("session") or ""),
            "local_id": info.get("local_id"),
            "desc": prev.get("desc") or "",
            "tags": prev.get("tags") or [],
            "scanned_at": now,
        }

    payload = {
        "wxid": wxid,
        "updated_at": time.time(),
        "updated_text": now,
        "session_limit": int(session_limit),
        "msg_limit": int(msg_limit),
        "scanned_messages": scanned,
        "found": len(found),
        "downloaded": downloaded,
        "existing": existing,
        "failed": failed,
        "total": len(items),
        "items": list(items.values()),
    }
    tmp = catalog_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, catalog_path)
    except Exception as exc:
        return {"ret": -3, "error": "写 catalog 失败: %r" % (exc,), **payload}
    return {"ret": 0, "catalog": catalog_path, **payload}


# --------------------------------------------------------------------------- #
# AI 重新识别/标注本地聊天表情（desc/tags 写回 catalog.json）
# --------------------------------------------------------------------------- #
def _save_catalog_atomic(path: str, data: dict) -> None:
    try:
        data["updated_at"] = time.time()
        data["updated_text"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        pass


def describe_chat_stickers(images_dir: str = "", config=None, limit=None, workers: int = 2,
                           force: bool = False, vision=None, progress=None) -> dict:
    """用视觉 AI 重新识别本地 chat_stickers 表情，更新 desc/tags 并写回 catalog。

    参数：
        images_dir 账号表情库目录（空=自动解析：当前账号 EMOJE/<wxid>，否则配置目录）
        force      True=连已有描述的也重新识别（“重新扫描表情包”用）
        vision     识别函数(path)->{"desc","tags"}；空则用 WeChatFavStickers._vision_ask
        progress   回调(done, total, ok)
    """
    base = str(images_dir or "")
    if not base:
        try:
            from .account_store import active_emoji_images_dir
            base = active_emoji_images_dir() or ""
        except Exception:
            base = ""
    if not base:
        from ..core.paths import resolve_path
        base = resolve_path(getattr(config, "emoji_images_dir", ""), "assets/emoji")
    sub = os.path.join(base, "chat_stickers")
    catalog_path = os.path.join(sub, "catalog.json")
    if not os.path.isfile(catalog_path):
        return {"total": 0, "pending": 0, "described": 0, "catalog": catalog_path}
    try:
        with open(catalog_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        return {"total": 0, "pending": 0, "described": 0, "error": repr(exc)}
    items = [x for x in (data.get("items") or []) if isinstance(x, dict)]
    from ..core.paths import resolve_stored_file
    todo = []
    for it in items:
        f = str(it.get("file") or "")
        if not f:
            continue
        f2 = resolve_stored_file(f, [sub, base])
        if not os.path.isfile(f2):
            continue
        it["file"] = f2
        if force or not str(it.get("desc") or "").strip():
            todo.append(it)
    if limit:
        todo = todo[:limit]
    if not todo:
        return {"total": len(items), "pending": 0, "described": 0, "catalog": catalog_path}
    if vision is None:
        try:
            from ..emoji.wechat_fav import WeChatFavStickers
            vision = WeChatFavStickers(base, config)._vision_ask
        except Exception as exc:
            return {"total": len(items), "pending": len(todo), "described": 0,
                    "error": "无法初始化视觉识别: %r" % (exc,)}
    lock = threading.Lock()
    ok = 0
    counter = {"done": 0}

    def worker(chunk):
        nonlocal ok
        for it in chunk:
            try:
                r = vision(it["file"])
            except Exception:
                r = None
            with lock:
                counter["done"] += 1
                if r:
                    it["desc"] = str(r.get("desc") or "")
                    it["tags"] = [str(t) for t in (r.get("tags") or [])][:5]
                    ok += 1
                if counter["done"] % 10 == 0:
                    _save_catalog_atomic(catalog_path, data)
                if progress:
                    try:
                        progress(counter["done"], len(todo), ok)
                    except Exception:
                        pass

    threads = []
    for i in range(max(1, int(workers or 2))):
        th = threading.Thread(target=worker, args=(todo[i::max(1, int(workers or 2))],), daemon=True)
        th.start()
        threads.append(th)
    for th in threads:
        th.join()
    _save_catalog_atomic(catalog_path, data)
    if progress:
        try:
            progress(len(todo), len(todo), ok)
        except Exception:
            pass
    return {"total": len(items), "pending": len(todo), "described": ok, "catalog": catalog_path}
