# -*- coding: utf-8 -*-
"""微信「收藏表情包」：非 GUI 拉取 → CDN 明文下载 → AI 描述 → AI 选用。

流程（全部不依赖窗口/鼠标/键盘/OCR）：
1. 读本地 SQLCipher 库 ``db_storage/emoticon/emoticon.db``（wechatauto 解密）：
   - ``kFavEmoticonOrderTable``   收藏顺序（md5 列表）
   - ``kNonStoreEmoticonTable``   每个表情的 cdn_url（腾讯 CDN 上的**明文原图**）
   - ``kStoreEmoticonCaptionsTable`` / ``kStoreEmoticonPackageTable`` 官方描述/包名
2. 从 cdn_url 下载明文图片，校验 md5 == 收藏 id，落到 ``<images_dir>/wechat_fav/``。
3. 调 DeepSeek 多模态（deepseek-flash 支持图片输入）给每张图生成一句描述 + 情绪标签，
   写入 ``catalog.json``（一次性，增量更新）。
4. 回复时把候选清单交给 AI（纯文本）挑选，返回本地图片路径，由
   ``WeChatAdapter.send_image`` 走 hook ``/SendImageTo`` 发出（内部剪贴板+按键，不点屏幕）。

CLI（在聊天机器人源码根目录运行）：
    python -m src.emoji.wechat_fav refresh --limit 50
    python -m src.emoji.wechat_fav describe --limit 50
    python -m src.emoji.wechat_fav stats
    python -m src.emoji.wechat_fav pick 今天好累啊
"""
from __future__ import annotations

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

HOOK_API = "http://127.0.0.1:30001"

_FAV_SUBDIR = "wechat_fav"
_IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

_DESC_SYSTEM = (
    "你是表情包理解助手。用户会给你一张微信聊天表情包图片，请只输出 JSON："
    '{"desc": "一句话（不超过20字）说明它表达的情绪/动作/含义", '
    '"tags": ["2-4个中文情绪或场景标签"]}'
)

_PICK_SYSTEM = (
    "你是聊天助手，需要从候选表情包中挑选最贴合当前聊天语境的一个。"
    "只输出 JSON：{\"id\": \"候选编号\"}；如果没有合适的，输出 {\"id\": \"\"}。"
    "不要输出其它内容。"
)


def _md5_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _guess_ext(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:4] == b"GIF8":
        return ".gif"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".bin"


