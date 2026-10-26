# -*- coding: utf-8 -*-
"""微信账号（wxid）档案与账号级表情库。

目录约定（可随 exe 整体搬迁，基于数据根目录）：
- 账号档案：``<数据根>/WID/<wxid>.json``
- 表情库：  ``<数据根>/EMOJE/<wxid>/wechat_fav/``（图片按 md5 命名 + catalog.json）

数据根目录 = exe 所在目录（可用环境变量 ``CHATBOT_HOME`` 覆盖），见 ``src/core/paths.py``。

首次发现未保存的账号时，执行一次「表情库扫描」：
通过微信 hook 进程内数据库读取当前账号的收藏表情（md5/cdn_url），
下载明文原图（md5 校验）并生成 catalog.json。

说明：早期尝试的“内存扫描”（遍历微信进程堆找 EmojiItem）被验证不可靠（JS 堆/对象生命周期问题），
这里采用等价的权威来源：微信进程内 DB + CDN，完全非 GUI。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from typing import Dict, List, Optional

from ..core.paths import assets_dir, data_home, emoje_dir, project_root, wid_dir

HOOK_API = "http://127.0.0.1:30001"
_FAV_SUBDIR = "wechat_fav"


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _ensure_dir(d: str) -> str:
    os.makedirs(d, exist_ok=True)
    return d


def _hook_post(path: str, payload: dict, timeout: float = 120.0):
    req = urllib.request.Request(
        HOOK_API + path,
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _hook_db(sql: str, db: str = "emoticon.db") -> List[dict]:
    out = _hook_post("/QueryDB/execute", {"optDbName": db, "SQL": sql})
    if out.get("status") != 0:
        raise RuntimeError("hook db error: %s" % out.get("desc"))
    return out.get("data") or []


_WXID_CACHE = {"wxid": "", "ts": 0.0}


def _ensure_wechatauto_importable() -> None:
    """打包运行自带 wechatauto；源码运行时在 <数据根>/_internal 或项目 _internal 中查找。"""
    try:
        import wechatauto  # noqa: F401
        return
    except Exception:
        pass
    for d in (os.path.join(data_home(), "_internal"),
              os.path.join(project_root(), "_internal")):
        try:
            if os.path.isdir(os.path.join(d, "wechatauto")):
                sys.path.insert(0, d)
                return
        except Exception:
            continue


def current_wxid() -> str:
    """读取当前登录的微信 wxid（本地解密 DB；失败返回空串；缓存 10 分钟）。"""
    now = time.time()
    if _WXID_CACHE["wxid"] and (now - _WXID_CACHE["ts"]) < 600:
        return _WXID_CACHE["wxid"]
    try:
        _ensure_wechatauto_importable()
        from wechatauto import WeChatDB
        db = WeChatDB()
        wxid = str(getattr(db, "wxid", "") or "")
        if wxid:
            _WXID_CACHE["wxid"] = wxid
            _WXID_CACHE["ts"] = now
        return wxid
    except Exception:
        return _WXID_CACHE["wxid"]


def clear_wxid_cache() -> None:
    _WXID_CACHE["wxid"] = ""
    _WXID_CACHE["ts"] = 0.0


# --------------------------------------------------------------------------- #
# 账号档案
# --------------------------------------------------------------------------- #
def wid_path(wxid: str) -> str:
    return os.path.join(wid_dir(), _safe(wxid) + ".json")


def _safe(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name) or "unknown"


def account_known(wxid: str) -> bool:
    return bool(wxid) and os.path.isfile(wid_path(wxid))


def load_account(wxid: str) -> dict:
    try:
        with open(wid_path(wxid), "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_account(wxid: str, data: dict) -> dict:
    _ensure_dir(wid_dir())
    p = wid_path(wxid)
    try:
        with open(p, "r", encoding="utf-8") as f:
            old = json.load(f) or {}
    except Exception:
        old = {}
    old.update(data or {})
    old["wxid"] = wxid
    old.setdefault("first_seen", time.time())
    old["last_seen"] = time.time()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(old, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return old


def account_emoji_dir(wxid: str) -> str:
    """账号表情目录（wechat_fav 的父目录，兼容 WeChatFavStickers(images_dir)）。"""
    return _ensure_dir(os.path.join(emoje_dir(), _safe(wxid)))


def account_fav_dir(wxid: str) -> str:
    return _ensure_dir(os.path.join(account_emoji_dir(wxid), _FAV_SUBDIR))


def account_catalog_path(wxid: str) -> str:
    return os.path.join(account_fav_dir(wxid), "catalog.json")


def active_emoji_images_dir() -> Optional[str]:
    """当前账号已建库时，返回 EMOJE\\<wxid>（可直接作为 emoji_images_dir）。"""
    wxid = current_wxid()
    if not wxid:
        return None
    if os.path.isfile(account_catalog_path(wxid)):
        return account_emoji_dir(wxid)
    return None


# --------------------------------------------------------------------------- #
# 表情库扫描（首次账号）
# --------------------------------------------------------------------------- #
def _ext_from_md5_or_bytes(md5: str, data: bytes = b"") -> str:
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:4] == b"GIF8":
        return ".gif"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".gif"


def _download_plain(url: str, md5: str, save_dir: str) -> Optional[str]:
    """下载明文原图（cdn_url 为明文），md5 校验后保存为 <md5><ext>。"""
    if not url or not md5:
        return None
    for ext in (".gif", ".png", ".jpg", ".webp", ""):
        p = os.path.join(save_dir, md5 + ext)
        if ext and os.path.isfile(p) and os.path.getsize(p) > 0:
            return p
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = resp.read()
        if not data or hashlib.md5(data).hexdigest() != md5:
            return None
        ext = _ext_from_md5_or_bytes(md5, data)
        p = os.path.join(save_dir, md5 + ext)
        with open(p, "wb") as f:
            f.write(data)
        return p
    except Exception:
        return None


def _load_desc_hints() -> Dict[str, dict]:
    """从历史 catalog 收集 md5 -> 描述/标签，避免重复 AI 描述（同一 md5 跨账号复用）。"""
    hints: Dict[str, dict] = {}
    candidates = [
        os.path.join(assets_dir(), "emoji", _FAV_SUBDIR, "catalog.json"),
        os.path.join(project_root(), "assets", "emoji", _FAV_SUBDIR, "catalog.json"),
    ]
    root = emoje_dir()
    if os.path.isdir(root):
        for name in os.listdir(root):
            candidates.append(os.path.join(root, name, _FAV_SUBDIR, "catalog.json"))
    for p in candidates:
        try:
            items = json.load(open(p, encoding="utf-8"))
            if isinstance(items, dict):
                items = items.get("items") or items.get("stickers") or []
            for it in items:
                mid = str(it.get("id") or "")
                if mid and (it.get("desc") or it.get("tags")):
                    hints[mid] = {"desc": it.get("desc") or "", "tags": it.get("tags") or []}
        except Exception:
            continue
    return hints


def scan_favorites(wxid: str, log=None) -> dict:
    """扫描当前账号收藏表情 → EMOJE\\<wxid>\\wechat_fav（md5 命名 + catalog.json）。"""
    def _log(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    fav_dir = account_fav_dir(wxid)
    hints = _load_desc_hints()

    # 1) 收藏顺序 + 元数据（hook 进程内 DB，权威来源）
    order: List[str] = []
    meta: Dict[str, dict] = {}
    try:
        for row in _hook_db("SELECT md5 FROM kFavEmoticonOrderTable"):
            mid = str(row.get("md5") or "").lower()
            if re.fullmatch(r"[0-9a-f]{32}", mid):
                order.append(mid)
        for row in _hook_db("SELECT md5, type, caption, product_id, cdn_url, thumb_url FROM kNonStoreEmoticonTable"):
            mid = str(row.get("md5") or "").lower()
            meta[mid] = {
                "type": str(row.get("type") or ""),
                "caption": str(row.get("caption") or ""),
                "product_id": str(row.get("product_id") or ""),
                "cdn_url": str(row.get("cdn_url") or ""),
                "thumb_url": str(row.get("thumb_url") or ""),
            }
    except Exception as e:
        _log("[emoji] hook db failed: %r" % (e,))
        return {"ret": -1, "error": repr(e)}

    ids = order or list(meta.keys())
    # 补充：官方表情包文件表里的 md5（仅当能找到可下载 URL 时）
    try:
        for row in _hook_db("SELECT md5_ FROM kStoreEmoticonFilesTable"):
            mid = str(row.get("md5_") or "").lower()
            if re.fullmatch(r"[0-9a-f]{32}", mid) and mid not in meta:
                meta[mid] = {"cdn_url": "", "type": "2", "caption": "", "thumb_url": "", "product_id": ""}
    except Exception:
        pass
    # 过滤掉既无本地文件又无可下载 URL 的条目（避免 AI 选中无法发送的表情）
    usable = []
    for mid in ids:
        m = meta.get(mid) or {}
        if m.get("cdn_url") or m.get("thumb_url"):
            usable.append(mid)
    ids = usable

    _log("[emoji] account=%s favorites=%d usable=%d" % (wxid, len(order), len(ids)))

    items: List[dict] = []
    downloaded = 0
    for mid in ids:
        m = meta.get(mid) or {}
        url = m.get("cdn_url") or m.get("thumb_url") or ""
        path = _download_plain(url, mid, fav_dir) if url else None
        if path:
            downloaded += 1
        hint = hints.get(mid) or {}
        items.append({
            "id": mid,
            "type": m.get("type") or "",
            "caption": m.get("caption") or "",
            "store_caption": "",
            "package": m.get("product_id") or "",
            "cdn_url": url,
            "thumb_url": m.get("thumb_url") or "",
            "file": path or "",
            "ext": os.path.splitext(path)[1] if path else "",
            "desc": hint.get("desc") or "",
            "tags": hint.get("tags") or [],
        })

    catalog = {
        "wxid": wxid,
        "scanned_at": time.time(),
        "scanned_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(items),
        "downloaded": downloaded,
        "items": items,
    }
    tmp = account_catalog_path(wxid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=1)
    os.replace(tmp, account_catalog_path(wxid))

    save_account(wxid, {
        "emoji_dir": fav_dir,
        "emoji_count": len(items),
        "emoji_downloaded": downloaded,
        "last_scan": time.time(),
        "last_scan_str": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    _log("[emoji] saved %d items (%d downloaded) -> %s" % (len(items), downloaded, account_catalog_path(wxid)))
    return {"ret": 0, "wxid": wxid, "total": len(items), "downloaded": downloaded,
            "catalog": account_catalog_path(wxid)}


def ensure_current_account(auto_scan: bool = True, log=None) -> dict:
    """确保当前账号已建档；新账号（或已建档但表情库缺失）执行一次表情库扫描。"""
    wxid = current_wxid()
    if not wxid:
        return {"ret": -2, "error": "cannot detect wxid"}
    known = account_known(wxid)
    has_catalog = os.path.isfile(account_catalog_path(wxid))
    if (not known) or (not has_catalog):
        if log:
            log("[account] wxid=%s known=%s catalog=%s -> scanning emoji"
                % (wxid, known, has_catalog))
        if auto_scan:
            scan_favorites(wxid, log=log)
        else:
            save_account(wxid, {"emoji_dir": account_fav_dir(wxid)})
    else:
        save_account(wxid, {})
    return {"ret": 0, "wxid": wxid, "known": known, "account": load_account(wxid)}
