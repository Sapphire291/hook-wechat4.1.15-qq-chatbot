# -*- coding: utf-8 -*-
"""直发层回归测试：/SendTextTo ret=0 之后绝不重发（防止对方收到两条相同消息）。

事故背景（2026-10-26 15:56）：旧实现把 RecvStats.repl 当作“是否成功”的依据，
repl 回执慢/没到时会 `for attempt in range(2)` 再发一次 —— 对方连收两条一样的回复，
而 send_path.log 只记了一次 ok=False，pipeline 也因此没把回复写进记忆。

用法：python tests/test_direct_send.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.integration.direct_send as ds  # noqa: E402


def main() -> int:
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(("PASS" if cond else "FAIL") + " - " + name + ((" ｜ " + str(extra)) if (extra and not cond) else ""))
        if not cond:
            ok = False

    originals = {
        "_post": ds._post,
        "current_chat": ds.current_chat,
        "resolve_wxid": ds.resolve_wxid,
        "_wait_repl": ds._wait_repl,
        "LAST_DETAIL": dict(ds.LAST_DETAIL),
    }

    try:
        ds.current_chat = lambda: "POS95518"
        ds.resolve_wxid = lambda name: "wxid_target"
        ds._wait_repl = lambda timeout=6.0: {"runs": 1, "repl": 0}

        # 1) ret=0 但 repl 一直 0（事故场景）→ 只发一次、返回 True
        calls = []

        def fake_post_ret0(path, payload=None, timeout=10):
            calls.append(path)
            if path in ("/ArmRecv", "/SendTextTo"):
                return {"ret": 0}
            if path == "/RecvStats":
                return {"runs": 1, "repl": 0}
            return {}

        ds._post = fake_post_ret0
        sent = ds.direct_send_text("wxid_target", "党的十五大要点……")
        sends = calls.count("/SendTextTo")
        check("ret=0 时只发送一次（不重复）", sent is True and sends == 1,
              (sent, sends, calls))
        check("发送诊断写入 LAST_DETAIL", "ret=0" in ds.last_detail("text"),
              ds.last_detail("text"))

        # 2) ret!=0 → 允许重试一次；第二次 ret=0 成功
        calls = []
        state = {"n": 0}

        def fake_post_retry(path, payload=None, timeout=10):
            calls.append(path)
            if path == "/ArmRecv":
                return {"ret": 0}
            if path == "/SendTextTo":
                state["n"] += 1
                return {"ret": 7 if state["n"] == 1 else 0}
            if path == "/RecvStats":
                return {"runs": 1, "repl": 1}
            return {}

        ds._post = fake_post_retry
        ds._wait_repl = lambda timeout=6.0: {"runs": 1, "repl": 1}
        sent = ds.direct_send_text("wxid_target", "hello")
        check("接口明确失败(ret!=0) 才重试，成功后返回 True",
              sent is True and calls.count("/SendTextTo") == 2, (sent, calls))

        # 3) 连续两次 ret!=0 → 返回 False（不误报成功）
        calls = []

        def fake_post_fail(path, payload=None, timeout=10):
            calls.append(path)
            if path == "/ArmRecv":
                return {"ret": 0}
            if path == "/SendTextTo":
                return {"ret": 9}
            return {"runs": 0, "repl": 0}

        ds._post = fake_post_fail
        sent = ds.direct_send_text("wxid_target", "hello")
        check("两次接口失败 → False，且每次只发一次",
              sent is False and calls.count("/SendTextTo") == 2, (sent, calls))

        # 4) ArmRecv 失败 → 不发送、返回 False
        calls = []

        def fake_post_arm(path, payload=None, timeout=10):
            calls.append(path)
            if path == "/ArmRecv":
                return {"ret": 3}
            if path == "/SendTextTo":
                return {"ret": 0}
            return {}

        ds._post = fake_post_arm
        sent = ds.direct_send_text("wxid_target", "hello")
        check("ArmRecv 失败不发送", sent is False and "/SendTextTo" not in calls, calls)

        # 5) 图片同样：ret=0 只发一次
        calls = []

        def fake_post_img(path, payload=None, timeout=10):
            calls.append(path)
            if path == "/ArmRecv":
                return {"ret": 0}
            if path == "/SendImageVirtual2":
                return {"ret": 0}
            return {"runs": 1, "repl": 0}

        ds._post = fake_post_img
        ok_img = ds.direct_send_image("wxid_target", "E:/EMOJE/x/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.gif")
        check("图片 ret=0 时只发送一次", ok_img is True and calls.count("/SendImageVirtual2") == 1,
              (ok_img, calls))
    finally:
        ds._post = originals["_post"]
        ds.current_chat = originals["current_chat"]
        ds.resolve_wxid = originals["resolve_wxid"]
        ds._wait_repl = originals["_wait_repl"]
        ds.LAST_DETAIL.update(originals["LAST_DETAIL"])

    print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
