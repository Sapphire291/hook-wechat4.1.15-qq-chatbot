# -*- coding: utf-8 -*-
"""QQ 接入适配器：基于 OneBot v11 正向 WebSocket 协议（连接外部兼容实现）。

依赖：websocket-client（懒导入；未安装时 available() 返回 False）。
连接地址来自 AppConfig.qq_onebot_ws_url，默认 ws://127.0.0.1:3001。

协议要点（OneBot v11 正向 WebSocket）：
  - 客户端主动连接服务端，服务端推送事件（JSON）；
  - 消息事件：post_type=message，message_type=group/private，含
    group_id / user_id、sender(user_id/nickname/card)、message/raw_message、message_id、self_id；
  - API 调用：发送 {"action", "params", "echo"}，服务端按 echo 返回结果。

断线后由后台线程自动重连。
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from ..core.interfaces import ChatAdapter
from ..core.models import AppConfig, Message, MessageType, Platform


class QQAdapter(ChatAdapter):
    """OneBot v11 正向 WebSocket 接入适配器。"""

    platform = Platform.QQ

    def __init__(self, config: AppConfig):
        super().__init__(config)
        self._ws = None                       # WebSocketApp 实例
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()         # 保护 _running / _thread / _ws 赋值
        self._send_lock = threading.Lock()    # 串行化 WebSocket 发送
        self._pending_lock = threading.Lock() # 保护 _pending
        self._pending: Dict[str, Dict[str, Any]] = {}  # echo -> {event, result}
        self._self_id = ""                    # 机器人自身 QQ（用于 @ 判定）
        self._chat_kind: Dict[str, bool] = {} # chat_id -> 是否群聊（回复时选择 action）
        self.last_image_message_id = ""        # 最近一条图片消息的 message_id（用于收藏）

    # ------------------------------------------------------------------ #
    # 依赖检测
    # ------------------------------------------------------------------ #
    def available(self) -> bool:
        """检测 websocket-client 是否安装且 OneBot 服务是否可达。"""
        try:
            import websocket  # noqa: F401
        except Exception:
            return False
        return self._probe_host()

    def _probe_host(self, timeout: float = 2.0) -> bool:
        """轻量探测 OneBot 服务是否监听（短超时，失败返回 False）。"""
        try:
            u = urlparse(self.config.qq_onebot_ws_url)
            host = u.hostname or "127.0.0.1"
            port = u.port or (443 if u.scheme == "wss" else 80)
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        """连接 OneBot 服务并启动接收线程；依赖不可用时返回 False。"""
        if not self.available():
            return False
        with self._lock:
            if self._running:
                return True
            self._running = True
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, name="qq-adapter", daemon=True
            )
            self._thread.start()
        return True

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
        self._stop_event.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 连接主循环（含自动重连）
    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                import websocket

                ws = websocket.WebSocketApp(
                    self.config.qq_onebot_ws_url,
                    on_open=self._on_ws_open,
                    on_message=self._on_ws_message,
                    on_error=self._on_ws_error,
                    on_close=self._on_ws_close,
                )
                self._ws = ws
                # run_forever 阻塞直到断开/出错
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception:
                pass
            finally:
                self._ws = None
            # 断线重连：等待一小段再尝试
            if self._stop_event.wait(timeout=5.0):
                break

    # ------------------------------------------------------------------ #
    # WebSocket 回调
    # ------------------------------------------------------------------ #
    def _on_ws_open(self, ws) -> None:
        # 异步获取自身 QQ，便于 @ 判定（避免阻塞接收循环）
        try:
            threading.Thread(target=self._fetch_self_id, daemon=True).start()
        except Exception:
            pass

    def _on_ws_message(self, ws, message) -> None:
        try:
            data = json.loads(message)
        except Exception:
            return
        if not isinstance(data, dict):
            return

        echo = data.get("echo")
        if echo is not None:
            self._resolve_pending(str(echo), data)
            return

        if data.get("post_type") == "message":
            sid = data.get("self_id")
            if sid is not None:
                self._self_id = str(sid)
            msg = self._to_message(data)
            if msg is not None:
                self._remember_chat_kind(msg)
                self._dispatch(msg)

    def _on_ws_error(self, ws, error) -> None:
        # 错误不抛出，由 _run 负责重连
        pass

    def _on_ws_close(self, ws, close_status_code, close_msg) -> None:
        pass

    # ------------------------------------------------------------------ #
    # API 调用（带 echo 等待返回）
    # ------------------------------------------------------------------ #
    def _call_api(self, action: str, params: dict, timeout: float = 10.0):
        ws = self._ws
        if ws is None:
            return None
        echo = uuid.uuid4().hex
        holder = {"event": threading.Event(), "result": None}
        with self._pending_lock:
            self._pending[echo] = holder

        payload = json.dumps(
            {"action": action, "params": params, "echo": echo}, ensure_ascii=False
        )
        try:
            with self._send_lock:
                ws.send(payload)
        except Exception:
            self._pop_pending(echo)
            return None

        if holder["event"].wait(timeout):
            return self._pop_pending(echo)
        self._pop_pending(echo)
        return None

    def _resolve_pending(self, echo: str, data: dict) -> None:
        holder = self._pop_pending(echo)
        if holder is not None:
            holder["result"] = data
            holder["event"].set()

    def _pop_pending(self, echo: str):
        with self._pending_lock:
            return self._pending.pop(echo, None)

    def _fetch_self_id(self) -> None:
        resp = self._call_api("get_login_info", {}, timeout=5)
        try:
            if resp and resp.get("retcode") == 0:
                data = resp.get("data") or {}
                self._self_id = str(data.get("user_id", "") or "")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 事件 -> Message
    # ------------------------------------------------------------------ #
    def _to_message(self, data: dict) -> Optional[Message]:
        try:
            message_type = str(data.get("message_type", "")).lower()
            if message_type == "group":
                chat_id = str(data.get("group_id", ""))
                is_group = True
                chat_name = ""
            elif message_type == "private":
                chat_id = str(data.get("user_id", ""))
                is_group = False
                chat_name = ""
            else:
                return None

            if not chat_id:
                return None

            sender = data.get("sender") or {}
            sender_id = str(sender.get("user_id", "") or data.get("user_id", ""))
            sender_name = str(sender.get("card") or sender.get("nickname") or sender_id)
            # 私聊会话名即对方昵称
            if not is_group:
                chat_name = sender_name

            content, mentions_me, mtype, image_paths = self._extract_content(data)

            ts = data.get("time")
            try:
                timestamp = float(ts) if ts is not None else time.time()
            except Exception:
                timestamp = time.time()

            if mtype == MessageType.IMAGE:
                self.last_image_message_id = str(data.get("message_id", "") or "")

            return Message(
                platform=Platform.QQ,
                chat_id=chat_id,
                chat_name=chat_name,
                is_group=is_group,
                sender_id=sender_id,
                sender_name=sender_name,
                content=content,
                mentions_me=mentions_me,
                timestamp=timestamp,
                message_type=mtype,
                image_paths=image_paths,
            )
        except Exception:
            return None

    def _extract_content(self, data: dict) -> Tuple[str, bool, MessageType, List[str]]:
        """提取文本、@ 判定、消息类型与已下载图片路径（兼容 message 为 list 或字符串）。"""
        mentions_me = False
        has_image = False
        image_paths: List[str] = []
        parts: List[str] = []
        message = data.get("message")
        self_id = self._self_id

        if isinstance(message, list):
            for seg in message:
                if not isinstance(seg, dict):
                    continue
                seg_type = str(seg.get("type", ""))
                seg_data = seg.get("data") or {}
                if seg_type == "text":
                    parts.append(str(seg_data.get("text", "")))
                elif seg_type == "at":
                    qq = str(seg_data.get("qq", ""))
                    if self_id and qq == self_id:
                        mentions_me = True
                    name = str(seg_data.get("name", "") or "")
                    parts.append(name or f"@{qq}")
                elif seg_type == "image":
                    has_image = True
                    p = self._save_image(seg_data)
                    if p:
                        image_paths.append(p)
                elif seg_type == "face":
                    parts.append(str(seg_data.get("name", "") or ""))
                else:
                    txt = seg_data.get("text") or seg_data.get("title") or seg_data.get("name")
                    if txt:
                        parts.append(str(txt))
        else:
            raw = data.get("raw_message")
            if raw is not None:
                parts.append(str(raw))

        raw_message = str(data.get("raw_message") or "")
        if not mentions_me and self_id and f"[CQ:at,qq={self_id}]" in raw_message:
            mentions_me = True

        text = "".join(parts).strip()

        if has_image:
            if not text:
                text = "[图片]"
            mtype = MessageType.IMAGE
        else:
            mtype = MessageType.TEXT if text else MessageType.OTHER

        return text, mentions_me, mtype, image_paths

    def _save_image(self, seg_data: dict) -> str:
        """直接从图片缓存路径读取（OneBot 实现已缓存到本地），不下载、不删除。"""
        try:
            for key in ("path", "file"):
                p = seg_data.get(key)
                if p and os.path.isfile(str(p)):
                    return str(p)
            return ""
        except Exception:
            return ""

    def _remember_chat_kind(self, msg: Message) -> None:
        try:
            self._chat_kind[str(msg.chat_id)] = bool(msg.is_group)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 发送
    # ------------------------------------------------------------------ #
    def send_text(self, chat_id: str, text: str) -> bool:
        if not text:
            return False
        try:
            return self._send_by_kind(chat_id, str(text))
        except Exception:
            return False

    def send_image(self, chat_id: str, image_path: str) -> bool:
        """通过 CQ 码 [CQ:image,file=...] 发送本地图片。"""
        if not image_path or not os.path.exists(image_path):
            return False
        try:
            cq = f"[CQ:image,file={image_path}]"
            return self._send_by_kind(chat_id, cq)
        except Exception:
            return False

    def _send_by_kind(self, chat_id: str, message: str) -> bool:
        """根据会话类型选择 send_group_msg / send_private_msg；失败落日志、明确失败重试一次。"""
        chat_id = str(chat_id)
        is_group = self._chat_kind.get(chat_id, True)  # 未知会话默认按群处理
        if is_group:
            action = "send_group_msg"
            params = {"group_id": self._as_number(chat_id), "message": message}
        else:
            action = "send_private_msg"
            params = {"user_id": self._as_number(chat_id), "message": message}
        for attempt in range(2):
            resp = self._call_api(action, params, timeout=10)
            if self._resp_ok(resp):
                if attempt > 0:
                    self._log_send("ok_after_retry", action, chat_id, resp)
                return True
            # 超时/无响应（resp=None）：不确定是否已发，宁可不重试，避免对方收到两条
            if resp is None:
                self._log_send("no_response", action, chat_id, None)
                return False
            self._log_send("failed_attempt%d" % (attempt + 1), action, chat_id, resp)
            if attempt == 0:
                time.sleep(0.6)
        return False

    @staticmethod
    def _log_send(tag: str, action: str, chat_id: str, resp) -> None:
        """QQ 发送结果写 data/qq_send.log，排查“QQ 没回复”时有据可查。"""
        try:
            import json as _json
            from ..core.paths import data_dir
            p = os.path.join(data_dir(), "qq_send.log")
            line = "%s %s action=%s chat=%s resp=%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), tag, action, chat_id,
                _json.dumps(resp, ensure_ascii=False)[:300] if resp is not None else "None")
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    @staticmethod
    def _as_number(value: str):
        try:
            return int(value)
        except Exception:
            return value

    @staticmethod
    def _resp_ok(resp) -> bool:
        if not isinstance(resp, dict):
            return False
        try:
            return resp.get("status") == "ok" and int(resp.get("retcode", -1)) == 0
        except Exception:
            return False


def fetch_qq_chats(ws_url: str) -> List[Tuple[str, str, bool]]:
    """独立连接 OneBot 拉取好友与群列表，返回 [(chat_id, 名称, 是否群聊)]。

    供设置界面的「从列表选择」使用，无需机器人已启动。
    """
    chats: List[Tuple[str, str, bool]] = []
    try:
        import websocket
        ws = websocket.create_connection(ws_url, timeout=8)
    except Exception:
        return chats
    try:
        for action, is_group in (("get_friend_list", False), ("get_group_list", True)):
            echo = uuid.uuid4().hex
            try:
                ws.send(json.dumps({"action": action, "params": {}, "echo": echo}))
                deadline = time.time() + 6
                while time.time() < deadline:
                    data = json.loads(ws.recv())
                    if data.get("echo") != echo:
                        continue
                    if data.get("retcode") == 0:
                        for item in (data.get("data") or []):
                            if is_group:
                                cid = str(item.get("group_id", ""))
                                name = item.get("group_name", "") or cid
                            else:
                                cid = str(item.get("user_id", ""))
                                name = item.get("remark") or item.get("nickname", "") or cid
                            if cid:
                                chats.append((cid, name, is_group))
                    break
            except Exception:
                pass
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return chats


def fetch_custom_faces(ws_url: str) -> List[dict]:
    """拉取 QQ 收藏表情（云端），返回原始条目列表 [{...}]。"""
    faces: List[dict] = []
    try:
        import websocket
        ws = websocket.create_connection(ws_url, timeout=8)
    except Exception:
        return faces
    try:
        echo = uuid.uuid4().hex
        ws.send(json.dumps({"action": "fetch_custom_face", "params": {}, "echo": echo}))
        deadline = time.time() + 6
        while time.time() < deadline:
            data = json.loads(ws.recv())
            if data.get("echo") != echo:
                continue
            if data.get("retcode") == 0:
                for item in (data.get("data") or []):
                    if isinstance(item, dict):
                        faces.append(item)
            break
    except Exception:
        pass
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return faces


def mark_favorite(ws_url: str, message_id: str) -> bool:
    """收藏一条消息（表情/图片），返回是否成功。"""
    if not message_id:
        return False
    try:
        import websocket
        ws = websocket.create_connection(ws_url, timeout=8)
    except Exception:
        return False
    try:
        echo = uuid.uuid4().hex
        ws.send(json.dumps({
            "action": "mark_msg_as_favorite",
            "params": {"message_id": int(message_id)},
            "echo": echo,
        }))
        deadline = time.time() + 6
        while time.time() < deadline:
            data = json.loads(ws.recv())
            if data.get("echo") == echo:
                return data.get("retcode") == 0
        return False
    except Exception:
        return False
    finally:
        try:
            ws.close()
        except Exception:
            pass
