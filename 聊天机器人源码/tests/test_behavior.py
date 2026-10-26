# -*- coding: utf-8 -*-
"""共享记忆 + 对话推进（追问/确认理解/连续含糊主动询问）测试。"""
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


_TMP_HOME = _mk_tmp("behavior_")
os.environ["CHATBOT_HOME"] = _TMP_HOME

sys.path.insert(0, _REPO_ROOT)

from src.core.models import AppConfig, Message, Platform, WhitelistEntry  # noqa: E402
from src.core.paths import platform_memory_dir  # noqa: E402
from src.memory.chat_memory import ContactMemory  # noqa: E402
from src.memory.ledger import Ledger  # noqa: E402
from src.memory.sticker_memory import StickerMemory  # noqa: E402
from src.core import pipeline as pipeline_mod  # noqa: E402
from src.core.events import EventBus, MessageQueue  # noqa: E402
from src.core.interfaces import ChatAdapter, ReplyGenerator  # noqa: E402
from src.policy import create_frequency_policy, create_whitelist_policy  # noqa: E402
from src.security import create_detectors  # noqa: E402
from src.core.records import RecordsStore  # noqa: E402


class FakeAdapter(ChatAdapter):
    platform = Platform.WECHAT

    def __init__(self, config):
        super().__init__(config)
        self.sent = []

    def start(self):
        return True

    def stop(self):
        pass

    def available(self):
        return True

    def send_text(self, chat_id, text):
        self.sent.append(text)
        return True

    def send_image(self, chat_id, image_path):
        return True


class RecordingGenerator(ReplyGenerator):
    def __init__(self):
        self.calls = []

    def generate(self, system_prompt, style_instruction, history, user_message,
                 images=None, **kwargs):
        self.calls.append({"system_prompt": system_prompt, "kwargs": kwargs,
                           "user": str(user_message)})
        return "在呢，怎么啦？想聊点什么？"


def build_processor():
    cfg = AppConfig()
    cfg.whitelist = [WhitelistEntry(Platform.WECHAT, "chat-1", "测试群", True)]
    cfg.frequency_value = 0
    cfg.emoji_enabled = False
    cfg.web_enabled = False
    cfg.memory_shared = True
    tmp = _mk_tmp("records_")
    gen = RecordingGenerator()
    adapter = FakeAdapter(cfg)
    proc = pipeline_mod.MessageProcessor(
        config=cfg, queue=MessageQueue(), adapters={Platform.WECHAT: adapter},
        whitelist=create_whitelist_policy(cfg),
        frequency=create_frequency_policy(cfg),
        detectors=create_detectors(cfg),
        generators={Platform.WECHAT: gen},
        records=RecordsStore(base_dir=tmp), bus=EventBus(), emoji=None)
    return proc, gen, adapter, cfg


def main() -> int:
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(("PASS" if cond else "FAIL") + " - " + name + ((" ｜ " + str(extra)) if (extra and not cond) else ""))
        if not cond:
            ok = False

    # ---------------- 1) 共享记忆 ----------------
    check("默认记忆完全隔离（memory_shared=False）", AppConfig().memory_shared is False)
    check("旧配置缺字段时默认隔离", AppConfig.from_dict({"ai": {}}).memory_shared is False)

    wx = ContactMemory("wechat", shared=True, log=lambda m: None)
    qq = ContactMemory("qq", shared=True, log=lambda m: None)
    check("QQ 与微信共用同一记忆根", wx.root == qq.root == platform_memory_dir("wechat"), (wx.root, qq.root))
    separate = ContactMemory("qq", shared=False, log=lambda m: None)
    check("关闭共享时 QQ 用独立目录", separate.root == platform_memory_dir("qq"), separate.root)

    # 共享账本：同一实例、id 不撞、平台字段正确
    check("共享账本是单实例", wx.ledger() is qq.ledger())
    id1 = wx.record("mem.test", "wechat:wxid_a", {"k": 1})
    id2 = qq.record("mem.test", "qq:10001", {"k": 2})
    check("共享账本 id 递增不重复", id1 != id2 and id2 == id1 + 1, (id1, id2))
    import json
    ledger_path = os.path.join(wx.root, "ledger", "ledger.jsonl")
    recs = [json.loads(x) for x in open(ledger_path, encoding="utf-8") if x.strip()]
    plats = [r.get("platform") for r in recs if r.get("action") == "mem.test"]
    check("共享账本记录保留各自平台", plats == ["wechat", "qq"], plats)

    # 表情记忆共享
    wxs = StickerMemory("wechat", shared=True)
    qqs = StickerMemory("qq", shared=True)
    check("表情记忆也共享同一文件", wxs.path == qqs.path, (wxs.path, qqs.path))

    # ---------------- 2) 宪章包含新行为 ----------------
    proc, gen, adapter, cfg = build_processor()
    msg0 = Message(platform=Platform.WECHAT, chat_id="chat-1", chat_name="测试群",
                   is_group=True, sender_id="u1", sender_name="张三",
                   content="今天好无聊", mentions_me=False, timestamp=time.time())
    proc.handle(msg0)
    main0 = next(c for c in reversed(gen.calls) if c["user"] == "今天好无聊")
    charter_prompt = main0["system_prompt"]
    check("宪章含“感兴趣可追问”", "追问" in charter_prompt)
    check("宪章含“没理解才先肯定再表达观点”",
          "先肯定对方的话" in charter_prompt and "表达我自己的观点" in charter_prompt
          and "不要每句话都反问确认" in charter_prompt)

    # ---------------- 3) 连续意义不明 → 主动询问 ----------------
    seq = ["嗯", "？", "哦"]
    vague_prompts = []
    for i, text in enumerate(seq):
        time.sleep(0.15)  # 频率策略 INTERVAL 最小 0.1s
        msg = Message(platform=Platform.WECHAT, chat_id="chat-1", chat_name="测试群",
                      is_group=True, sender_id="u2", sender_name="李四",
                      content=text, mentions_me=False, timestamp=time.time())
        proc.handle(msg)
        main_call = next(c for c in reversed(gen.calls) if c["user"] == text)
        vague_prompts.append(main_call["system_prompt"])
    check("第 1、2 条含糊不触发主动询问",
          all("【对话推进】" not in p for p in vague_prompts[:2]))
    check("连续第 3 条含糊触发主动询问",
          "【对话推进】" in vague_prompts[2], vague_prompts[2][-200:])

    # 说完一条正常内容后计数清零
    time.sleep(0.15)
    msg_ok = Message(platform=Platform.WECHAT, chat_id="chat-1", chat_name="测试群",
                     is_group=True, sender_id="u2", sender_name="李四",
                     content="今天股市怎么样，帮我看看最近行情", mentions_me=False,
                     timestamp=time.time())
    proc.handle(msg_ok)
    check("正常消息后含糊计数清零", proc._vague_streak.get(proc._chat_key(msg_ok), 0) == 0)

    # 判定函数本身
    def is_vague(text, **kw):
        m = Message(platform=Platform.WECHAT, chat_id="chat-1", chat_name="测试群",
                    is_group=True, sender_id="u1", sender_name="张三",
                    content=text, mentions_me=False, timestamp=time.time(), **kw)
        return proc._is_vague_message(m)

    check("“嗯”算含糊", is_vague("嗯"))
    check("“？”算含糊", is_vague("？"))
    check("只有表情占位算含糊", is_vague("[图片]"))
    check("正常一句话不算含糊", not is_vague("今天大盘怎么样"))
    check("带图片的消息不算含糊", not is_vague("", image_paths=["x.jpg"]))

    print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
