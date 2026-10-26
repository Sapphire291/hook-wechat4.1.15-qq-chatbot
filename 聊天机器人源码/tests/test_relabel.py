# -*- coding: utf-8 -*-
"""表情包“重新扫描/重新标注”测试（不联网：用假 vision 替身）。"""
import json
import os
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP_BASE = os.path.join(_REPO_ROOT, ".test_tmp")
os.makedirs(_TMP_BASE, exist_ok=True)


def _mk_tmp(prefix="t_"):
    path = os.path.join(_TMP_BASE, "%s%d_%d" % (prefix, os.getpid(), int(time.time() * 1000) % 1000000))
    try:
        os.mkdir(path)
    except FileExistsError:
        os.makedirs(path, exist_ok=True)
    return path


_TMP_HOME = _mk_tmp("relabel_")
os.environ["CHATBOT_HOME"] = _TMP_HOME

sys.path.insert(0, _REPO_ROOT)

from src.emoji.relabel import local_sticker_stats, relabel_all_stickers  # noqa: E402
from src.emoji.wechat_fav import WeChatFavStickers  # noqa: E402
from src.integration.chat_stickers import describe_chat_stickers  # noqa: E402


def _write_catalog(path, items):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False, indent=1)


def main() -> int:
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(("PASS" if cond else "FAIL") + " - " + name + ((" ｜ " + str(extra)) if (extra and not cond) else ""))
        if not cond:
            ok = False

    base = _mk_tmp("emoje_")
    fav_dir = os.path.join(base, "wechat_fav")
    chat_dir = os.path.join(base, "chat_stickers")
    os.makedirs(fav_dir, exist_ok=True)
    os.makedirs(chat_dir, exist_ok=True)
    # 两个本地库各放 2 张假表情（其中一张已有旧描述，验证 force 会覆盖）
    for i, name in enumerate(("fav_a.png", "fav_b.png")):
        open(os.path.join(fav_dir, name), "wb").write(b"PNG")
    for i, name in enumerate(("chat_a.gif", "chat_b.gif")):
        open(os.path.join(chat_dir, name), "wb").write(b"GIF")
    _write_catalog(os.path.join(fav_dir, "catalog.json"), [
        {"id": "fav_a", "file": os.path.join(fav_dir, "fav_a.png"), "desc": "旧描述A", "tags": ["旧"]},
        {"id": "fav_b", "file": os.path.join(fav_dir, "fav_b.png"), "desc": "", "tags": []},
    ])
    os.makedirs(chat_dir, exist_ok=True)
    with open(os.path.join(chat_dir, "catalog.json"), "w", encoding="utf-8") as f:
        json.dump({"items": [
            {"id": "chat_a", "file": os.path.join(chat_dir, "chat_a.gif"), "desc": "", "tags": []},
            {"id": "chat_b", "file": os.path.join(chat_dir, "chat_b.gif"), "desc": "旧描述B", "tags": []},
        ]}, f, ensure_ascii=False, indent=1)

    calls = []

    def fake_vision(path):
        calls.append(os.path.basename(path))
        return {"desc": "新描述-%s" % os.path.basename(path), "tags": ["开心", "测试"]}

    stats = local_sticker_stats(base)
    check("统计本地表情数量", stats.get("fav") == 2 and stats.get("chat") == 2 and stats.get("total") == 4, stats)

    fav = WeChatFavStickers(base, None)
    fav._vision_ask = fake_vision
    prog = []
    res = fav.describe(force=True, workers=1, progress=lambda d, t, o: prog.append((d, t, o)))
    check("收藏表情 force 重新标注（含已有描述）", res.get("described") == 2 and res.get("pending") == 2, res)
    check("收藏表情写回 catalog", all(
        (it.get("desc") or "").startswith("新描述") for it in json.load(open(os.path.join(fav_dir, "catalog.json"), encoding="utf-8"))["items"]))
    check("收藏表情进度回调", bool(prog) and prog[-1][0] == 2, prog)

    chat_prog = []
    res2 = describe_chat_stickers(base, config=None, force=True, workers=1,
                                  vision=fake_vision,
                                  progress=lambda d, t, o: chat_prog.append((d, t, o)))
    check("聊天表情 force 重新标注", res2.get("described") == 2, res2)
    items2 = json.load(open(os.path.join(chat_dir, "catalog.json"), encoding="utf-8"))["items"]
    check("聊天表情写回 catalog", all(it.get("desc", "").startswith("新描述") for it in items2), items2)
    check("聊天表情进度回调", bool(chat_prog) and chat_prog[-1][0] == 2, chat_prog)

    # 聚合入口：force 全部重扫两个库
    agg = relabel_all_stickers(None, images_dir=base, workers=1, vision=fake_vision,
                               progress=lambda *a: None)
    check("聚合入口重扫两个库", agg.get("wechat_fav", {}).get("described") == 2
          and agg.get("chat_stickers", {}).get("described") == 2, agg)

    # 非 force 时只补空描述（不算“重新扫描”，但保证原逻辑仍可用）
    fav2 = WeChatFavStickers(base, None)
    fav2._vision_ask = fake_vision
    res3 = fav2.describe(force=False, workers=1)
    check("非 force 时无待补项", res3.get("pending") == 0, res3)

    print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
