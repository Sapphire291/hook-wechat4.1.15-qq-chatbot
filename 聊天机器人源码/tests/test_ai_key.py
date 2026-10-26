# -*- coding: utf-8 -*-
"""AI Key 健壮性：打码/空 Key 回退 + 掩码识别（QQ 不回复的根因防再犯）。"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP_BASE = os.path.join(_REPO_ROOT, ".test_tmp")
os.makedirs(_TMP_BASE, exist_ok=True)
_TMP_HOME = os.path.join(_TMP_BASE, "aikey_%d" % os.getpid())
os.makedirs(_TMP_HOME, exist_ok=True)
os.environ["CHATBOT_HOME"] = _TMP_HOME

sys.path.insert(0, _REPO_ROOT)

from src.ai.deepseek import DeepSeekClient, _is_masked_key  # noqa: E402
from src.core.models import AppConfig, Platform  # noqa: E402


def main() -> int:
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(("PASS" if cond else "FAIL") + " - " + name + ((" ｜ " + str(extra)) if (extra and not cond) else ""))
        if not cond:
            ok = False

    check("识别空 Key", _is_masked_key(""))
    check("识别打码 Key", _is_masked_key("***********************************"))
    check("识别 sk-****", _is_masked_key("sk-********"))
    check("正常 Key 不误判", not _is_masked_key("sk-fake-test-key-0123456789abcdef"))

    cfg = AppConfig()
    cfg.wechat_ai.api_key = "sk-goodwechatkey1234567890"
    cfg.qq_ai.api_key = "********"          # 事故场景：QQ Key 被打码
    cfg.qq_ai.base_url = cfg.wechat_ai.base_url = "https://api.deepseek.com"
    qq_client = DeepSeekClient(cfg, Platform.QQ)
    effective = qq_client._effective_key(cfg.qq_ai)
    check("QQ Key 打码 → 回退到微信同源 Key", effective == "sk-goodwechatkey1234567890", effective)
    # 回退会写 ai_error.log（写到临时数据目录，不污染真实目录）
    log_path = os.path.join(os.environ.get("CHATBOT_HOME", ""), "data", "ai_error.log")
    check("回退写 ai_error.log", os.path.exists(log_path), log_path)

    # base_url 不同 → 不回退（避免把 Key 发到别的地址）
    cfg2 = AppConfig()
    cfg2.wechat_ai.api_key = "sk-goodwechatkey1234567890"
    cfg2.wechat_ai.base_url = "https://api.deepseek.com"
    cfg2.qq_ai.api_key = "********"
    cfg2.qq_ai.base_url = "https://other.example.com"
    qq2 = DeepSeekClient(cfg2, Platform.QQ)
    check("base_url 不同不回退", qq2._effective_key(cfg2.qq_ai) == "", qq2._effective_key(cfg2.qq_ai))

    # 正常 Key 原样返回
    cfg3 = AppConfig()
    cfg3.qq_ai.api_key = "sk-qqvalidkey0987654321"
    check("正常 Key 原样使用",
          DeepSeekClient(cfg3, Platform.QQ)._effective_key(cfg3.qq_ai) == "sk-qqvalidkey0987654321")

    print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
