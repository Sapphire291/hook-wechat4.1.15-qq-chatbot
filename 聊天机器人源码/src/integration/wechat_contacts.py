# -*- coding: utf-8 -*-
"""微信联系人/群扫描与缓存。

目标：不依赖 hook 发送能力，独立从本地解密数据库扫描全部好友和群，
保存 wxid / @chatroom ID，供白名单显示和按昵称/备注查找。

缓存：<exe 目录>/data/wechat_contacts.json
"""
from __future__ import annotations

import json
import os
import time

from ..core.paths import data_dir

CACHE_NAME = "wechat_contacts.json"


def cache_path() -> str:
    return os.path.join(data_dir(), CACHE_NAME)


def _display(nick: str, remark: str, username: str) -> str:
    return (remark or nick or username or "").strip()


def _rows_from_wechatauto():
    """直接读 wechatauto 的 contact.db（独立于 hook HTTP）。"""
    from wechatauto import WeChatDB
    db = WeChatDB()
    self_wxid = ""
    try:
        info = db.get_self_info() or {}
        self_wxid = str(info.get("username") or info.get("wxid") or "")
    except Exception:
        pass
    conn = None
    try:
        conn = db._contact_conn()
        try:
            cur = conn.execute("SELECT username,nick_name,remark,local_type FROM contact")
            rows = cur.fetchall()
        except Exception:
            cur = conn.execute("SELECT username,nick_name,remark FROM contact")
            rows = cur.fetchall()
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
    out = []
    for r in rows:
        try:
            if hasattr(r, "keys"):
                d = dict(r)
            else:
                d = {"username": r[0], "nick_name": r[1], "remark": r[2],
                     "local_type": r[3] if len(r) > 3 else 1}
        except Exception:
            continue
        username = str(d.get("username") or "").strip()
        if not username:
            continue
        nick = str(d.get("nick_name") or "").strip()
        remark = str(d.get("remark") or "").strip()
        try:
            local_type = int(d.get("local_type") or 1)
        except Exception:
            local_type = 1
        out.append({"username": username, "nick_name": nick, "remark": remark,
                    "local_type": local_type})
    return self_wxid, out


def _rows_from_hook():
    """回退：hook 的只读 SQL 接口（不依赖发送能力）。"""
    import urllib.request
    req = urllib.request.Request(
        "http://127.0.0.1:30001/QueryDB/execute",
        data=json.dumps({"optDbName": "contact.db",
                         "SQL": "SELECT username,nick_name,remark,local_type FROM contact"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8")).get("data") or []
    out = []
    for d in data:
        username = str(d.get("username") or "").strip()
        if username:
            out.append({"username": username,
                        "nick_name": str(d.get("nick_name") or ""),
                        "remark": str(d.get("remark") or ""),
                        "local_type": int(d.get("local_type") or 1)})
    return "", out


def scan_wechat_contacts(persist: bool = True) -> dict:
    """扫描全部好友与群，返回并可选保存缓存。"""
    self_wxid, rows = "", []
    errors = []
    try:
        self_wxid, rows = _rows_from_wechatauto()
    except Exception as exc:
        errors.append("wechatauto:%r" % (exc,))
    if not rows:
        try:
            self_wxid, rows = _rows_from_hook()
        except Exception as exc:
            errors.append("hook:%r" % (exc,))
    friends, groups = [], []
    seen = set()
    for d in rows:
        username = d["username"]
        if username in seen:
            continue
        seen.add(username)
        is_group = username.endswith("@chatroom")
        nick, remark = d.get("nick_name", ""), d.get("remark", "")
        local_type = d.get("local_type", 1)
        if is_group:
            groups.append({"id": username, "name": _display(nick, remark, username),
                           "nick_name": nick, "remark": remark, "is_group": True})
        else:
            if username.startswith("gh_"):
                continue  # 公众号，不算好友
            if username.startswith("@") or username in ("medianote", "newsapp", "notifymessage"):
                continue
            if local_type not in (1, 0, 3) and not username.startswith(("wxid_", "filehelper")):
                continue
            friends.append({"id": username, "name": _display(nick, remark, username),
                            "nick_name": nick, "remark": remark, "is_group": False})
    friends.sort(key=lambda x: x["name"].casefold())
    groups.sort(key=lambda x: x["name"].casefold())
    payload = {
        "updated_at": int(time.time()),
        "updated_text": time.strftime("%Y-%m-%d %H:%M:%S"),
        "self_wxid": self_wxid,
        "friends": friends,
        "groups": groups,
        "count": len(friends) + len(groups),
        "errors": errors,
    }
    if persist and (friends or groups):
        try:
            p = cache_path()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        except Exception as exc:
            payload.setdefault("errors", []).append("save:%r" % (exc,))
    return payload


def load_cached_contacts() -> dict:
    try:
        with open(cache_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"friends": [], "groups": [], "count": 0}


def get_contacts(force: bool = False) -> dict:
    if not force:
        cached = load_cached_contacts()
        if cached.get("count", 0) > 0:
            return cached
    return scan_wechat_contacts(persist=True)


def contact_tuples(force: bool = False):
    """返回 [(id, display_name, is_group)]，供白名单选择/查找。"""
    data = get_contacts(force=force)
    out = []
    for g in data.get("groups") or []:
        out.append((str(g.get("id")), str(g.get("name") or g.get("id")), True))
    for f in data.get("friends") or []:
        out.append((str(f.get("id")), str(f.get("name") or f.get("id")), False))
    return out


def resolve_contact(name: str, force: bool = False):
    """按 wxid / 昵称 / 备注 / 显示名解析联系人。"""
    if not name:
        return None
    low = str(name).strip().casefold()
    data = get_contacts(force=force)
    for group in (data.get("groups") or [], data.get("friends") or []):
        for item in group:
            fields = [str(item.get("id") or ""), str(item.get("name") or ""),
                      str(item.get("nick_name") or ""), str(item.get("remark") or "")]
            if low in [f.strip().casefold() for f in fields if f]:
                return item
    return None
