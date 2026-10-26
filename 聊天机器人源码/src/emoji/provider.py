# -*- coding: utf-8 -*-
"""表情包提供器：emoji 字符 + 本地图片库 + 平台原生表情（QQ CQ face / 微信表情）。"""
from __future__ import annotations

import json
import os
from typing import Optional

from ..core.interfaces import EmojiProvider
from ..core.models import AppConfig, Platform
from ..core.paths import project_root

# 关键词 -> emoji 字符（按对话语境选择）
_KEYWORD_EMOJI = {
    "开心": "😄", "高兴": "😄", "哈哈": "😂", "笑": "😄", "好笑": "😂",
    "难过": "😢", "伤心": "😢", "哭": "😭", "生气": "😠", "愤怒": "😡",
    "谢谢": "🙏", "感谢": "🙏", "棒": "👍", "厉害": "👍", "赞": "👍",
    "好的": "👌", "可以": "👌", "没问题": "👌", "再见": "👋", "你好": "👋",
    "抱歉": "😅", "对不起": "😅", "疑问": "🤔", "为什么": "🤔", "惊讶": "😮",
    "震惊": "😮", "爱": "❤️", "喜欢": "❤️", "加油": "💪", "努力": "💪",
    "晚安": "🌙", "早安": "☀️", "好吃": "😋", "恭喜": "🎉",
}

# 关键词 -> QQ 小黄脸 face id（发送 [CQ:face,id=xx]）
_QQ_FACE = {
    "开心": 14, "高兴": 14, "哈哈": 13, "笑": 13, "好笑": 13,
    "难过": 5, "伤心": 5, "哭": 96, "生气": 6, "愤怒": 6,
    "谢谢": 178, "感谢": 178, "棒": 76, "厉害": 76, "赞": 76,
    "好的": 53, "可以": 53, "没问题": 53, "再见": 39, "你好": 2,
    "抱歉": 107, "对不起": 107, "疑问": 32, "为什么": 32, "惊讶": 0,
    "爱": 66, "喜欢": 66, "加油": 74, "努力": 74, "晚安": 175,
    "恭喜": 173, "好吃": 76,
}

# 关键词 -> 微信自带表情文字（微信会渲染成表情）
_WECHAT_EMOJI = {
    "开心": "[微笑]", "高兴": "[微笑]", "哈哈": "[呲牙]", "笑": "[呲牙]", "好笑": "[呲牙]",
    "难过": "[流泪]", "伤心": "[流泪]", "哭": "[大哭]", "生气": "[发怒]", "愤怒": "[发怒]",
    "谢谢": "[抱拳]", "感谢": "[抱拳]", "棒": "[强]", "厉害": "[强]", "赞": "[强]",
    "好的": "[OK]", "可以": "[OK]", "没问题": "[OK]", "再见": "[再见]", "你好": "[微笑]",
    "抱歉": "[尴尬]", "对不起": "[尴尬]", "疑问": "[疑问]", "为什么": "[疑问]", "惊讶": "[惊讶]",
    "爱": "[爱心]", "喜欢": "[爱心]", "加油": "[加油]", "努力": "[加油]", "晚安": "[月亮]",
    "恭喜": "[庆祝]", "好吃": "[色]",
}


