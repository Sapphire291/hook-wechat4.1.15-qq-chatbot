# -*- coding: utf-8 -*-
"""chatbot 的微信直发适配（方案B1 / exp67 / mode11）。

- 以“当前打开的微信会话”为供体；
- 发送前把当前 session username 临时换成目标 wxid（支持不等长）；
- 微信自己构造并加密消息，发送后自动还原，当前聊天不切换到目标；
- 文本 / 图片 / 贴纸都走同一入口。
"""
from __future__ import annotations

import json
import time
import urllib.request

HOOK_API = "http://127.0.0.1:30001"


def _post(path, payload, timeout=40):
    req = urllib.request.Request(
        HOOK_API + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _sql(db, query):
    try:
        rows = _post("/QueryDB/execute", {"optDbName": db, "SQL": query}, 15).get("data") or []
        return rows
    except Exception:
        return []


def current_chat():
    try:
        return _post("/GetChat", {}).get("chat", "") or ""
    except Exception:
        return ""


def resolve_wxid(name):
    if not name:
        return ""
    low = str(name).strip()
    if low.startswith(("wxid_", "gh_", "filehelper")) or low.endswith("@chatroom"):
        return low
    esc = low.replace("'", "''")
    rows = _sql(
        "contact.db",
        "SELECT username,remark,nick_name FROM contact WHERE remark='%s' OR nick_name='%s' LIMIT 3" % (esc, esc),
    )
    for r in rows:
        if r.get("username"):
            return str(r["username"])
    return ""


def _wait_stats(timeout=20):
    for _ in range(max(1, int(timeout * 2))):
        time.sleep(0.5)
        try:
            st = _post("/RecvStats", {}, 10)
        except Exception:
            return {}
        if st.get("runs", 0) > 0:
            return st
    try:
        return _post("/RecvStats", {}, 10)
    except Exception:
        return {}


def _wait_repl(timeout=6.0):
    """等待 hook 回报“对方消息已收到”（repl>0）；只用于观测，不作为发不发的依据。

    注意：不能因为 repl 没及时到就重发——/SendTextTo 返回 ret=0 时消息已经在微信里了，
    重发会让对方收到两条完全相同的消息（2026-10-26 15:56 的实际事故）。
    """
    deadline = time.time() + max(0.5, float(timeout))
    last = {}
    while time.time() < deadline:
        time.sleep(0.4)
        try:
            st = _post("/RecvStats", {}, 10)
        except Exception:
            return last
        last = st or {}
        try:
            if int(last.get("repl") or 0) > 0:
                return last
        except Exception:
            pass
    return last


#: 最近一次发送的诊断信息（写入 data/send_path.log，方便排查）
LAST_DETAIL = {"text": "", "image": ""}


def last_detail(kind: str = "text") -> str:
    try:
        return str(LAST_DETAIL.get(kind) or "")
    except Exception:
        return ""


def _donor_pair(target_wxid):
    chat = current_chat()
    if not chat:
        return "", ""
    donor = resolve_wxid(chat)
    if not donor:
        # 当前会话可能是一条目标会话；允许 donor==target（微信按正常发送）
        donor = target_wxid
    return chat, donor


def direct_send_text(target_wxid: str, text: str, timeout: float = 30.0) -> bool:
    """直发文字。

    重要：/SendTextTo 返回 ret=0 = 已成功入队/发送，**绝不再重发**（旧实现等不到
    RecvStats.repl 就重试，曾导致对方连收两条一模一样的消息）。
    只有 ret!=0（接口明确失败）或抛异常才重试一次；repl 仅用于日志观测。
    """
    if not target_wxid or not text:
        return False
    chat, donor = _donor_pair(target_wxid)
    if not chat or not donor:
        LAST_DETAIL["text"] = "no chat/donor"
        return False
    errors = []
    for attempt in range(2):
        try:
            arm = _post("/ArmRecv", {"mode": 11, "from": donor, "to": target_wxid}, 10)
            if arm.get("ret") != 0:
                errors.append("arm_ret=%r" % (arm.get("ret"),))
                time.sleep(1.0)
                continue
            r = _post("/SendTextTo", {"chat": chat, "text": text}, timeout)
            try:
                ret = int(r.get("ret") or 0)
            except Exception:
                ret = -1
            if ret == 0:
                # 已发送：等一会儿回执仅用于观测，不再重发
                st = _wait_repl(6.0)
                LAST_DETAIL["text"] = "ret=0 runs=%r repl=%r" % (
                    st.get("runs"), st.get("repl"))
                return True
            errors.append("send_ret=%r" % ret)
        except Exception as exc:
            errors.append("exc=%r" % (exc,))
        time.sleep(1.0)
    LAST_DETAIL["text"] = "failed: " + "; ".join(errors[-3:])
    return False


def direct_send_image(target_wxid: str, path: str, timeout: float = 90.0,
                      route: str = "/SendImageVirtual2") -> bool:
    """直发表情/图片：同样以 ret==0 为成功，避免重发造成重复。"""
    if not target_wxid or not path:
        return False
    chat, donor = _donor_pair(target_wxid)
    if not chat or not donor:
        LAST_DETAIL["image"] = "no chat/donor"
        return False
    errors = []
    for attempt in range(2):
        try:
            arm = _post("/ArmRecv", {"mode": 11, "from": donor, "to": target_wxid}, 10)
            if arm.get("ret") != 0:
                errors.append("arm_ret=%r" % (arm.get("ret"),))
                time.sleep(1.0)
                continue
            r = _post(route, {"chat": chat, "path": path}, timeout)
            try:
                ret = int(r.get("ret") or 0)
            except Exception:
                ret = -1
            if ret == 0:
                st = _wait_repl(6.0)
                LAST_DETAIL["image"] = "ret=0 route=%s runs=%r repl=%r" % (
                    route, st.get("runs"), st.get("repl"))
                return True
            errors.append("send_ret=%r" % ret)
        except Exception as exc:
            errors.append("exc=%r" % (exc,))
        time.sleep(1.0)
    LAST_DETAIL["image"] = "failed: " + "; ".join(errors[-3:])
    return False


def direct_available(timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(urllib.request.Request(HOOK_API + "/Version", data=b"{}",
                                                           headers={"Content-Type": "application/json"},
                                                           method="POST"), timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False