class WeChatFavStickers:
    """微信收藏表情包目录（拉取/描述/选用）。线程安全：内部用一把锁。"""

    def __init__(self, images_dir: str, config=None, platform: Platform = Platform.WECHAT):
        self.images_dir = images_dir
        self.fav_dir = os.path.join(images_dir, _FAV_SUBDIR)
        self.catalog_path = os.path.join(self.fav_dir, "catalog.json")
        self.config = config
        self.platform = platform
        self._lock = threading.Lock()
        self._items: Optional[List[dict]] = None
        self._last_pick_ts = 0.0

    # ---------------- 基础 ----------------
    def _ensure_dir(self) -> None:
        os.makedirs(self.fav_dir, exist_ok=True)

    def load(self, force: bool = False) -> List[dict]:
        with self._lock:
            if self._items is not None and not force:
                return self._items
            items: List[dict] = []
            try:
                with open(self.catalog_path, encoding="utf-8") as f:
                    data = json.load(f)
                items = [x for x in (data.get("items") or []) if x.get("file")]
                # 换盘符后旧绝对路径失效：按文件名在当前表情目录里重新定位
                from ..core.paths import resolve_stored_file
                for x in items:
                    f = x.get("file") or ""
                    if f and not os.path.isfile(f):
                        x["file"] = resolve_stored_file(f, [self.fav_dir, self.images_dir])
            except Exception:
                items = []
            self._items = items
            return items

    def _save(self, items: List[dict]) -> None:
        self._ensure_dir()
        data = {"updated_at": int(time.time()), "count": len(items), "items": items}
        tmp = self.catalog_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.catalog_path)
        self._items = items

    # ---------------- 拉取 ----------------
    def _hook_post(self, path: str, payload: dict, timeout: float = 120.0):
        req = urllib.request.Request(
            HOOK_API + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _list_via_hook(self) -> Optional[List[dict]]:
        """优先走 hook 进程内 DB（无需解密密钥）：/Emoji/List。"""
        try:
            out = self._hook_post("/Emoji/List", {})
        except Exception:
            return None
        if not out or out.get("ret") != 0:
            return None
        items = []
        for it in out.get("items") or []:
            items.append(
                {
                    "id": str(it.get("id") or ""),
                    "type": str(it.get("type") or ""),
                    "caption": str(it.get("caption") or ""),
                    "store_caption": str(it.get("store_caption") or ""),
                    "package": str(it.get("package") or ""),
                    "cdn_url": str(it.get("cdn_url") or ""),
                    "thumb_url": str(it.get("thumb_url") or ""),
                    "file": "",
                    "ext": "",
                    "desc": "",
                    "tags": [],
                }
            )
        return [x for x in items if x["id"]]

    def _open_db(self):
        """兜底：打开解密后的 emoticon.db（wechatauto 从微信内存取密钥）。"""
        from wechatauto.db import WeChatDB

        db = WeChatDB()
        return db._open("emoticon/emoticon.db")
    def _query_db(self) -> List[dict]:
        hooked = self._list_via_hook()
        if hooked is not None:
            return hooked
        # 兜底：直接读解密后的 emoticon.db（wechatauto）
        conn = self._open_db()
        fav = [str(r[0]) for r in conn.execute("SELECT md5 FROM kFavEmoticonOrderTable").fetchall()]
        nonstore: Dict[str, dict] = {}
        try:
            for r in conn.execute(
                "SELECT md5, type, caption, product_id, cdn_url, thumb_url FROM kNonStoreEmoticonTable"
            ).fetchall():
                nonstore[str(r[0])] = {
                    "type": str(r[1]),
                    "caption": str(r[2] or ""),
                    "product_id": str(r[3] or ""),
                    "cdn_url": str(r[4] or ""),
                    "thumb_url": str(r[5] or ""),
                }
        except Exception:
            pass
        caps: Dict[str, str] = {}
        try:
            for r in conn.execute(
                "SELECT md5_, caption_ FROM kStoreEmoticonCaptionsTable WHERE language_='default'"
            ).fetchall():
                caps[str(r[0])] = str(r[1] or "")
        except Exception:
            pass
        pkgs: Dict[str, str] = {}
        try:
            for r in conn.execute("SELECT package_id_, package_name_ FROM kStoreEmoticonPackageTable").fetchall():
                pkgs[str(r[0])] = str(r[1] or "")
        except Exception:
            pass
        items = []
        for md5 in fav:
            info = nonstore.get(md5, {})
            pid = info.get("product_id", "")
            items.append(
                {
                    "id": md5,
                    "type": info.get("type", ""),
                    "caption": info.get("caption", ""),
                    "store_caption": caps.get(md5, ""),
                    "package": pkgs.get(pid, ""),
                    "cdn_url": info.get("cdn_url", ""),
                    "thumb_url": info.get("thumb_url", ""),
                    "file": "",
                    "ext": "",
                    "desc": "",
                    "tags": [],
                }
            )
        return items

    def _download_one(self, item: dict) -> bool:
        # 优先 hook /Emoji/Fetch（进程内取 cdn_url 并校验 md5）
        try:
            out = self._hook_post("/Emoji/Fetch", {"id": item["id"], "save_dir": self.fav_dir})
            if out and out.get("ret") == 0 and out.get("md5ok"):
                item["file"] = str(out.get("path") or "")
                item["ext"] = str(out.get("ext") or "")
                return bool(item["file"])
        except Exception:
            pass
        url = item.get("cdn_url") or item.get("thumb_url")
        if not url:
            return False
        md5 = item["id"]
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = resp.read()
        except Exception:
            return False
        if not data:
            return False
        if hashlib.md5(data).hexdigest() != md5:
            return False  # 只接受 md5 对齐的明文原图
        ext = _guess_ext(data)
        if ext == ".bin":
            return False
        path = os.path.join(self.fav_dir, md5 + ext)
        if not os.path.exists(path) or os.path.getsize(path) != len(data):
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        item["file"] = path
        item["ext"] = ext
        return True

    def refresh(self, limit: Optional[int] = None, workers: int = 6) -> dict:
        """从微信库读取收藏列表，并下载尚缺的明文原图（md5 校验）。"""
        self._ensure_dir()
        old = {x["id"]: x for x in self.load(force=True)}
        items = self._query_db()
        if limit:
            items = items[:limit]
        todo = [x for x in items]
        done = 0

        def worker(chunk):
            nonlocal done
            for it in chunk:
                prev = old.get(it["id"]) or {}
                local = prev.get("file") or ""
                if local and os.path.exists(local):
                    it["file"] = local
                    it["ext"] = prev.get("ext", os.path.splitext(local)[1])
                else:
                    self._download_one(it)
                it["desc"] = prev.get("desc", "")
                it["tags"] = prev.get("tags", [])
                if it.get("file"):
                    done += 1

        threads = []
        for i in range(max(1, workers)):
            chunk = todo[i::workers]
            t = threading.Thread(target=worker, args=(chunk,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        self._save(items)
        return {"count": len(items), "downloaded": done, "catalog": self.catalog_path}

    # ---------------- AI 描述 ----------------
    def _ai_client(self):
        ai = self.config.ai_for(self.platform) if self.config is not None else None
        key = (getattr(ai, "api_key", "") or "").strip()
        base = (getattr(ai, "base_url", "") or "https://api.deepseek.com").rstrip("/")
        model = (getattr(ai, "model", "") or "deepseek-flash").strip()
        if not key:
            return None
        return key, base, model

    def _vision_ask(self, image_path: str, timeout: int = 90) -> Optional[dict]:
        client = self._ai_client()
        if not client:
            return None
        key, base, model = client
        try:
            raw = open(image_path, "rb").read()
        except OSError:
            return None
        b64 = base64.b64encode(raw).decode()
        ext = os.path.splitext(image_path)[1].lower()
        mime = "image/png" if ext == ".png" else "image/gif" if ext == ".gif" else "image/jpeg"
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": _DESC_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请描述这张表情包，输出 JSON。"},
                        {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, b64)}},
                    ],
                },
            ],
            "max_tokens": 300,
            "stream": False,
        }
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
            text = (msg.get("content") or "").strip()
            if not text:
                text = (msg.get("reasoning_content") or "").strip()
        except Exception:
            return None
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            j = json.loads(m.group(0))
        except Exception:
            return None
        desc = str(j.get("desc") or "").strip()
        tags = [str(t) for t in (j.get("tags") or [])][:5]
        if not desc:
            return None
        return {"desc": desc, "tags": tags}

    def describe(self, limit: Optional[int] = None, workers: int = 3,
                 force: bool = False, progress=None) -> dict:
        """AI 标注收藏表情。force=True 时连已有描述也重新识别（用于“重新扫描表情包”）。"""
        items = self.load(force=True)
        todo = [x for x in items if x.get("file") and (force or not x.get("desc"))]
        if limit:
            todo = todo[:limit]
        if not todo:
            return {"pending": 0, "described": 0, "total": len(items)}
        lock = threading.Lock()
        ok = 0
        counter = {"done": 0}

        def worker(chunk):
            nonlocal ok
            for it in chunk:
                r = self._vision_ask(it["file"])
                with lock:
                    counter["done"] += 1
                    if r:
                        it["desc"] = r["desc"]
                        it["tags"] = r["tags"]
                        ok += 1
                    if counter["done"] % 20 == 0:
                        self._save(items)
                    if progress:
                        try:
                            progress(counter["done"], len(todo), ok)
                        except Exception:
                            pass

        threads = []
        for i in range(max(1, workers)):
            chunk = todo[i::workers]
            t = threading.Thread(target=worker, args=(chunk,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        self._save(items)
        if progress:
            try:
                progress(len(todo), len(todo), ok)
            except Exception:
                pass
        return {"pending": len(todo), "described": ok, "total": len(items)}

    # ---------------- 选用 ----------------
    def _ask_pick(self, text: str, candidates: List[dict]) -> Optional[str]:
        client = self._ai_client()
        if not client or not candidates:
            return None
        key, base, model = client
        lines = []
        for i, it in enumerate(candidates):
            desc = it.get("desc") or it.get("store_caption") or it.get("caption") or it.get("package") or "无描述"
            tags = "/".join(it.get("tags") or [])
            lines.append("%d. %s%s" % (i + 1, desc, (" [" + tags + "]") if tags else ""))
        prompt = "当前聊天上下文：%s\n\n候选表情包：\n%s\n\n请选择最合适的一个。" % (text[:300], "\n".join(lines))
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": _PICK_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 400,
            "stream": False,
        }
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
            # deepseek-flash 是推理模型：最终答案可能在 reasoning_content 里
            text_out = (msg.get("content") or "").strip()
            if not text_out:
                text_out = (msg.get("reasoning_content") or "").strip()
        except Exception:
            return None
        m = re.search(r"\{.*\}", text_out, re.S)
        if not m:
            return None
        j = None
        # 推理模型输出里可能出现多个 {}：取最后一个包含 id 的合法 JSON
        for cand in reversed(re.findall(r"\{[^{}]*\}", text_out)):
            try:
                t = json.loads(cand)
            except Exception:
                continue
            if isinstance(t, dict) and "id" in t:
                j = t
                break
        if j is None:
            try:
                j = json.loads(m.group(0))
            except Exception:
                return None
        idx = str(j.get("id") or "").strip()
        if not idx.isdigit():
            return None
        i = int(idx) - 1
        if 0 <= i < len(candidates):
            return candidates[i].get("file")
        return None

    # ---------------- 标签索引 / 标签筛选 / 增量描述（供“自主发表情”流程） ----------------
    def all_tags(self):
        """catalog 中所有标签（去重排序）。"""
        tags = set()
        for it in self.load():
            for t in it.get("tags") or []:
                t = str(t).strip()
                if t:
                    tags.add(t)
        return sorted(tags)

    def by_tags(self, tags, limit=8, text="", exclude_md5=None):
        """按标签/描述关键词过滤候选（可排除指定 md5，用于“换一张”）。"""
        wanted = [str(t).strip().lower() for t in (tags or []) if str(t).strip()]
        ctx = str(text or "").lower()
        excl = {str(x).lower() for x in (exclude_md5 or []) if str(x).strip()}
        items = [x for x in self.load() if x.get("file") and os.path.isfile(x["file"])
                 and str(x.get("id") or "").lower() not in excl
                 and os.path.splitext(os.path.basename(x["file"]))[0].lower() not in excl]
        scored = []
        for it in items:
            itags = [str(t).lower() for t in (it.get("tags") or [])]
            desc = str(it.get("desc") or "").lower()
            blob = desc + " " + " ".join(itags)
            score = 0
            for w in wanted:
                if any(w == t or w in t or t in w for t in itags):
                    score += 3
                elif w and w in desc:
                    score += 1
            if ctx:
                for w in wanted:
                    if w and w in ctx:
                        score += 1
            if score > 0:
                scored.append((score, int(it.get("use_count") or 0), it))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        return [it for _, _, it in scored[:max(1, int(limit))]]

    def ensure_desc(self, items, limit=3):
        """只对传入候选里缺描述/标签的项做多模态读图（增量、写回 catalog）。"""
        todo = [it for it in (items or []) if it.get("file") and not it.get("desc")][:max(0, int(limit))]
        if not todo:
            return 0
        all_items = self.load()
        done = 0
        for it in todo:
            r = self._vision_ask(it["file"])
            if not r:
                continue
            it["desc"] = r["desc"]
            it["tags"] = r["tags"]
            done += 1
        if done:
            self._save(all_items)
        return done

    def pick_by_tags(self, text, tags, limit=8, exclude_md5=None):
        """标签筛选 → 补描述 → AI 选具体一张；返回本地路径（可排除指定 md5）。"""
        cands = self.by_tags(tags, limit=limit, text=text, exclude_md5=exclude_md5)
        if not cands:
            return None
        self.ensure_desc(cands, limit=3)
        cands = [it for it in cands if it.get("desc")] or cands
        return self._ask_pick(text, cands[:12])

    def pick(self, text: str) -> Optional[str]:
        """根据聊天内容挑一张收藏表情，返回本地图片路径；无合适返回 None。"""
        if not text:
            return None
        items = [x for x in self.load() if x.get("file")]
        if not items:
            return None
        # 限频：同一条回复链路 2s 内不重复请求
        now = time.time()
        if now - self._last_pick_ts < 2.0:
            return None
        self._last_pick_ts = now
        # 先用关键词/标签预筛（减少 token）
        kw = [w for w in re.split(r"[\s，。！？、,.!?~～]+", text) if len(w) >= 2][:12]
        scored = []
        for it in items:
            blob = " ".join([it.get("desc", ""), " ".join(it.get("tags") or []),
                             it.get("store_caption", ""), it.get("caption", ""), it.get("package", "")])
            score = sum(1 for w in kw if w in blob)
            if score:
                scored.append((score, it))
        scored.sort(key=lambda x: -x[0])
        candidates = [it for _, it in scored[:30]]
        if not candidates:
            # 没有命中：随机取一部分带描述的表情交给 AI 判断语境
            with_desc = [x for x in items if x.get("desc")]
            candidates = with_desc[:40] if with_desc else items[:40]
        return self._ask_pick(text, candidates)

    def stats(self) -> dict:
        items = self.load()
        return {
            "total": len(items),
            "downloaded": sum(1 for x in items if x.get("file")),
            "described": sum(1 for x in items if x.get("desc")),
            "catalog": self.catalog_path,
        }


def _cli() -> None:
    import argparse

    from ..core.config import ConfigStore
    from ..core.paths import project_root

    ap = argparse.ArgumentParser(description="微信收藏表情包目录工具")
    ap.add_argument("cmd", choices=["refresh", "describe", "stats", "pick"])
    ap.add_argument("text", nargs="?", default="")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=0)
    args = ap.parse_args()

    from ..core.paths import resolve_path
    cfg = ConfigStore().load()
    d = resolve_path(cfg.emoji_images_dir, "assets/emoji")
    fav = WeChatFavStickers(d, cfg)
    if args.cmd == "refresh":
        print(fav.refresh(limit=args.limit, workers=args.workers or 6))
    elif args.cmd == "describe":
        print(fav.describe(limit=args.limit, workers=args.workers or 3))
    elif args.cmd == "stats":
        print(fav.stats())
    else:
        print(fav.pick(args.text or "今天有点累"))


if __name__ == "__main__":
    _cli()