class EmojiProviderImpl(EmojiProvider):
    """按语境选择 emoji 字符 / 平台原生表情，并从本地图片库匹配贴图。

    微信平台优先使用「微信收藏表情包」目录（src/emoji/wechat_fav.py 维护的
    assets/emoji/wechat_fav/catalog.json）：由 AI 阅读表情描述后挑选，完全不依赖 GUI。
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.images_dir = self._resolve_images_dir()
        self._fav = None
        self._fav_dir = None
        self._fav_lock = __import__("threading").Lock()

    def _resolve_images_dir(self) -> str:
        # 优先：当前微信账号的账号级表情库（<数据根>/EMOJE/<wxid>）
        try:
            from ..integration.account_store import active_emoji_images_dir
            acct = active_emoji_images_dir()
            if acct:
                return acct
        except Exception:
            pass
        from ..core.paths import resolve_path
        return resolve_path(self.config.emoji_images_dir, "assets/emoji")

    def _fav_provider(self):
        """懒加载微信收藏表情包目录（无 catalog 时返回 None）；账号目录变化时自动重载。"""
        images_dir = self._resolve_images_dir()
        if self._fav is not None and self._fav_dir == images_dir:
            return self._fav
        with self._fav_lock:
            if self._fav is not None and self._fav_dir == images_dir:
                return self._fav
            try:
                from .wechat_fav import WeChatFavStickers

                fav = WeChatFavStickers(images_dir, self.config)
                self._fav = fav if fav.load() else False
                self._fav_dir = images_dir
            except Exception:
                self._fav = False
                self._fav_dir = images_dir
        return self._fav or None

    def pick_emoji(self, text: str) -> str:
        if not text:
            return ""
        for kw, emo in _KEYWORD_EMOJI.items():
            if kw in text:
                return emo
        return ""

    def _pick_chat_sticker(self, text: str) -> Optional[str]:
        """从手动扫描得到的 chat_stickers 本地库里挑选。"""
        try:
            base = self._resolve_images_dir()
            path = os.path.join(base, "chat_stickers", "catalog.json")
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            from ..core.paths import resolve_stored_file
            sub = os.path.join(base, "chat_stickers")
            items = []
            for it in data.get("items") or []:
                f = str(it.get("file") or "")
                if not f:
                    continue
                f = resolve_stored_file(f, [sub, base])
                if f and os.path.isfile(f):
                    it = dict(it)
                    it["file"] = f
                    items.append(it)
            if not items:
                return None
            low = (text or "").casefold()
            for it in items:
                for tag in it.get("tags") or []:
                    if str(tag).casefold() in low:
                        return it["file"]
                desc = str(it.get("desc") or "").casefold()
                if desc and any(desc[i:i + 2] in low for i in range(max(0, len(desc) - 1))):
                    return it["file"]
            request_words = ("表情", "图片", "贴图", "斗图", "发图", "发个图", "表情包")
            if any(w in (text or "") for w in request_words):
                return items[sum(ord(c) for c in (text or "")) % len(items)]["file"]
        except Exception:
            pass
        return None

    def pick_image(self, text: str, platform: Optional[Platform] = None) -> Optional[str]:
        """按语境选择贴图：微信优先收藏表情包（AI 挑选），否则本地图片库关键词匹配。"""
        if platform == Platform.WECHAT:
            fav = self._fav_provider()
            if fav is not None:
                try:
                    fav.load(force=True)  # 人工改过描述/标签后立即生效
                    path = fav.pick(text or "")
                    if path and os.path.exists(path):
                        return path
                except Exception:
                    pass
            local = self._pick_chat_sticker(text or "")
            if local:
                return local
        images_dir = self._resolve_images_dir()  # 实时解析，支持界面改目录后即时生效
        if not text or not os.path.isdir(images_dir):
            return None
        exts = (".png", ".jpg", ".jpeg", ".gif")
        try:
            files = [f for f in os.listdir(images_dir) if f.lower().endswith(exts)]
        except OSError:
            return None
        if not files:
            return None
        for f in sorted(files):
            stem = os.path.splitext(f)[0]
            if stem and stem in text:
                return os.path.join(images_dir, f)
        return None

    # ---------------- 自主发表情包（AI 标记驱动） ----------------
    def all_sticker_tags(self, platform: Platform):
        if platform != Platform.WECHAT:
            return []
        fav = self._fav_provider()
        if fav is None:
            return []
        try:
            return fav.all_tags()
        except Exception:
            return []

    def pick_sticker_for_intent(self, platform: Platform, intent: str, keywords=None,
                                tags=None, text: str = "", exclude_md5=None):
        """按“标签→候选→AI选图”挑一张收藏表情；返回 dict(path/md5/tags/desc/ext/is_gif4f) 或 None。"""
        if platform != Platform.WECHAT:
            return None
        fav = self._fav_provider()
        if fav is None:
            return None
        try:
            fav.load(force=True)
            tags = [str(t) for t in (tags or []) if str(t).strip()]
            if not tags:
                tags = [str(intent)] if intent else []
                tags += [str(k) for k in (keywords or [])[:4]]
            ctx = " ".join([str(intent or "")] + [str(k) for k in (keywords or [])] + [str(text or "")])
            path = fav.pick_by_tags(ctx, tags, exclude_md5=exclude_md5)
            if not path or not os.path.isfile(path):
                return None
            info = {"path": path, "md5": os.path.splitext(os.path.basename(path))[0].lower(),
                    "ext": os.path.splitext(path)[1].lower(),
                    "is_gif4f": os.path.splitext(path)[1].lower() in (".gif", ".webp"),
                    "tags": tags, "desc": ""}
            for it in fav.load():
                if it.get("file") == path:
                    info["md5"] = str(it.get("id") or info["md5"]).lower()
                    info["tags"] = it.get("tags") or tags
                    info["desc"] = it.get("desc") or ""
                    info["ext"] = it.get("ext") or info["ext"]
                    break
            return info
        except Exception:
            return None

    def pick_sticker(self, platform: Platform, text: str) -> str:
        """返回平台原生表情：QQ 用 CQ face、微信用表情文字；无匹配返回空串。"""
        if not text:
            return ""
        if platform == Platform.QQ:
            for kw, fid in _QQ_FACE.items():
                if kw in text:
                    return f"[CQ:face,id={fid}]"
            return ""
        for kw, emo in _WECHAT_EMOJI.items():
            if kw in text:
                return emo
        return ""
