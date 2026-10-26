# -*- coding: utf-8 -*-
"""长回复分段发送：规则单测（不联网）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.text_split import split_for_send, _split_sentences  # noqa: E402


def main() -> int:
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(("PASS" if cond else "FAIL") + " - " + name + ((" ｜ " + str(extra)) if (extra and not cond) else ""))
        if not cond:
            ok = False

    # 1) 有自然分段 → 按分段切
    paras = "第一段说行情。今天涨了一点。\n\n第二段说消息面。公告还没出。\n\n第三段吐槽两句。就这样吧。"
    parts = split_for_send(paras, "今天怎么样", min_chars=40, max_parts=4)
    check("按自然分段切（3 段 → 3 条）", len(parts) == 3, parts)
    check("分段内容不丢", all(p.strip() for p in parts) and "第一段" in parts[0] and "第三段" in parts[-1], parts)

    # 1.1) 两段短闲聊也要分开发（不再受“最少字数”限制，实测 70 字两段曾被漏拆）
    short_two = ("哎哟，这么突然[捂脸] 我也想你啊，聊绿聊红聊新闻，来来回回都是你陪着。\n\n"
                 "不过先说好，想我可以，行情我可不敢替你查差了。[呲牙] [OK]")
    short_parts = split_for_send(short_two, "想你了", min_chars=80, max_parts=4)
    check("两段短闲聊按分段发（不受最少字数限制）", len(short_parts) == 2, short_parts)
    check("无分段的短文本仍不分段",
          len(split_for_send("好的，收到。今天聊得挺开心。", "嗯", min_chars=80)) == 1)

    # 2) 无分段长句 → 按句合并成 2~4 条，而不是每个句号一条
    long_text = ("今天大盘整体还行，指数小幅上涨。板块上化工比较强，万华跟着反弹了一点。"
                 "不过成交额不算大，说明资金还在观望。你要是追高得小心，别一上头就满仓。"
                 "消息面暂时没有特别大的雷。晚上再看看有没有公告。")
    parts2 = split_for_send(long_text, "今天怎么样", min_chars=60, max_parts=4)
    check("长句按语义合并（2~4 条）", 2 <= len(parts2) <= 4, len(parts2))
    check("不是每个句号都切", len(parts2) < long_text.count("。"), [len(p) for p in parts2])
    check("切分不丢内容", "".join(parts2).replace(" ", "") == long_text.replace(" ", ""), parts2)

    # 3) 短文本不分段
    check("短文本不分段", split_for_send("好的，收到", "嗯", min_chars=80) == ["好的，收到"])

    # 4) 逻辑性长文（作文/方案/代码/表格/清单）不分段
    essay = "论坚持。".ljust(0) + ("坚持是一种力量。" * 60)
    check("作文/写作请求不分段", split_for_send(essay, "帮我写一篇作文", min_chars=40) == [essay])
    code = "代码如下：\n\n```python\nprint('hi')\nprint('ho')\n```\n\n运行即可。"
    check("含代码块不分段", split_for_send(code, "帮我写个脚本", min_chars=10) == [code])
    lst = "要点如下：\n- 第一点\n- 第二点\n- 第三点\n- 第四点\n- 第五点\n以上。"
    check("结构化清单不分段", len(split_for_send(lst, "给我列一下", min_chars=10)) == 1)

    # 4.1) 带小标题/分点的“逻辑性强”长文本：即使有自然分段也不拆
    structured = ("**基本盘**：正式代表 2048 人，代表全国 5800 万名党员，江泽民作报告。\n\n"
                  "**理论上的大动作**：大会首次使用“邓小平理论”这个概念，并通过党章修正案。\n\n"
                  "**其他议程**：通过三项决议，随后十五届一中全会选举江泽民为总书记。")
    structured_parts = split_for_send(structured, "介绍一下党的十五大", min_chars=40)
    check("带小标题的结构化长文不分段", structured_parts == [structured], structured_parts)
    numbered = ("一、行情：今天指数小幅上涨，化工板块偏强。\n\n"
                "二、消息面：暂时没有大的利空，晚上看公告。\n\n"
                "三、操作：别追高，控制仓位。")
    check("分点式回答不分段",
          len(split_for_send(numbered, "帮我分析一下今天行情", min_chars=40)) == 1)
    analysis = ("先说结论：这只票短期偏震荡。首先，成交额没有明显放大，说明资金还在观望；"
                "其次，板块轮动比较快，追高的风险不小；最后，消息面上暂时没有新的催化。"
                "综上，建议等回调再看，不要上头满仓。")
    check("分析论证型长文不分段",
          len(split_for_send(analysis, "这票后面怎么走", min_chars=40)) == 1)
    # 4.2) 普通聊天式长段落（无结构标记）仍然分段
    chatty = ("今天大盘整体还行，指数小幅上涨。板块上化工比较强，万华跟着反弹了一点。\n\n"
              "不过成交额不算大，说明资金还在观望。你要是追高得小心，别一上头就满仓。\n\n"
              "消息面暂时没有特别大的雷。晚上再看看有没有公告。")
    check("日常聊天长段落仍会分段",
          len(split_for_send(chatty, "今天怎么样", min_chars=40)) >= 2,
          split_for_send(chatty, "今天怎么样", min_chars=40))

    # 5) 最多条数限制
    parts3 = split_for_send(long_text, "今天怎么样", min_chars=40, max_parts=2)
    check("最多条数生效", len(parts3) <= 2, len(parts3))

    # 6) 引号/括号里的句号不切
    sents = _split_sentences("他说“你好。再见。”然后就走了。我也回家了。")
    check("括号/引号内句号不切", len(sents) == 2 and "再见" in sents[0] and "然后" in sents[0], sents)

    # 7) 太短的尾段并入上一段
    parts4 = split_for_send("今天不错。" * 5 + "好。", "聊聊", min_chars=20, max_parts=4)
    check("极短尾段并入", all(len(p) >= 6 for p in parts4), [len(p) for p in parts4])

    print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
