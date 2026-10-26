# -*- coding: utf-8 -*-
"""表情包「重新扫描 + 重新标注」：用聊天主 AI（视觉模型）重扫本地全部表情。

覆盖两个本地库（当前微信账号）：
- `EMOJE/<wxid>/wechat_fav/`      微信真实收藏表情（catalog.json）
- `EMOJE/<wxid>/chat_stickers/`   扫描/自动保存的聊天表情（catalog.json）

说明：
- 用的是与聊天同一套 AI 配置（`config.ai_for(Platform.WECHAT)`，含图片输入）；
- force=True 时连已有描述也覆盖重标；失败的单张保留旧描述；
- 支持进度回调，供设置界面显示 x/y。
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from ..core.paths import resolve_path
from ..integration.chat_stickers import describe_chat_stickers
from .wechat_fav import WeChatFavStickers


def _images_dir(config, images_dir: Optional[str] = None) -> str:
    """解析当前账号表情库目录：显式指定 > 当前账号 EMOJE\\<wxid> > 配置目录。"""
    if images_dir:
        return str(images_dir)
    try:
        from ..integration.account_store import active_emoji_images_dir
        base = active_emoji_images_dir()
        if base:
            return base
    except Exception:
        pass
    return resolve_path(getattr(config, "emoji_images_dir", ""), "assets/emoji")


def local_sticker_stats(images_dir: Optional[str] = None, config=None) -> dict:
    """统计本地可重扫的表情数量：{"fav": 收藏数, "chat": 聊天表情数, "total": 合计}。"""
    import json
    base = _images_dir(config, images_dir)
    fav = chat = 0
    for sub, key in (("wechat_fav", "fav"), ("chat_stickers", "chat")):
        path = os.path.join(base, sub, "catalog.json")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        items = [x for x in (data.get("items") or []) if isinstance(x, dict)]
        if key == "fav":
            fav = sum(1 for x in items if x.get("file"))
        else:
            chat = sum(1 for x in items if x.get("file"))
    return {"fav": fav, "chat": chat, "total": fav + chat, "dir": base}


def relabel_all_stickers(config, images_dir: Optional[str] = None, workers: int = 3,
                         progress: Optional[Callable[[int, int, str, int], None]] = None,
                         vision=None, rescan_chat: bool = True) -> dict:
    """重新扫描 + 重新标注本地全部表情（收藏 + 聊天表情）。

    流程：①（可选）先扫描最近聊天记录，把新表情下载到 chat_stickers；
          ② 用聊天主 AI 视觉模型重新识别/标注 wechat_fav + chat_stickers 全部表情。
    返回 {"wechat_fav": {...}, "chat_stickers": {...}, "scan": {...}}。
    progress(done,total,phase,ok)。
    """
    base = _images_dir(config, images_dir)
    scan_res = {}
    if rescan_chat:
        if progress:
            try:
                progress(0, 0, "扫描聊天记录", 0)
            except Exception:
                pass
        try:
            from ..integration.chat_stickers import scan_chat_stickers
            scan_res = scan_chat_stickers(session_limit=5, msg_limit=120) or {}
        except Exception as exc:
            scan_res = {"ret": -99, "error": repr(exc)[:200]}
    fav = WeChatFavStickers(base, config)
    if vision is None:
        vision = fav._vision_ask
    else:
        fav._vision_ask = vision

    def p_fav(done, total, ok):
        if progress:
            try:
                progress(done, total, "收藏表情", ok)
            except Exception:
                pass

    fav_res = fav.describe(force=True, workers=workers, progress=p_fav)

    def p_chat(done, total, ok):
        if progress:
            try:
                progress(done, total, "聊天表情", ok)
            except Exception:
                pass

    chat_res = describe_chat_stickers(base, config=config, force=True, workers=workers,
                                      vision=vision, progress=p_chat)
    return {"wechat_fav": fav_res, "chat_stickers": chat_res, "scan": scan_res, "dir": base}
