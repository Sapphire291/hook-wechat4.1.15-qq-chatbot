# -*- coding: utf-8 -*-
"""可移植性（不绑固定盘符）测试：路径解析、旧绝对路径回退、表情记忆相对路径。"""
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


_TMP_HOME = _mk_tmp("paths_")
os.environ["CHATBOT_HOME"] = _TMP_HOME

sys.path.insert(0, _REPO_ROOT)

from src.core import paths as P  # noqa: E402
from src.memory.sticker_memory import StickerMemory  # noqa: E402


def main() -> int:
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(("PASS" if cond else "FAIL") + " - " + name + ((" ｜ " + str(extra)) if (extra and not cond) else ""))
        if not cond:
            ok = False

    home = os.path.abspath(_TMP_HOME)
    check("data_home 使用 CHATBOT_HOME", os.path.abspath(P.data_home()) == home, P.data_home())
    for name, fn in (("data_dir", P.data_dir), ("memory_dir", P.memory_dir),
                     ("wid_dir", P.wid_dir), ("emoje_dir", P.emoje_dir)):
        p = os.path.abspath(fn())
        check("%s 在数据根下" % name, p.startswith(home + os.sep) or p == home, p)

    # 相对路径：EMOJE 按数据根；assets/... 按随包资源
    check("相对路径 EMOJE 按数据根解析", os.path.abspath(P.resolve_path("EMOJE")) == os.path.join(home, "EMOJE"))
    check("相对路径 assets/emoji 按资源根解析",
          os.path.abspath(P.resolve_path("assets/emoji")) == os.path.abspath(os.path.join(P.project_root(), "assets", "emoji")))

    # 旧盘符绝对路径失效 → 数据根下同名目录回退
    os.makedirs(os.path.join(home, "EMOJE"), exist_ok=True)
    old_abs = r"K:\SomeOldDrive\CHATBOT\EMOJE"
    check("旧绝对路径回退到数据根同名目录", os.path.abspath(P.resolve_path(old_abs)) == os.path.join(home, "EMOJE"))

    # 旧绝对文件路径 → 按文件名在新表情目录里找
    sub = os.path.join(home, "EMOJE", "wxid_test", "chat_stickers")
    os.makedirs(sub, exist_ok=True)
    target = os.path.join(sub, "abc123.gif")
    with open(target, "wb") as f:
        f.write(b"GIF89a")
    old_file = r"K:\CHATBOT\EMOJE\wxid_old\chat_stickers\abc123.gif"
    resolved = P.resolve_stored_file(old_file, [sub])
    check("旧 catalog 绝对路径按文件名回退", os.path.abspath(resolved) == os.path.abspath(target), resolved)
    check("相对 file 路径也能解析", os.path.abspath(P.resolve_stored_file("abc123.gif", [sub])) == os.path.abspath(target))

    # 表情记忆：新记录存相对数据根路径
    mem = StickerMemory("wechat", root=home)
    mem.record_send(contact="wechat:test", md5="abc123", path=target, tags=["笑"], desc="测试", send_ok=True)
    raw = [json.loads(x) for x in open(mem.path, encoding="utf-8") if x.strip()][-1]
    check("表情记忆存相对数据根路径", not os.path.isabs(raw.get("path") or ""), raw.get("path"))
    check("相对路径可拼回原文件", os.path.isfile(os.path.join(home, raw["path"])))

    # --check-paths 的关键函数存在且可用
    import importlib.util
    spec = importlib.util.spec_from_file_location("bot_main", os.path.join(_REPO_ROOT, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        check("main._check_paths 可调用", callable(getattr(mod, "_check_paths", None)))
    except Exception as exc:
        check("main 可导入（%r）" % (exc,), False)

    print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
