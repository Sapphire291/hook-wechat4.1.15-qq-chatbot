# -*- coding: utf-8 -*-
"""设置界面：分平台 AI 配置、白名单（支持从好友/群列表选择）、回复策略等。"""
from __future__ import annotations

import json
import os
import threading
import time
import tkinter as tk
import uuid
from tkinter import ttk, messagebox, simpledialog
from typing import Callable, List, Optional

try:
    from PIL import Image, ImageTk
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False

from ..core.models import (
    AppConfig,
    FrequencyStrategy,
    Platform,
    PlatformAIConfig,
    ReplyStyle,
    WhitelistEntry,
)

# 联网搜索服务（设置界面下拉框）：值 -> 展示名
try:
    from ..web.search import PROVIDER_LABELS as _WEB_PROVIDER_LABELS
except Exception:
    _WEB_PROVIDER_LABELS = {
        "auto": "自动（免 Key 多引擎）",
        "so_html": "360 网页兜底（免 Key）",
        "bing_html": "Bing 网页兜底（免 Key）",
        "baidu_html": "百度网页兜底（免 Key）",
        "ddg": "DuckDuckGo 兜底（免 Key）",
        "serper": "Serper（需 Key）",
        "bing": "Bing API（需 Key）",
        "brave": "Brave（需 Key）",
        "tavily": "Tavily（需 Key）",
        "searxng": "SearXNG（自建）",
    }
#: 下拉框顺序（默认 auto；免 Key 多引擎在前，便于没有 Key 时直接测试）
_WEB_PROVIDER_ORDER = ["auto", "so_html", "bing_html", "baidu_html", "ddg",
                       "serper", "bing", "brave", "tavily", "searxng"]
_WEB_PROVIDER_NAMES = [_WEB_PROVIDER_LABELS.get(v, v) for v in _WEB_PROVIDER_ORDER]
_WEB_PROVIDER_VALUES = {_WEB_PROVIDER_LABELS.get(v, v): v for v in _WEB_PROVIDER_ORDER}

_STYLE_NAMES = ["正式", "幽默", "简洁", "详细", "自定义"]
_STRATEGY_LABELS = {
    FrequencyStrategy.INTERVAL: "按时间间隔（N 秒）",
    FrequencyStrategy.COUNT: "按消息条数（N 条）",
    FrequencyStrategy.MENTION_ONLY: "仅被 @ 时回复",
}
_LABEL_STRATEGY = {v: k for k, v in _STRATEGY_LABELS.items()}
_PLATFORM_LABELS = {Platform.WECHAT: "微信", Platform.QQ: "QQ"}
_PLATFORM_VALUES = {v: k for k, v in _PLATFORM_LABELS.items()}


class SettingsPanel(ttk.Frame):
    """设置面板：微信 AI / QQ AI / 白名单 / 回复设置 四个子页。"""

    def __init__(self, master, on_save: Optional[Callable[[AppConfig], None]] = None,
                 chat_fetcher=None, sticker_fetcher=None, sticker_favoriter=None,
                 memory_store=None, wechat_plugin=None, bot_controller=None,
                 memory_group_action=None):
        super().__init__(master, padding=6)
        self.on_save = on_save
        self.bot_controller = bot_controller
        self.memory_group_action = memory_group_action
        self.chat_fetcher = chat_fetcher
        self.sticker_fetcher = sticker_fetcher
        self.sticker_favoriter = sticker_favoriter
        self.memory_store = memory_store
        self.wechat_plugin = wechat_plugin
        self._qr_photo = None
        self._scroll_pairs = []
        self._memory_groups = []
        self._wl_original_names = {}
        self._wl_contacts_cache = None
        self._build()

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)

        self.wx_tab = self._make_scrollable("微信 AI")
        self.qq_tab = self._make_scrollable("QQ AI")
        self.wl_tab = self._make_scrollable("白名单")
        self.other_tab = self._make_scrollable("回复设置")

        self._build_ai(self.wx_tab, "wx")
        self._build_ai(self.qq_tab, "qq")
        self._build_whitelist()
        self._build_other()

        self.sticker_tab = self._make_scrollable("表情包")
        self._build_stickers()

        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="x", pady=(8, 0))
        self._build_platform_bar(bottom)
        ttk.Button(bottom, text="保存设置", command=self._save).pack(fill="x", pady=(6, 0))

        # 给滚动页内所有控件绑定鼠标滚轮（Entry/Text 等控件上也能滚动页面）
        for canvas, inner in self._scroll_pairs:
            self._bind_wheel_recursive(inner, canvas)

    def _make_scrollable(self, text: str):
        """创建一个带垂直滚动条的标签页，返回其中的内容容器。"""
        outer = ttk.Frame(self.nb)
        self.nb.add(outer, text=text)
        canvas = tk.Canvas(outer, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas, padding=10)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        self._scroll_pairs.append((canvas, inner))
        return inner

    def _bind_wheel_recursive(self, widget, canvas) -> None:
        """递归给内容容器内所有控件绑定鼠标滚轮；有自身滚动条的控件（Text 等）
        只滚动自身，不带动外层页面。"""
        if isinstance(widget, (tk.Text, tk.Listbox, tk.Canvas, ttk.Treeview)):
            def _inner(event, w=widget):
                w.yview_scroll(int(-event.delta / 120), "units")
                return "break"
            widget.bind("<MouseWheel>", _inner)
        else:
            handler = lambda event, c=canvas: c.yview_scroll(int(-event.delta / 120), "units")
            widget.bind("<MouseWheel>", handler)
        for child in widget.winfo_children():
            self._bind_wheel_recursive(child, canvas)

    def _build_ai(self, parent, key: str) -> None:
        """构建一个平台的 AI 配置表单（key: 'wx' 或 'qq'）。"""
        ttk.Label(parent, text="底层提示词（自我层）").pack(anchor="w")
        ttk.Label(parent, text="每轮注入；绑定 memory/wechatMM/SELF.md 或 QQMM/SELF.md",
                  foreground="#888").pack(anchor="w")
        prompt_frame = ttk.Frame(parent)
        prompt_frame.pack(fill="x", pady=(0, 8))
        prompt = tk.Text(prompt_frame, height=13, wrap="word")
        prompt_sb = ttk.Scrollbar(prompt_frame, orient="vertical", command=prompt.yview)
        prompt.configure(yscrollcommand=prompt_sb.set)
        prompt.pack(side="left", fill="both", expand=True)
        prompt_sb.pack(side="right", fill="y")
        setattr(self, f"{key}_self_prompt", prompt)

        grid = ttk.Frame(parent)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="API Key").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        key_entry = ttk.Entry(grid, show="*")
        key_entry.grid(row=0, column=1, sticky="ew", pady=3)
        setattr(self, f"{key}_key", key_entry)

        ttk.Label(grid, text="Base URL").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        base_entry = ttk.Entry(grid)
        base_entry.grid(row=1, column=1, sticky="ew", pady=3)
        setattr(self, f"{key}_base", base_entry)

        ttk.Label(grid, text="模型").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        model_box = ttk.Combobox(grid)
        model_box.grid(row=2, column=1, sticky="ew", pady=3)
        setattr(self, f"{key}_model", model_box)
        refresh_btn = ttk.Button(grid, text="刷新模型列表",
                                 command=lambda k=key: self._refresh_models(k))
        refresh_btn.grid(row=2, column=2, sticky="e", padx=(8, 0), pady=3)
        setattr(self, f"{key}_model_btn", refresh_btn)

        ttk.Button(parent, text="ECHO 记忆",
                   command=lambda: self._open_echo_window(key)).pack(anchor="w", pady=(10, 0))

        if key == "qq":
            pass
        elif key == "wx":
            self._build_wechat_section(parent)

    def _open_memory_window(self, key: str) -> None:
        if self.memory_store is None:
            messagebox.showwarning("提示", "当前环境未提供长期记忆存储")
            return
        from .memory import MemoryWindow
        prefix = "wechat:" if key == "wx" else "qq:"
        MemoryWindow(self, self.memory_store, platform_prefix=prefix,
                     on_path_saved=self._on_memory_path_saved)

    def _open_echo_window(self, key: str) -> None:
        """ECHO 记忆：按联系人分开的摘要记忆（真正的记忆），可查看/编辑/整理。"""
        pv = "wechat" if key == "wx" else "qq"
        try:
            mem = self._echo_mem(pv)
        except Exception as e:
            messagebox.showwarning("提示", "记忆不可用: %r" % (e,))
            return
        win = tk.Toplevel(self)
        win.title("ECHO 记忆 · %s" % ("微信" if pv == "wechat" else "QQ"))
        win.geometry("1000x780")

        ttk.Label(win, text="每个联系人一份「摘要记忆」（选择性记录，AI 每轮从这里读取；"
                            "8000 字上限，超限自动压缩到 4000）。可直接修改后保存。",
                  foreground="#444", wraplength=960, justify="left").pack(
            anchor="w", padx=10, pady=(10, 2))

        chats = []
        try:
            chats = mem.list_chats()
        except Exception:
            chats = []
        labels = ["%s（%s，%d 字）" % (name, chat, chars) for chat, name, _l, chars in chats]

        row = ttk.Frame(win)
        row.pack(fill="x", padx=10, pady=3)
        ttk.Label(row, text="联系人").pack(side="left")
        var = tk.StringVar(value=labels[0] if labels else "")
        combo = ttk.Combobox(row, textvariable=var, values=labels, width=60, state="readonly")
        combo.pack(side="left", padx=6)
        status = ttk.Label(row, text="", foreground="#666")
        status.pack(side="left", padx=(8, 0))

        txt = tk.Text(win, wrap="word", undo=True)
        txt.pack(fill="both", expand=True, padx=10, pady=(4, 6))

        def current_chat():
            i = combo.current()
            if 0 <= i < len(chats):
                return chats[i][0]
            return chats[0][0] if chats else ""

        def refresh_status():
            try:
                d = mem.doctor()
                status.configure(text="联系人=%s（有摘要 %s）| 对话轮=%s | 日志=%s 行" % (
                    d.get("chats"), d.get("summaries"), d.get("turns"), d.get("lines")))
            except Exception:
                status.configure(text="")

        def load_memory(_evt=None):
            chat = current_chat()
            try:
                body = mem.working_memory(chat, max_chars=8000) if chat else ""
            except Exception as e:
                body = "（读取失败: %r）" % (e,)
            txt.delete("1.0", "end")
            txt.insert("1.0", body)
            refresh_status()

        def save_memory():
            chat = current_chat()
            if not chat:
                messagebox.showwarning("提示", "没有可保存的联系人")
                return
            try:
                from ..ai.deepseek import DeepSeekClient
                from ..core.models import Platform
                gen = DeepSeekClient(self.collect(), Platform(pv))
                mem.write_summary(chat, txt.get("1.0", "end-1c"), generator=gen,
                                  max_chars=8000, compress_to=4000)
                refresh_status()
                messagebox.showinfo("提示", "已保存该联系人的摘要记忆（已记账）")
            except Exception as e:
                messagebox.showwarning("提示", "保存失败: %r" % (e,))

        def view_exchanges():
            chat = current_chat()
            try:
                recs = mem.entries(chat)
            except Exception as e:
                messagebox.showwarning("提示", "读取交流记录失败: %r" % (e,))
                return
            top = tk.Toplevel(win)
            top.title("完整交流记录 · %s" % (mem.display_name(chat) if chat else ""))
            top.geometry("900x680")
            ttk.Label(top, text="来自 data\\exchanges.jsonl（完整交流记录；回复记录界面只显示机器人回复）",
                      foreground="#666").pack(anchor="w", padx=10, pady=6)
            box = tk.Text(top, wrap="word")
            box.pack(fill="both", expand=True, padx=10, pady=(0, 10))
            lines = []
            for rec in recs:
                who = rec.get("sender") or ("我" if rec.get("role") == "assistant" else "对方")
                lines.append("%s: %s" % (who, str(rec.get("text") or "")))
            box.insert("1.0", "\n".join(lines) if lines else "（该联系人暂无完整交流记录）")

        def do_cleanup():
            self._cleanup_summaries(pv)
            load_memory()

        row2 = ttk.Frame(win)
        row2.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(row2, text="保存修改", command=save_memory).pack(side="left")
        ttk.Button(row2, text="刷新", command=load_memory).pack(side="left", padx=6)
        ttk.Button(row2, text="查看交流记录", command=view_exchanges).pack(side="left", padx=(6, 0))
        ttk.Button(row2, text="整理全部记忆", command=do_cleanup).pack(side="left", padx=(6, 0))
        ttk.Button(row2, text="打开记忆目录",
                   command=lambda: self._open_memory_dir(pv)).pack(side="left", padx=(6, 0))
        ttk.Button(row2, text="打开账本目录",
                   command=lambda: self._open_ledger_dir(pv)).pack(side="left", padx=(6, 0))
        ttk.Button(row2, text="关闭", command=win.destroy).pack(side="right")

        combo.bind("<<ComboboxSelected>>", load_memory)
        load_memory()

    def _cleanup_summaries(self, platform_value: str) -> None:
        """对每个联系人的摘要记忆做一次 AI 整理/压缩（摘要才是真记忆）。"""
        try:
            from ..ai.deepseek import DeepSeekClient
            from ..core.models import Platform
            gen = DeepSeekClient(self.collect(), Platform(platform_value))
            mem = self._echo_mem(platform_value)
            done = 0
            for chat, name, _lines, chars in mem.list_chats():
                if chars <= 0:
                    continue
                cur = mem.summary(chat)
                try:
                    new_text = gen.generate(
                        system_prompt=("整理下面的『联系人记忆摘要』：保留身份/关系/偏好/重要事件/"
                                       "计划/约定/承诺/雷区/图片要点；删除寒暄、重复、无信息量内容；"
                                       "总长不超过 4000 字；不要编造；直接输出整理后的完整摘要。"),
                        style_instruction="简洁", history=[], user_message=cur) or ""
                except Exception:
                    new_text = ""
                if new_text.strip():
                    mem.write_summary(chat, new_text, generator=gen,
                                      max_chars=4000, compress_to=4000)
                    done += 1
            d = mem.doctor()
            messagebox.showinfo("记忆整理", "平台=%s\n整理联系人=%d\n摘要总字数=%s"
                                % (platform_value, done, d.get("summary_chars")))
        except Exception as e:
            messagebox.showwarning("提示", "整理失败: %r" % (e,))

    def _on_memory_path_saved(self, path: str) -> None:
        # ECHO 记忆目录由平台固定（memory/wechatMM、memory/QQMM），不再接受手改路径。
        pass

    def _build_wechat_section(self, parent) -> None:
        """「微信 AI」页里的微信连接区块（hook 接入）。"""
        ttk.Separator(parent, orient="horizontal").pack(fill="x", pady=10)
        ttk.Label(parent, text="微信连接（Hook）", font=("", 10, "bold")).pack(anchor="w")
        self.wechat_conn_status = ttk.Label(parent, text="未连接", font=("", 11), foreground="#666")
        self.wechat_conn_status.pack(anchor="w", pady=4)
        row = ttk.Frame(parent)
        row.pack(pady=4)
        ttk.Button(row, text="检测微信", command=self._wechat_check).pack(side="left")
        ttk.Button(row, text="一键连接", command=self._wechat_connect).pack(side="left", padx=8)
        ttk.Button(row, text="断开", command=self._wechat_disconnect).pack(side="left")
        ttk.Label(parent, text="（需电脑端微信已登录；微信 4.x 需本机注入 hook）",
                  foreground="#888").pack()

    def _wechat_check(self) -> None:
        p = self.wechat_plugin
        if p is None:
            self.wechat_conn_status.config(text="未找到微信 Hook 插件", foreground="#b33")
            return
        try:
            self.wechat_conn_status.config(text="检测中…", foreground="#666")
            p._detect_async()
            self.after(2500, self._wechat_sync_status)
        except Exception as exc:
            self.wechat_conn_status.config(text=f"检测失败：{exc}", foreground="#b33")

    def _wechat_connect(self) -> None:
        p = self.wechat_plugin
        if p is None:
            self.wechat_conn_status.config(text="未找到微信 Hook 插件", foreground="#b33")
            return
        try:
            self.wechat_conn_status.config(text="正在连接…", foreground="#666")
            p._attach_async()
            self.after(3000, self._wechat_sync_status)
        except Exception as exc:
            self.wechat_conn_status.config(text=f"连接失败：{exc}", foreground="#b33")

    def _wechat_disconnect(self) -> None:
        p = self.wechat_plugin
        if p is None:
            return
        try:
            p._detach()
            self.wechat_conn_status.config(text="已断开", foreground="#666")
        except Exception:
            pass

    def _wechat_sync_status(self) -> None:
        p = self.wechat_plugin
        if p is None:
            return
        try:
            ok, msg = p.check()
            self.wechat_conn_status.config(text=msg, foreground="#2a7d2a" if ok else "#b33")
        except Exception:
            pass

    def _refresh_models(self, key: str) -> None:
        """拉取该平台 API 的可用模型列表，填充到下拉框。"""
        api_key = getattr(self, f"{key}_key").get().strip()
        base_url = getattr(self, f"{key}_base").get().strip() or "https://api.deepseek.com"
        if not api_key:
            messagebox.showinfo("提示", "请先填写 API Key")
            return
        from ..ai.deepseek import fetch_models
        models = fetch_models(api_key, base_url)
        box = getattr(self, f"{key}_model")
        if models:
            box["values"] = models
            current = box.get()
            if current not in models:
                box.set(models[0])
            messagebox.showinfo("已刷新", f"共拉取到 {len(models)} 个模型")
        else:
            messagebox.showwarning("获取失败", "未能拉取模型列表（请检查 API Key / Base URL / 网络）")

    def _build_whitelist(self) -> None:
        bar = ttk.Frame(self.wl_tab)
        bar.pack(fill="x")
        ttk.Label(bar, text="当前白名单（机器人会回复以下会话）").pack(side="left")
        ttk.Button(bar, text="删除选中", command=self._remove_whitelist).pack(side="right")
        ttk.Button(bar, text="解除记忆合并", command=self._unlink_memory_selected).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="打通记忆（合并选中会话）",
                   command=self._link_memory_selected).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="刷新好友/群并同步ID",
                   command=self._refresh_contacts).pack(side="right", padx=(0, 6))

        cols = ("platform", "name", "chat_id", "is_group")
        tree_frame = ttk.Frame(self.wl_tab)
        tree_frame.pack(fill="both", expand=True, pady=(4, 8))
        self.wl_tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
        for c, title, w in [("platform", "平台", 70), ("name", "会话名", 150),
                            ("chat_id", "标识（QQ号/群号/会话名）", 210), ("is_group", "类型", 70)]:
            self.wl_tree.heading(c, text=title)
            self.wl_tree.column(c, width=w, anchor="w")
        self.wl_tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.wl_tree.yview)
        self.wl_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")

        self.wl_mem_hint = ttk.Label(self.wl_tab, text="", foreground="#666")
        self.wl_mem_hint.pack(anchor="w", pady=(4, 0))
        self.wl_group_preview = ttk.Label(self.wl_tab, text="", foreground="#555",
                                         wraplength=760, justify="left")
        self.wl_group_preview.pack(anchor="w", pady=(2, 0))

        add_frame = ttk.LabelFrame(self.wl_tab, text="添加白名单")
        add_frame.pack(fill="x")
        row = ttk.Frame(add_frame)
        row.pack(fill="x", padx=6, pady=6)
        ttk.Label(row, text="平台").pack(side="left", padx=(0, 4))
        self.add_platform = ttk.Combobox(row, values=["微信", "QQ"], width=6, state="readonly")
        self.add_platform.set("QQ")
        self.add_platform.pack(side="left", padx=(0, 12))
        ttk.Button(row, text="从好友/群列表选择…", command=self._pick_from_list).pack(side="left", padx=(0, 12))
        ttk.Button(row, text="手动添加", command=self._show_manual_add).pack(side="left")
        search_row = ttk.Frame(add_frame)
        search_row.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(search_row, text="查找昵称/备注").pack(side="left", padx=(0, 4))
        self.wl_search = ttk.Entry(search_row)
        self.wl_search.pack(side="left", fill="x", expand=True)
        self.wl_search.bind("<Return>", lambda e: self._search_whitelist())
        ttk.Button(search_row, text="查找并添加", command=self._search_whitelist).pack(side="left", padx=(6, 0))
        ttk.Label(add_frame, text="提示：查找时只需输入昵称或备注；加入时自动保存 wxid / 群 ID。",
                  foreground="#888").pack(anchor="w", padx=6, pady=(0, 4))

    def _build_other(self) -> None:
        f = ttk.Frame(self.other_tab)
        f.pack(fill="x", pady=4)
        ttk.Label(f, text="回复风格").pack(side="left", padx=(0, 8))
        self.style_var = tk.StringVar(value="简洁")
        self.style_box = ttk.Combobox(f, textvariable=self.style_var, values=_STYLE_NAMES,
                                      state="readonly", width=8)
        self.style_box.pack(side="left")
        self.style_box.bind("<<ComboboxSelected>>", self._on_style_change)
        self.instruction_entry = ttk.Entry(f, state="disabled")
        self.instruction_entry.pack(side="left", fill="x", expand=True, padx=(8, 0))

        f = ttk.Frame(self.other_tab)
        f.pack(fill="x", pady=4)
        ttk.Label(f, text="回复频率").pack(side="left", padx=(0, 8))
        self.strategy_var = tk.StringVar(value=_STRATEGY_LABELS[FrequencyStrategy.INTERVAL])
        self.strategy_box = ttk.Combobox(f, textvariable=self.strategy_var,
                                         values=list(_STRATEGY_LABELS.values()),
                                         state="readonly", width=16)
        self.strategy_box.pack(side="left", padx=(0, 8))
        ttk.Label(f, text="参数 N").pack(side="left", padx=(0, 4))
        self.freq_value = tk.Spinbox(f, from_=0.1, to=100000, width=8, increment=0.1)
        self.freq_value.pack(side="left")

        f = ttk.Frame(self.other_tab)
        f.pack(fill="x", pady=4)
        self.wechat_on = tk.BooleanVar(value=True)
        self.qq_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="启用微信", variable=self.wechat_on).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(f, text="启用 QQ", variable=self.qq_on).pack(side="left", padx=(0, 12))
        ttk.Label(f, text="QQ OneBot WS").pack(side="left", padx=(0, 4))
        self.qq_ws = ttk.Entry(f)
        self.qq_ws.pack(side="left", fill="x", expand=True)

        f = ttk.Frame(self.other_tab)
        f.pack(fill="x", pady=4)
        self.hook_send_on = tk.BooleanVar(value=False)
        self.screen_send_on = tk.BooleanVar(value=False)
        self.auto_inject_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="允许 hook 发送", variable=self.hook_send_on).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(f, text="允许屏幕自动化", variable=self.screen_send_on).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(f, text="启动自动注入 hook", variable=self.auto_inject_on).pack(side="left")

        f = ttk.Frame(self.other_tab)
        f.pack(fill="x", pady=4)
        self.lock_input_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="微信最小化时隔离微信输入（只接受 exe 信号）",
                        variable=self.lock_input_var).pack(side="left", padx=(0, 8))
        ttk.Label(f, text="（不影响 Word 焦点；Ctrl+Alt+Shift+U 临时解锁 30 秒）",
                  foreground="#888").pack(side="left")

        f = ttk.Frame(self.other_tab)
        f.pack(fill="x", pady=4)
        self.emoji_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="启用表情包", variable=self.emoji_on).pack(side="left", padx=(0, 12))
        ttk.Label(f, text="图片库目录").pack(side="left", padx=(0, 4))
        self.emoji_dir = ttk.Entry(f)
        self.emoji_dir.pack(side="left", fill="x", expand=True)

        # ===== 长回复分段发送 =====
        f = ttk.Frame(self.other_tab)
        f.pack(fill="x", pady=4)
        self.split_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="长回复分段发送（按语义分几条连续发）",
                        variable=self.split_on).pack(side="left", padx=(0, 12))
        ttk.Label(f, text="最少字数").pack(side="left", padx=(0, 4))
        self.split_min = tk.Spinbox(f, from_=20, to=2000, width=7)
        self.split_min.pack(side="left", padx=(0, 12))
        ttk.Label(f, text="最多条数").pack(side="left", padx=(0, 4))
        self.split_max = tk.Spinbox(f, from_=1, to=6, width=5)
        self.split_max.pack(side="left", padx=(0, 12))
        ttk.Label(f, text="（作文/方案/代码/表格/分析论证等逻辑性长文不分段）",
                  foreground="#888").pack(side="left")

        # ===== ECHO 记忆系统（L0–L5，参考 dsh-agent-soul）=====
        f = ttk.Frame(self.other_tab)
        f.pack(fill="x", pady=(10, 2))
        ttk.Label(f, text="ECHO 记忆系统", font=("", 10, "bold")).pack(side="left")
        ttk.Label(f, text="（一手日志 + 压缩索引 + 门牌检索；微信/QQ 数据完全隔离）",
                  foreground="#888").pack(side="left", padx=(8, 0))

        f = ttk.Frame(self.other_tab)
        f.pack(fill="x", pady=3)
        ttk.Label(f, text="微信记忆目录").pack(side="left", padx=(0, 6))
        self.memory_wx_dir = ttk.Entry(f)
        self.memory_wx_dir.pack(side="left", fill="x", expand=True)
        ttk.Button(f, text="打开", width=6,
                   command=lambda: self._open_memory_dir("wechat")).pack(side="left", padx=(6, 4))
        ttk.Button(f, text="体检", width=6,
                   command=lambda: self._echo_doctor("wechat")).pack(side="left")

        f = ttk.Frame(self.other_tab)
        f.pack(fill="x", pady=3)
        ttk.Label(f, text="QQ 记忆目录").pack(side="left", padx=(0, 6))
        self.memory_qq_dir = ttk.Entry(f)
        self.memory_qq_dir.pack(side="left", fill="x", expand=True)
        ttk.Button(f, text="打开", width=6,
                   command=lambda: self._open_memory_dir("qq")).pack(side="left", padx=(6, 4))
        ttk.Button(f, text="体检", width=6,
                   command=lambda: self._echo_doctor("qq")).pack(side="left")

        # 共享记忆能力保留但默认关闭（用户要求 QQ/微信记忆完全隔离，不在界面露出开关）
        self.memory_shared_var = tk.BooleanVar(value=False)

        f = ttk.Frame(self.other_tab)
        f.pack(fill="x", pady=3)
        ttk.Label(f, text="压缩预算（字符）").pack(side="left", padx=(0, 4))
        self.memory_budget = tk.Spinbox(f, from_=1000, to=1000000, width=9)
        self.memory_budget.pack(side="left", padx=(0, 12))
        ttk.Label(f, text="保留最近（字符）").pack(side="left", padx=(0, 4))
        self.memory_keep = tk.Spinbox(f, from_=200, to=500000, width=9)
        self.memory_keep.pack(side="left", padx=(0, 12))
        ttk.Label(f, text="（超过预算时把最旧对话压成带门牌的索引摘要，原文永不删除）",
                  foreground="#888").pack(side="left")

        f = ttk.Frame(self.other_tab)
        f.pack(fill="x", pady=(3, 6))
        ttk.Button(f, text="打开记忆根目录",
                   command=lambda: self._open_memory_root()).pack(side="left", padx=(0, 8))
        ttk.Button(f, text="全部体检",
                   command=lambda: self._echo_doctor("all")).pack(side="left", padx=(0, 8))
        ttk.Button(f, text="重建索引（导入旧历史）",
                   command=lambda: self._echo_rebuild("all")).pack(side="left")

        # ===== 墨 · 只追加账本 =====
        f = ttk.Frame(self.other_tab)
        f.pack(fill="x", pady=(10, 2))
        ttk.Label(f, text="墨 · 只追加账本", font=("", 10, "bold")).pack(side="left")
        ttk.Label(f, text="（记忆压缩/吊销/裁剪、回复发送、配置变更全部留痕，不可改写）",
                  foreground="#888").pack(side="left", padx=(8, 0))
        for pv, label in (("wechat", "微信账本"), ("qq", "QQ 账本")):
            f = ttk.Frame(self.other_tab)
            f.pack(fill="x", pady=3)
            ttk.Label(f, text=label).pack(side="left", padx=(0, 6))
            ent = ttk.Entry(f)
            ent.pack(side="left", fill="x", expand=True)
            setattr(self, "ledger_%s_entry" % pv, ent)
            ttk.Button(f, text="打开", width=6,
                       command=lambda p=pv: self._open_ledger_dir(p)).pack(side="left", padx=(6, 4))
            ttk.Button(f, text="统计", width=6,
                       command=lambda p=pv: self._ledger_stats(p)).pack(side="left", padx=(0, 4))
            ttk.Button(f, text="校验", width=6,
                       command=lambda p=pv: self._ledger_verify(p)).pack(side="left")

    # ===== 联网检索（阶段 A）=====
    def _build_web(self) -> None:
        tab = self.web_tab
        f = ttk.Frame(tab)
        f.pack(fill="x", pady=(0, 4))
        ttk.Label(f, text="联网检索（阶段 A：搜索 + 抓取正文）",
                  font=("", 10, "bold")).pack(side="left")
        ttk.Label(f, text="（默认关闭；外部内容只当资料、绝不执行；只允许公网 http/https）",
                  foreground="#888").pack(side="left", padx=(8, 0))

        f = ttk.Frame(tab)
        f.pack(fill="x", pady=3)
        self.web_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="启用联网检索（按需自动搜索）",
                        variable=self.web_on).pack(side="left", padx=(0, 16))
        ttk.Label(f, text="搜索服务").pack(side="left", padx=(0, 4))
        self.web_provider = ttk.Combobox(f, values=_WEB_PROVIDER_NAMES,
                                         state="readonly", width=22)
        self.web_provider.pack(side="left")

        f = ttk.Frame(tab)
        f.pack(fill="x", pady=3)
        ttk.Label(f, text="API Key").pack(side="left", padx=(0, 4))
        self.web_key = ttk.Entry(f, show="*")
        self.web_key.pack(side="left", fill="x", expand=True)
        ttk.Label(f, text="（只有 Serper/Bing/Brave/Tavily 需要；免 Key 用兜底）",
                  foreground="#888").pack(side="left", padx=(8, 0))

        f = ttk.Frame(tab)
        f.pack(fill="x", pady=3)
        ttk.Label(f, text="SearXNG 地址").pack(side="left", padx=(0, 4))
        self.web_base = ttk.Entry(f)
        self.web_base.pack(side="left", fill="x", expand=True)
        ttk.Label(f, text="（选 SearXNG 时填，如 http://127.0.0.1:8888）",
                  foreground="#888").pack(side="left", padx=(8, 0))

        f = ttk.Frame(tab)
        f.pack(fill="x", pady=3)
        ttk.Label(f, text="最多来源").pack(side="left", padx=(0, 4))
        self.web_max = tk.Spinbox(f, from_=1, to=12, width=5)
        self.web_max.pack(side="left", padx=(0, 14))
        ttk.Label(f, text="抓取正文条数").pack(side="left", padx=(0, 4))
        self.web_fetch = tk.Spinbox(f, from_=0, to=6, width=5)
        self.web_fetch.pack(side="left", padx=(0, 14))
        ttk.Label(f, text="单次超时(秒)").pack(side="left", padx=(0, 4))
        self.web_timeout_box = tk.Spinbox(f, from_=3, to=30, width=5)
        self.web_timeout_box.pack(side="left", padx=(0, 14))
        ttk.Label(f, text="单轮预算(秒)").pack(side="left", padx=(0, 4))
        self.web_budget = tk.Spinbox(f, from_=5, to=60, width=5)
        self.web_budget.pack(side="left")

        f = ttk.Frame(tab)
        f.pack(fill="x", pady=3)
        ttk.Label(f, text="每会话每分钟上限").pack(side="left", padx=(0, 4))
        self.web_per_min = tk.Spinbox(f, from_=1, to=20, width=5)
        self.web_per_min.pack(side="left", padx=(0, 14))
        ttk.Label(f, text="每日总配额").pack(side="left", padx=(0, 4))
        self.web_daily = tk.Spinbox(f, from_=0, to=100000, width=8)
        self.web_daily.pack(side="left", padx=(0, 6))
        ttk.Label(f, text="（0=不限）", foreground="#888").pack(side="left")

        f = ttk.Frame(tab)
        f.pack(fill="x", pady=(6, 2))
        self.web_test_btn = ttk.Button(f, text="测试联网", command=self._test_web)
        self.web_test_btn.pack(side="left", padx=(0, 8))
        ttk.Button(f, text="打开联网日志", command=self._open_web_log).pack(side="left", padx=(0, 8))
        ttk.Button(f, text="打开缓存目录", command=self._open_web_cache).pack(side="left", padx=(0, 8))
        ttk.Button(f, text="刷新用量", command=self._refresh_web_usage).pack(side="left")
        self.web_usage_label = ttk.Label(f, text="", foreground="#666")
        self.web_usage_label.pack(side="left", padx=(12, 0))

        tips = (
            "搜索策略：\n"
            "1) 只有 AI 判断这条消息需要外部信息时才联网（闲聊不联网，省时省钱）；\n"
            "2) 自动模式会用 360 / Bing / 百度网页版多引擎并行搜索，AI 一次给出 2~3 个短查询词合并去重；\n"
            "3) 结果先按「与问题的关键词重合 + 权威站点 + 届/轮一致」过滤，八竿子打不着的页面不会喂给 AI；\n"
            "4) 抓取最多 N 条正文（并行，默认 4 条、单条 2600 字），只取正文容器；第一轮全不相关会自动换 Bing 重搜一次；\n"
            "5) 外部内容一律当「不可信数据」，注入特征命中整段丢弃；回复要求引用来源 URL，查不到就说没查到。\n"
            "免 Key 兜底顺序：360 网页 → Bing 网页 → 百度网页 → DuckDuckGo；有 Key 时优先用 API 服务。\n"
            "安全线：搜索词先脱敏；只允许公网 http/https（拒绝内网/本机/元数据地址，重定向逐跳校验），超时 8s、最多 2MB；"
            "限频 + 每日配额；记录在 data\\web_log.jsonl，账本记 web.search / web.fetch / web.quote。")
        ttk.Label(tab, text=tips, foreground="#555", justify="left",
                  wraplength=1000).pack(anchor="w", pady=(8, 0))

    def _web_context_for_test(self, cfg=None):
        """按当前界面值创建 WebContext（用于「测试联网」与用量展示，不必先保存）。"""
        from ..web.web_context import WebContext
        return WebContext(cfg if cfg is not None else self.collect(), Platform.WECHAT)

    def _refresh_web_usage(self, cfg=None) -> None:
        try:
            text = self._web_context_for_test(cfg).usage_text()
        except Exception as e:
            text = "用量读取失败: %r" % (e,)
        try:
            self.web_usage_label.config(text=text)
        except Exception:
            pass

    def _test_web(self) -> None:
        query = simpledialog.askstring("测试联网", "输入要测试的搜索词：",
                                       initialvalue="今天的新闻", parent=self)
        if not query:
            return
        try:
            cfg = self.collect()
        except Exception as exc:
            messagebox.showwarning("提示", "读取当前设置失败: %r" % (exc,))
            return
        btn = getattr(self, "web_test_btn", None)
        if btn is not None:
            try:
                btn.configure(state="disabled")
            except Exception:
                pass

        def worker():
            text = ""
            try:
                ctx = self._web_context_for_test(cfg)
                rep = ctx.probe(query)
                text = rep.get("text") or "（无结果）"
            except Exception as exc:
                text = "测试失败：%r" % (exc,)

            def done():
                if btn is not None:
                    try:
                        btn.configure(state="normal")
                    except Exception:
                        pass
                self._show_text_window("联网测试结果", text)
                self._refresh_web_usage()

            try:
                self.after(0, done)
            except Exception:
                pass

        threading.Thread(target=worker, name="web-probe", daemon=True).start()

    def _show_text_window(self, title: str, text: str) -> None:
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("980x680")
        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        txt = tk.Text(frame, wrap="word")
        sb = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        txt.insert("1.0", str(text or ""))
        txt.configure(state="disabled")

    def _open_web_log(self) -> None:
        try:
            from ..core.paths import data_dir
            path = os.path.join(data_dir(), "web_log.jsonl")
            if os.path.exists(path):
                os.startfile(path)
            else:
                os.startfile(data_dir())
        except Exception as e:
            messagebox.showwarning("提示", "打开联网日志失败: %r" % (e,))

    def _open_web_cache(self) -> None:
        try:
            from ..core.paths import data_dir
            path = os.path.join(data_dir(), "web_cache")
            os.makedirs(path, exist_ok=True)
            os.startfile(path)
        except Exception as e:
            messagebox.showwarning("提示", "打开缓存目录失败: %r" % (e,))

    # ---- ECHO 记忆控制（按钮实现）----
    # ---- 墨（账本）控制 ----
    def _ledger_obj(self, platform_value: str):
        from ..memory.ledger import Ledger
        from ..core.paths import platform_memory_dir
        if self._shared_memory_enabled():
            return Ledger.shared(platform_memory_dir("wechat"))
        return Ledger(platform_value)

    def _open_ledger_dir(self, platform_value: str) -> None:
        try:
            os.startfile(self._ledger_obj(platform_value).dir)
        except Exception as e:
            messagebox.showwarning("提示", "打开账本目录失败: %r" % (e,))

    def _ledger_stats(self, platform_value: str) -> None:
        try:
            st = self._ledger_obj(platform_value).stats()
            acts = st.get("by_action") or {}
            text = "平台: %s\n路径: %s\n记录数: %s\n字节: %s\n最后一条: %s\n\n动作统计:\n%s" % (
                st.get("platform"), st.get("path"), st.get("records"), st.get("bytes"),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.get("last_t") or 0)),
                "\n".join("  %s: %s" % (k, v) for k, v in sorted(acts.items())))
            messagebox.showinfo("墨 · 账本统计", text)
        except Exception as e:
            messagebox.showwarning("提示", "账本统计失败: %r" % (e,))

    def _ledger_verify(self, platform_value: str) -> None:
        try:
            ok, errs = self._ledger_obj(platform_value).verify()
            if ok:
                messagebox.showinfo("墨 · 账本校验", "✅ 通过：所有记录可解析、id 严格递增、字段齐全")
            else:
                messagebox.showwarning("墨 · 账本校验",
                                       "⚠️ 发现 %d 处问题：\n%s" % (len(errs), "\n".join(errs[:10])))
        except Exception as e:
            messagebox.showwarning("提示", "账本校验失败: %r" % (e,))

    # ---- ECHO 编辑对话框 ----
    def _edit_text_file(self, platform_value: str, name: str) -> None:
        try:
            mem = self._echo_mem(platform_value)
            cur = ""
            try:
                with open(os.path.join(mem.root, name), "r", encoding="utf-8") as f:
                    cur = f.read()
            except Exception:
                cur = ""
            win = tk.Toplevel(self)
            win.title("编辑 %s · %s" % (name, platform_value))
            win.geometry("820x600")
            txt = tk.Text(win, wrap="word")
            txt.pack(fill="both", expand=True, padx=10, pady=10)
            txt.insert("1.0", cur)

            def save():
                try:
                    mem.edit_memory_file(name, txt.get("1.0", "end-1c"))
                    messagebox.showinfo("提示", "%s 已保存（已记账）" % name)
                    win.destroy()
                except Exception as e:
                    messagebox.showwarning("提示", "保存失败: %r" % (e,))

            bar = ttk.Frame(win)
            bar.pack(fill="x", padx=10, pady=(0, 10))
            ttk.Button(bar, text="保存", command=save).pack(side="left")
            ttk.Button(bar, text="取消", command=win.destroy).pack(side="left", padx=8)
        except Exception as e:
            messagebox.showwarning("提示", "打开编辑器失败: %r" % (e,))

    def _append_correction(self, platform_value: str) -> None:
        try:
            from tkinter import simpledialog
            chat = simpledialog.askstring("更正记忆", "会话 key（如 wechat:wxid_xxx）:")
            if not chat:
                return
            seq = simpledialog.askstring("更正记忆", "要更正的 seq（可留空，只追加说明）:")
            note = simpledialog.askstring("更正记忆", "更正/补充内容:")
            if not note:
                return
            mem = self._echo_mem(platform_value)
            ref = "log:%s#seq=%s" % (chat, seq or "?")
            mem.append(chat, "note", "更正 %s: %s" % (ref, note))
            mem.record("memory.correction", chat, {"seq": seq or "", "note": note[:200]})
            messagebox.showinfo("提示", "已追加更正记录（一手层只追加，不改原文）")
        except Exception as e:
            messagebox.showwarning("提示", "追加更正失败: %r" % (e,))

    def _shared_memory_enabled(self) -> bool:
        try:
            return bool(self.memory_shared_var.get())
        except Exception:
            return False

    def _echo_mem(self, platform_value: str):
        from ..memory.chat_memory import ContactMemory
        return ContactMemory(platform_value, log=lambda m: None,
                             shared=self._shared_memory_enabled())

    def _open_memory_dir(self, platform_value: str) -> None:
        try:
            from ..core.paths import platform_memory_dir
            os.startfile(platform_memory_dir("wechat" if self._shared_memory_enabled() else platform_value))
        except Exception as e:
            messagebox.showwarning("提示", "打开记忆目录失败: %r" % (e,))

    def _open_memory_root(self) -> None:
        try:
            from ..core.paths import memory_dir
            os.startfile(memory_dir())
        except Exception as e:
            messagebox.showwarning("提示", "打开记忆根目录失败: %r" % (e,))

    @staticmethod
    @staticmethod
    def _doctor_text(d: dict) -> str:
        return "\n".join([
            "平台: %s" % d.get("platform"),
            "目录: %s" % d.get("root"),
            "",
            "对话轮数: %s" % d.get("turns"),
            "联系人记忆数: %s（有摘要 %s）" % (d.get("chats"), d.get("summaries")),
            "原始日志行数: %s" % d.get("lines"),
            "摘要总字数: %s" % d.get("summary_chars"),
            "墨账本: %s 条" % ((d.get("ledger") or {}).get("records")),
        ])

    def _echo_doctor(self, which: str = "all") -> None:
        try:
            plats = ["wechat", "qq"] if which == "all" else [which]
            text = "\n\n".join(self._doctor_text(self._echo_mem(p).doctor()) for p in plats)
            messagebox.showinfo("ECHO 记忆体检", text)
        except Exception as e:
            messagebox.showwarning("提示", "记忆体检失败: %r" % (e,))

    def _echo_rebuild(self, which: str = "all") -> None:
        try:
            plats = ["wechat", "qq"] if which == "all" else [which]
            stats = []
            for p in plats:
                mem = self._echo_mem(p)
                try:
                    marker = os.path.join(mem.root, ".migrated_v1")
                    if os.path.isfile(marker):
                        os.remove(marker)
                except Exception:
                    pass
                mem.migrate_legacy()
                d = mem.doctor()
                stats.append("%s: %s 条日志" % (p, (d.get("L3") or {}).get("lines")))
            messagebox.showinfo("ECHO 重建索引", "旧历史已导入一手层：\n" + "\n".join(stats))
        except Exception as e:
            messagebox.showwarning("提示", "重建索引失败: %r" % (e,))

    def _build_platform_bar(self, parent) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x")
        self.wx_status = ttk.Label(row, text="微信: 未启用", anchor="w")
        self.wx_status.pack(side="left", padx=(0, 6))
        self.wx_btn = ttk.Button(row, text="启动微信", width=9,
                                 command=lambda: self._toggle_platform(Platform.WECHAT))
        self.wx_btn.pack(side="left", padx=(0, 14))
        self.qq_status = ttk.Label(row, text="QQ: 未启用", anchor="w")
        self.qq_status.pack(side="left", padx=(0, 6))
        self.qq_btn = ttk.Button(row, text="启动QQ", width=9,
                                 command=lambda: self._toggle_platform(Platform.QQ))
        self.qq_btn.pack(side="left")
        self.refresh_platform_status()

    def _toggle_platform(self, platform) -> None:
        if self.bot_controller is None:
            return
        try:
            if self.bot_controller.is_running(platform):
                self.bot_controller.stop(platform)
            else:
                self.bot_controller.start(platform)
        except Exception:
            pass
        self.refresh_platform_status()

    def refresh_platform_status(self) -> None:
        if self.bot_controller is None:
            self.wx_status.config(text="微信: 未连接")
            self.qq_status.config(text="QQ: 未连接")
            self.wx_btn.config(state="disabled")
            self.qq_btn.config(state="disabled")
            return
        for platform, label, status_widget, btn_widget in (
                (Platform.WECHAT, "微信", self.wx_status, self.wx_btn),
                (Platform.QQ, "QQ", self.qq_status, self.qq_btn)):
            try:
                running = self.bot_controller.is_running(platform)
            except Exception:
                running = False
            status_widget.config(text="%s: %s" % (label, "运行中" if running else "已停止"))
            btn_widget.config(text=("停止" if running else "启动") + label, state="normal")

    def _build_stickers(self) -> None:
        bar = ttk.Frame(self.sticker_tab)
        bar.pack(fill="x")
        ttk.Label(bar, text="平台").pack(side="left", padx=(0, 4))
        self.sticker_platform = ttk.Combobox(bar, values=["微信", "QQ"], width=6, state="readonly")
        self.sticker_platform.set("微信")
        self.sticker_platform.pack(side="left", padx=(0, 8))
        ttk.Label(bar, text="收藏表情（云端，非本地保存）").pack(side="left")
        ttk.Button(bar, text="收藏最近收到的表情", command=self._favorite_last).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="刷新收藏表情", command=self._refresh_faces).pack(side="right")

        local_bar = ttk.Frame(self.sticker_tab)
        local_bar.pack(fill="x", pady=(8, 0))
        ttk.Label(local_bar, text="微信本地表情库（扫描聊天记录，未收藏的自定义表情）").pack(side="left")
        self.auto_save_stickers_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(local_bar, text="收到新表情自动保存",
                        variable=self.auto_save_stickers_var).pack(side="left", padx=(10, 0))
        self.scan_chat_btn = ttk.Button(
            local_bar, text="扫描聊天表情（最近5会话×120条）",
            command=self._scan_chat_stickers)
        self.scan_chat_btn.pack(side="right")
        self.scan_status = ttk.Label(local_bar, text="", foreground="#666")
        self.scan_status.pack(side="left", padx=(12, 0))
        try:
            from ..core.paths import data_home as _data_home
            _ei_hint = os.path.join(_data_home(), "EMOJE", "<wxid>", "chat_stickers")
        except Exception:
            _ei_hint = "EMOJE\\<wxid>\\chat_stickers"
        ttk.Label(self.sticker_tab,
                  text="保存到 %s；只是本地下载，不会真的收藏进微信账号。" % _ei_hint,
                  foreground="#888").pack(anchor="w", pady=(2, 0))

        cols = ("name", "url")
        tree_frame = ttk.Frame(self.sticker_tab)
        tree_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.face_tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=20)
        for c, t, w in [("name", "名称", 220), ("url", "描述 / 标签", 420)]:
            self.face_tree.heading(c, text=t)
            self.face_tree.column(c, width=w, anchor="w")
        self.face_tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.face_tree.yview)
        self.face_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.face_tree.bind("<<TreeviewSelect>>", self._on_sticker_select)
        self.sticker_platform.bind("<<ComboboxSelected>>",
                                   lambda _e: self._refresh_faces())

        self._wechat_sticker_rows = {}
        preview = ttk.Frame(self.sticker_tab)
        preview.pack(fill="x", pady=(8, 0))
        self.sticker_preview = ttk.Label(preview, text="选择表情查看预览", anchor="center",
                                         relief="sunken", padding=8, width=48)
        self.sticker_preview.pack(side="left", padx=(0, 10))
        self.sticker_meta = ttk.Label(preview, text="", justify="left", wraplength=430)
        self.sticker_meta.pack(side="left", fill="both", expand=True)

        ttk.Label(self.sticker_tab,
                  text="微信：列表来自本地 wechat_fav（真实收藏）+ chat_stickers（扫描/自动保存）。"
                       "选中后可预览、编辑描述/标签；标签会参与发送时的表情挑选。"
                       "QQ：以上为云端收藏列表。",
                  wraplength=560, justify="left").pack(anchor="w", pady=(6, 0))

        ttk.Button(bar, text="编辑描述/标签",
                   command=self._edit_sticker_annotation).pack(side="right", padx=(6, 0))
        self.relabel_btn = ttk.Button(bar, text="重新扫描表情包",
                                      command=self._relabel_stickers)
        self.relabel_btn.pack(side="right", padx=(6, 0))

    def _sticker_platform(self) -> Platform:
        return _PLATFORM_VALUES.get(self.sticker_platform.get(), Platform.QQ)

    def _refresh_faces(self) -> None:
        if self._sticker_platform() == Platform.WECHAT:
            self._refresh_wechat_local()
            return
        if not self.sticker_fetcher:
            messagebox.showwarning("提示", "当前环境未提供收藏表情接口")
            return
        try:
            faces = self.sticker_fetcher(self._sticker_platform())
        except Exception as exc:
            messagebox.showerror("获取失败", f"拉取收藏表情失败：{exc}")
            return
        for i in self.face_tree.get_children():
            self.face_tree.delete(i)
        self._wechat_sticker_rows.clear()
        self.face_tree.heading("name", text="名称")
        self.face_tree.heading("url", text="链接")
        self.face_tree.column("name", width=220)
        self.face_tree.column("url", width=420)
        self.sticker_preview.configure(image="", text="QQ 云端表情无本地预览")
        self.sticker_meta.configure(text="")
        if not faces:
            messagebox.showinfo("无收藏表情", "QQ 云端暂无收藏表情")
            return
        for f in faces:
            name = f.get("FaceName") or f.get("name") or f.get("FileName") or ""
            url = f.get("FileUrl") or f.get("url") or f.get("Url") or ""
            self.face_tree.insert("", "end", values=(name, url))

    def _refresh_wechat_local(self, show_empty: bool = True) -> None:
        """微信：显示本地真实收藏 + 扫描/自动保存的自定义表情，带预览。"""
        try:
            from ..integration.account_store import active_emoji_images_dir
            base = active_emoji_images_dir()
        except Exception:
            base = None
        for i in self.face_tree.get_children():
            self.face_tree.delete(i)
        self._wechat_sticker_rows.clear()
        self.face_tree.heading("name", text="来源 / 文件")
        self.face_tree.heading("url", text="描述 / 标签")
        self.face_tree.column("name", width=260)
        self.face_tree.column("url", width=420)
        self.sticker_preview.configure(image="", text="选择表情查看预览")
        self.sticker_meta.configure(text="")
        if not base:
            if show_empty:
                messagebox.showwarning("未找到表情库", "当前账号表情库尚未建立，请先点扫描或检查微信登录。")
            return
        catalogs = [
            (os.path.join(base, "wechat_fav", "catalog.json"), "收藏"),
            (os.path.join(base, "chat_stickers", "catalog.json"), "本地"),
        ]
        total = 0
        for cat_path, source in catalogs:
            try:
                with open(cat_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            for item in data.get("items") or []:
                path = str(item.get("file") or "")
                if not path or not os.path.isfile(path):
                    continue
                ext = os.path.splitext(path)[1].lower()
                mid = str(item.get("id") or os.path.splitext(os.path.basename(path))[0])
                label = "[%s] %s%s" % (source, mid[:10], ext)
                desc = str(item.get("desc") or item.get("caption") or "")
                tags = "、".join(str(t) for t in (item.get("tags") or []) if str(t).strip())
                summary = desc + (("  |  " + tags) if tags else "")
                if not summary:
                    summary = "(未标注)"
                iid = self.face_tree.insert("", "end", values=(label, summary))
                self._wechat_sticker_rows[iid] = {
                    "catalog": cat_path, "id": mid, "source": source, "item": item,
                }
                total += 1
        if total == 0 and show_empty:
            messagebox.showinfo("本地表情库为空",
                                "没有找到本地表情。请先点“扫描聊天表情”或等待收到表情自动保存。")
        self.sticker_meta.configure(text="本地表情共 %d 个；选中后可预览、编辑描述/标签。" % total)

    def _on_sticker_select(self, _event=None) -> None:
        sel = self.face_tree.selection()
        if not sel:
            return
        info = self._wechat_sticker_rows.get(sel[0])
        if not info:
            return
        item = info["item"]
        path = str(item.get("file") or "")
        if not path or not os.path.isfile(path):
            self.sticker_preview.configure(image="", text="文件不存在")
            return
        if _HAS_PIL:
            try:
                im = Image.open(path)
                im.thumbnail((360, 360))
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGB")
                self._preview_photo = ImageTk.PhotoImage(im)
                self.sticker_preview.configure(image=self._preview_photo, text="")
            except Exception as exc:
                self.sticker_preview.configure(image="", text="预览失败: %s" % exc)
        desc = str(item.get("desc") or item.get("caption") or "")
        tags = "、".join(str(t) for t in (item.get("tags") or []) if str(t).strip())
        self.sticker_meta.configure(
            text="来源：%s\n文件：%s\n描述：%s\n标签：%s"
                 % (info["source"], path, desc or "(未标注)", tags or "(无)"))

    def _edit_sticker_annotation(self) -> None:
        sel = self.face_tree.selection()
        info = self._wechat_sticker_rows.get(sel[0]) if sel else None
        if not info:
            messagebox.showwarning("请选择表情", "请先在微信表情列表里选中一个表情。")
            return
        item = info["item"]
        win = tk.Toplevel(self)
        win.title("编辑表情描述/标签")
        win.transient(self)
        win.grab_set()
        win.resizable(False, False)
        ttk.Label(win, text="描述（它表达的情绪/含义）").grid(row=0, column=0, padx=8, pady=(10, 2), sticky="w")
        desc_var = tk.StringVar(value=str(item.get("desc") or item.get("caption") or ""))
        desc_entry = ttk.Entry(win, textvariable=desc_var, width=52)
        desc_entry.grid(row=1, column=0, padx=8, pady=(0, 8))
        ttk.Label(win, text="标签（用逗号分隔，如：开心, 嘲讽, 无语）").grid(row=2, column=0, padx=8, pady=(0, 2), sticky="w")
        tags_var = tk.StringVar(value=",".join(str(t) for t in (item.get("tags") or [])))
        tags_entry = ttk.Entry(win, textvariable=tags_var, width=52)
        tags_entry.grid(row=3, column=0, padx=8, pady=(0, 8))
        desc_entry.focus_set()

        def save():
            desc = desc_var.get().strip()
            tags = [t.strip() for t in tags_var.get().replace("，", ",").split(",") if t.strip()]
            try:
                with open(info["catalog"], "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {"items": []}
            for it in data.get("items") or []:
                if str(it.get("id") or "") == str(info["id"]):
                    it["desc"] = desc
                    it["tags"] = tags
                    break
            tmp = info["catalog"] + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, info["catalog"])
            item["desc"] = desc
            item["tags"] = tags
            summary = desc + (("  |  " + "、".join(tags)) if tags else "")
            self.face_tree.item(sel[0], values=(self.face_tree.item(sel[0], "values")[0],
                                                summary or "(未标注)"))
            self.sticker_meta.configure(
                text="来源：%s\n文件：%s\n描述：%s\n标签：%s"
                     % (info["source"], item.get("file", ""), desc or "(未标注)",
                        "、".join(tags) or "(无)"))
            win.destroy()
            messagebox.showinfo("已保存", "描述和标签已写入本地 catalog。")

        ttk.Button(win, text="保存", command=save).grid(row=4, column=0, padx=8, pady=(0, 10), sticky="e")

    def _favorite_last(self) -> None:
        if not self.sticker_favoriter:
            messagebox.showwarning("提示", "当前环境未提供收藏接口")
            return
        ok, msg = self.sticker_favoriter(self._sticker_platform())
        if ok:
            messagebox.showinfo("已收藏", msg)
            self._refresh_faces()
        else:
            messagebox.showwarning("未收藏", msg)

    def _relabel_stickers(self) -> None:
        """用聊天主 AI（视觉模型）重新识别并标注本地全部表情包。"""
        if self._sticker_platform() != Platform.WECHAT:
            messagebox.showinfo(
                "提示", "重新扫描表情包针对微信本地表情库（收藏表情 + 聊天表情）；"
                        "QQ 列表是云端收藏，不含本地文件。")
            return
        cfg = getattr(self, "_cfg", None)
        if cfg is None:
            messagebox.showwarning("提示", "配置尚未加载，请稍后再试。")
            return
        try:
            from ..emoji.relabel import local_sticker_stats
            stats = local_sticker_stats()
        except Exception:
            stats = {}
        total = int(stats.get("total") or 0)
        if total <= 0:
            messagebox.showinfo("没有可扫描的表情",
                                "本地表情库为空，请先点“扫描聊天表情”或等待收到新表情。")
            return
        if not messagebox.askyesno(
                "重新扫描表情包",
                "将先重新扫描最近聊天记录里的表情，再用聊天主 AI 重新识别并标注本地全部表情：\n"
                "收藏表情 %d 张 + 聊天表情 %d 张，共 %d 张（如扫描到新的会更多）。\n\n"
                "每张都会调用一次视觉模型，可能需要几分钟并消耗 API 额度。继续吗？"
                % (int(stats.get("fav") or 0), int(stats.get("chat") or 0), total)):
            return
        btn = getattr(self, "relabel_btn", None)
        if btn is not None:
            btn.configure(state="disabled")
        try:
            self.scan_status.config(text="正在重新扫描并标注全部表情……")
        except Exception:
            pass

        def on_progress(done, total_n, phase, described):
            try:
                self.after(0, lambda: self.scan_status.config(
                    text="%s：%d/%d（成功 %d）" % (phase, done, total_n, described)))
            except Exception:
                pass

        def worker():
            try:
                from ..emoji.relabel import relabel_all_stickers
                result = relabel_all_stickers(cfg, progress=on_progress)
            except Exception as exc:
                result = {"error": repr(exc)}

            def done():
                if btn is not None:
                    btn.configure(state="normal")
                try:
                    if result.get("error"):
                        self.scan_status.config(text="重新扫描失败：%s" % result["error"])
                    else:
                        fav = result.get("wechat_fav") or {}
                        chat = result.get("chat_stickers") or {}
                        self.scan_status.config(
                            text="重新标注完成：收藏 %d/%d，聊天表情 %d/%d"
                                 % (fav.get("described", 0), fav.get("pending", 0),
                                    chat.get("described", 0), chat.get("pending", 0)))
                        self._refresh_wechat_local(show_empty=False)
                except Exception:
                    pass

            try:
                self.after(0, done)
            except Exception:
                pass

        threading.Thread(target=worker, name="relabel-stickers", daemon=True).start()

    def _scan_chat_stickers(self) -> None:
        """最近 5 个会话 × 120 条消息，扫描未收藏的动画表情到本地库。"""
        btn = getattr(self, "scan_chat_btn", None)
        if btn is not None:
            btn.configure(state="disabled")

        def worker():
            try:
                from ..integration.chat_stickers import scan_chat_stickers
                result = scan_chat_stickers(session_limit=5, msg_limit=120)
            except Exception as exc:
                result = {"ret": -99, "error": repr(exc)}

            def done():
                if btn is not None:
                    btn.configure(state="normal")
                if result.get("ret") == 0:
                    try:
                        if self._sticker_platform() == Platform.WECHAT:
                            self._refresh_wechat_local(show_empty=False)
                    except Exception:
                        pass
                    try:
                        self.scan_status.config(
                            text="扫描完成：消息 %d，表情 %d，新下载 %d，已存在 %d，失败 %d"
                                 % (result.get("scanned_messages", 0),
                                    result.get("found", 0), result.get("downloaded", 0),
                                    result.get("existing", 0), result.get("failed", 0)))
                    except Exception:
                        pass
                else:
                    try:
                        self.scan_status.config(text="扫描失败：%s" % (result.get("error") or result))
                    except Exception:
                        pass

            try:
                self.after(0, done)
            except Exception:
                pass

        threading.Thread(target=worker, name="chat-sticker-scan", daemon=True).start()

    # ------------------------------------------------------------------ #
    # 白名单操作
    # ------------------------------------------------------------------ #
    def _wechat_contacts(self, force: bool = False):
        if not force and self._wl_contacts_cache is not None:
            return self._wl_contacts_cache
        if not self.chat_fetcher:
            return []
        try:
            self._wl_contacts_cache = self.chat_fetcher(Platform.WECHAT) or []
        except Exception:
            self._wl_contacts_cache = []
        return self._wl_contacts_cache

    def _resolve_wechat_id(self, entry: WhitelistEntry):
        """把旧配置里的显示名同步成 wxid / @chatroom。"""
        if entry.platform != Platform.WECHAT:
            return
        ids = {c[0] for c in self._wechat_contacts()}
        if entry.chat_id in ids:
            return
        try:
            from ..integration.wechat_contacts import resolve_contact
            hit = resolve_contact(entry.chat_name or entry.chat_id)
            if hit:
                if not entry.chat_name or entry.chat_name == entry.chat_id:
                    entry.chat_name = str(hit.get("name") or entry.chat_name)
                entry.chat_id = str(hit.get("id") or entry.chat_id)
                entry.is_group = bool(hit.get("is_group"))
        except Exception:
            pass

    def _fill_whitelist(self, entries: List[WhitelistEntry]) -> None:
        for i in self.wl_tree.get_children():
            self.wl_tree.delete(i)
        self._wl_original_names = {}
        has_wechat = any(e.platform == Platform.WECHAT for e in entries)
        if has_wechat:
            self._wechat_contacts()  # 触发一次扫描/刷新缓存
        for e in entries:
            self._resolve_wechat_id(e)
            token = "%s:%s" % (e.platform.value, e.chat_id)
            self._wl_original_names[token] = e.chat_name
            display = e.chat_name
            for g in self._memory_groups:
                if token in [str(x) for x in (g.get("chats") or [])]:
                    display = str(g.get("name") or "记忆组")
                    break
            self.wl_tree.insert("", "end", values=(
                _PLATFORM_LABELS.get(e.platform, e.platform.value),
                display, e.chat_id, "群聊" if e.is_group else "私聊",
            ))

    def _refresh_wl_display(self) -> None:
        try:
            for iid in self.wl_tree.get_children():
                v = self.wl_tree.item(iid, "values")
                pl = _PLATFORM_VALUES.get(v[0], Platform.QQ)
                token = "%s:%s" % (pl.value, v[2])
                display = self._wl_original_names.get(token, v[1])
                for g in self._memory_groups:
                    if token in [str(x) for x in (g.get("chats") or [])]:
                        display = str(g.get("name") or "记忆组")
                        break
                self.wl_tree.item(iid, values=(v[0], display, v[2], v[3]))
        except Exception:
            pass

    def _refresh_contacts(self) -> None:
        chats = self._wechat_contacts(force=True)
        if not chats:
            messagebox.showinfo("未扫描到", "未扫描到好友/群（请确认微信已登录且数据库可读）")
            return
        hit = 0
        for iid in self.wl_tree.get_children():
            v = self.wl_tree.item(iid, "values")
            if _PLATFORM_VALUES.get(v[0]) != Platform.WECHAT:
                continue
            try:
                from ..integration.wechat_contacts import resolve_contact
                item = resolve_contact(v[1] or v[2])
            except Exception:
                item = None
            if item:
                self.wl_tree.item(iid, values=(v[0], item.get("name") or v[1],
                                               item.get("id") or v[2],
                                               "群聊" if item.get("is_group") else "私聊"))
                hit += 1
        messagebox.showinfo("扫描完成", f"已扫描 {len(chats)} 个好友/群，同步 {hit} 条白名单 ID")

    def _search_whitelist(self) -> None:
        platform = _PLATFORM_VALUES.get(self.add_platform.get(), Platform.QQ)
        q = (self.wl_search.get() or "").strip()
        if platform == Platform.QQ:
            chats = self.chat_fetcher(platform) if self.chat_fetcher else []
            q_low = q.casefold()
            chats = [c for c in (chats or []) if q_low in str(c[1]).casefold()]
            if not chats:
                messagebox.showinfo("未找到", f"没有找到包含「{q}」的 QQ 会话")
                return
            self._show_pick_dialog(platform, chats)
            return
        try:
            from ..integration.wechat_contacts import get_contacts
            data = get_contacts(force=True)
        except Exception as exc:
            messagebox.showerror("扫描失败", f"读取好友/群失败：{exc}")
            return
        q_low = q.casefold()
        matched = []
        for g in (data.get("groups") or []):
            fields = [str(g.get("name") or ""), str(g.get("nick_name") or ""), str(g.get("remark") or "")]
            if not q or any(q_low in f.casefold() for f in fields if f):
                matched.append((str(g.get("id")), str(g.get("name") or g.get("id")), True))
        for f in (data.get("friends") or []):
            fields = [str(f.get("name") or ""), str(f.get("nick_name") or ""), str(f.get("remark") or "")]
            if not q or any(q_low in x.casefold() for x in fields if x):
                matched.append((str(f.get("id")), str(f.get("name") or f.get("id")), False))
        if not matched:
            messagebox.showinfo("未找到", f"没有找到昵称/备注包含「{q}」的好友或群")
            return
        self._show_pick_dialog(platform, matched)

    def _collect_whitelist(self) -> List[WhitelistEntry]:
        out = []
        for iid in self.wl_tree.get_children():
            v = self.wl_tree.item(iid, "values")
            pl = _PLATFORM_VALUES.get(v[0], Platform.QQ)
            token = "%s:%s" % (pl.value, v[2])
            out.append(WhitelistEntry(
                platform=pl,
                chat_name=self._wl_original_names.get(token, v[1]),
                chat_id=v[2], is_group=(v[3] == "群聊"),
            ))
        return out

    def _add_entry(self, platform: Platform, chat_id: str, name: str, is_group: bool) -> None:
        if not chat_id:
            messagebox.showwarning("提示", "请先选择或填写会话标识")
            return
        self.wl_tree.insert("", "end", values=(
            _PLATFORM_LABELS.get(platform, platform.value),
            name or chat_id, chat_id, "群聊" if is_group else "私聊",
        ))

    def _remove_whitelist(self) -> None:
        for iid in self.wl_tree.selection():
            self.wl_tree.delete(iid)

    # ---- 记忆合并（把多个白名单会话合并为一个记忆/会话；未合并的保持独立） ----
    def _selected_wl_tokens(self):
        tokens = []
        for iid in self.wl_tree.selection():
            v = self.wl_tree.item(iid, "values")
            try:
                pl = _PLATFORM_VALUES.get(v[0], Platform.QQ).value
            except Exception:
                pl = "qq"
            if v[2]:
                tokens.append("%s:%s" % (pl, v[2]))
        return tokens

    def _refresh_memory_hint(self) -> None:
        try:
            groups = [g for g in self._memory_groups if (g.get("chats") or [])]
            if not groups:
                text = "记忆合并：未合并（每个会话独立记忆/会话）"
            else:
                parts = ["%s（%d 个会话）" % (g.get("name") or g.get("id"),
                                             len(g.get("chats") or [])) for g in groups]
                text = "记忆合并：" + "；".join(parts)
            self.wl_mem_hint.configure(text=text)
        except Exception:
            pass

    def _link_memory_selected(self) -> None:
        tokens = self._selected_wl_tokens()
        if len(tokens) < 2:
            messagebox.showwarning("提示", "请在白名单里选中至少 2 个会话，再点“打通记忆”")
            return
        name = simpledialog.askstring("合并记忆", "给合并后的会话起个名字：", parent=self)
        if not name:
            return
        name = name.strip() or "合并会话"
        # 记录成员是否为群聊，供记忆组 65%/35% 权重使用
        members = []
        for iid in self.wl_tree.selection():
            v = self.wl_tree.item(iid, "values")
            try:
                pl = _PLATFORM_VALUES.get(v[0], Platform.QQ).value
            except Exception:
                pl = "qq"
            if v[2]:
                members.append({"chat": "%s:%s" % (pl, v[2]),
                                "is_group": (v[3] == "群聊")})
        for g in self._memory_groups:
            g["chats"] = [c for c in (g.get("chats") or []) if c not in tokens]
        self._memory_groups = [g for g in self._memory_groups if g.get("chats")]
        self._memory_groups.append({"id": uuid.uuid4().hex[:8], "name": name,
                                    "chats": tokens, "members": members})
        self._refresh_memory_hint()
        self._refresh_wl_display()
        try:
            self._save()
            group = self._memory_groups[-1] if self._memory_groups else None
            if group:
                self._run_memory_action("merge", dict(group))
            messagebox.showinfo("已合并", "已把 %d 个会话合并为记忆组「%s」。\n组摘要正在后台生成，完成后会显示在下方。" % (
                len(tokens), name))
        except Exception as e:
            messagebox.showwarning("保存失败", "合并已加入界面但保存失败：%r" % (e,))

    def _unlink_memory_selected(self) -> None:
        tokens = self._selected_wl_tokens()
        if not tokens:
            messagebox.showinfo("提示", "请先选中要解除合并的会话")
            return
        affected = []
        for g in self._memory_groups:
            if any(c in (g.get("chats") or []) for c in tokens):
                affected.append(dict(g))
        for g in self._memory_groups:
            g["chats"] = [c for c in (g.get("chats") or []) if c not in tokens]
        self._memory_groups = [g for g in self._memory_groups if g.get("chats")]
        self._refresh_memory_hint()
        self._refresh_wl_display()
        try:
            self._save()
            for group in affected:
                g2 = dict(group)
                g2["split_tokens"] = [c for c in (group.get("chats") or []) if c in tokens]
                self._run_memory_action("split", g2)
            messagebox.showinfo("已解除", "已解除选中会话的记忆合并；记忆正在拆分回各成员。")
        except Exception as e:
            messagebox.showwarning("保存失败", "已解除界面状态但保存失败：%r" % (e,))

    def _run_memory_action(self, action: str, group: dict) -> None:
        """UI 触发立即合并/拆分；后台执行完成后刷新组摘要预览。"""
        if self.memory_group_action is None:
            if getattr(self, "wl_group_preview", None) is not None:
                self.wl_group_preview.configure(text="记忆组预览：未连接运行中的记忆子智能体")
            return

        def worker():
            try:
                text = self.memory_group_action(action, dict(group)) or ""
            except Exception as exc:
                text = "记忆组操作失败：%r" % (exc,)
            try:
                self.after(0, lambda: self._show_group_preview(group, text))
            except Exception:
                pass

        threading.Thread(target=worker, name="memory-group-ui", daemon=True).start()

    def _show_group_preview(self, group: dict, text: str) -> None:
        try:
            name = str(group.get("name") or group.get("id") or "记忆组")
            preview = str(text or "").strip()
            if len(preview) > 500:
                preview = preview[:500] + "…"
            self.wl_group_preview.configure(
                text=("记忆组「%s」摘要：\n%s" % (name, preview or "（暂无）")))
        except Exception:
            pass

    def _pick_from_list(self) -> None:
        platform = _PLATFORM_VALUES.get(self.add_platform.get(), Platform.QQ)
        if not self.chat_fetcher:
            messagebox.showwarning("提示", "当前环境未提供会话列表")
            return
        try:
            chats = self.chat_fetcher(platform)
        except Exception as exc:
            messagebox.showerror("获取失败", f"拉取会话列表失败：{exc}")
            return
        if not chats:
            messagebox.showinfo("无会话", "没有拉取到会话（请确认机器人已登录 / 微信已登录）")
            return
        self._show_pick_dialog(platform, chats)

    def _show_pick_dialog(self, platform: Platform, chats) -> None:
        win = tk.Toplevel(self)
        win.title("选择会话")
        win.geometry("500x430")
        win.transient(self.winfo_toplevel())
        win.grab_set()
        label = "微信" if platform == Platform.WECHAT else "QQ"
        ttk.Label(win, text=f"选择要加入白名单的{label}会话（双击即可加入）").pack(anchor="w", padx=8, pady=6)
        cols = ("name", "chat_id", "kind")
        tree = ttk.Treeview(win, columns=cols, show="headings")
        for c, t, w in [("name", "名称", 200), ("chat_id", "标识", 180), ("kind", "类型", 80)]:
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w")
        tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(0, 8))
        sb = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, 8), pady=(0, 8))
        for cid, name, is_group in chats:
            tree.insert("", "end", values=(name, cid, "群聊" if is_group else "私聊"))

        def on_pick(_event=None):
            sel = tree.selection()
            if not sel:
                return
            v = tree.item(sel[0], "values")
            self._add_entry(platform, v[1], v[0], v[2] == "群聊")
            win.destroy()

        tree.bind("<Double-1>", on_pick)
        ttk.Button(win, text="加入白名单", command=on_pick).pack(pady=(0, 8))

    def _show_manual_add(self) -> None:
        platform = _PLATFORM_VALUES.get(self.add_platform.get(), Platform.QQ)
        win = tk.Toplevel(self)
        win.title("手动添加")
        win.geometry("380x190")
        win.transient(self.winfo_toplevel())
        win.grab_set()
        f = ttk.Frame(win, padding=10)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="会话名").grid(row=0, column=0, sticky="w", pady=4)
        name_e = ttk.Entry(f)
        name_e.grid(row=0, column=1, sticky="ew", pady=4)
        hint = "QQ号 / 群号" if platform == Platform.QQ else "会话名 / wxid"
        ttk.Label(f, text=hint).grid(row=1, column=0, sticky="w", pady=4)
        id_e = ttk.Entry(f)
        id_e.grid(row=1, column=1, sticky="ew", pady=4)
        group_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="群聊", variable=group_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=4)

        def ok():
            self._add_entry(platform, id_e.get().strip(), name_e.get().strip(), group_var.get())
            win.destroy()

        ttk.Button(f, text="添加", command=ok).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    def _on_style_change(self, *_args) -> None:
        if self.style_var.get() == "自定义":
            self.instruction_entry.configure(state="normal")
        else:
            self.instruction_entry.configure(state="disabled")

    # ------------------------------------------------------------------ #
    # 数据装载 / 收集
    # ------------------------------------------------------------------ #
    def _load_ai(self, key: str, ai: PlatformAIConfig) -> None:
        # 底层提示词绑定 memory/<平台>MM/SELF.md
        pv = "wechat" if key == "wx" else "qq"
        try:
            mem = self._echo_mem(pv)
            self_path = os.path.join(mem.root, "SELF.md")
            content = ""
            if os.path.isfile(self_path):
                content = open(self_path, "r", encoding="utf-8").read()
            if not str(content or "").strip():
                content = str(ai.system_prompt or "")
                os.makedirs(mem.root, exist_ok=True)
                with open(self_path, "w", encoding="utf-8") as f:
                    f.write(content)
        except Exception:
            content = str(ai.system_prompt or "")
        box = getattr(self, f"{key}_self_prompt", None)
        if box is not None:
            box.delete("1.0", tk.END)
            box.insert("1.0", content)
        getattr(self, f"{key}_key").delete(0, tk.END)
        getattr(self, f"{key}_key").insert(0, ai.api_key)
        getattr(self, f"{key}_base").delete(0, tk.END)
        getattr(self, f"{key}_base").insert(0, ai.base_url)
        getattr(self, f"{key}_model").set(ai.model)

    def _collect_ai(self, key: str) -> PlatformAIConfig:
        box = getattr(self, f"{key}_self_prompt", None)
        system_prompt = box.get("1.0", tk.END).strip() if box is not None else ""
        pv = "wechat" if key == "wx" else "qq"
        try:
            mem = self._echo_mem(pv)
            os.makedirs(mem.root, exist_ok=True)
            with open(os.path.join(mem.root, "SELF.md"), "w", encoding="utf-8") as f:
                f.write(system_prompt)
        except Exception:
            pass
        # SELF.md 与 config.system_prompt 是两个独立注入层：UI 只改 SELF.md，
        # config.system_prompt 保持原值单独注入。
        config_prompt = ""
        try:
            if getattr(self, "_cfg", None) is not None:
                pv2 = "wechat" if key == "wx" else "qq"
                config_prompt = str(self._cfg.ai_for(Platform(pv2)).system_prompt or "")
        except Exception:
            config_prompt = ""
        return PlatformAIConfig(
            system_prompt=config_prompt,
            api_key=getattr(self, f"{key}_key").get().strip(),
            base_url=getattr(self, f"{key}_base").get().strip() or "https://api.deepseek.com",
            model=getattr(self, f"{key}_model").get().strip() or "deepseek-chat",
        )

    def load(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._load_ai("wx", cfg.wechat_ai)
        self._load_ai("qq", cfg.qq_ai)
        self._memory_groups = [dict(g) for g in (getattr(cfg, "memory_groups", []) or [])
                               if isinstance(g, dict)]
        self._fill_whitelist(cfg.whitelist)
        self._refresh_memory_hint()
        self.style_var.set(cfg.reply_style.name)
        self.instruction_entry.configure(state="normal")
        self.instruction_entry.delete(0, tk.END)
        self.instruction_entry.insert(0, cfg.reply_style.instruction)
        self._on_style_change()
        self.strategy_var.set(_STRATEGY_LABELS.get(cfg.frequency_strategy, _STRATEGY_LABELS[FrequencyStrategy.INTERVAL]))
        self.freq_value.delete(0, tk.END)
        self.freq_value.insert(0, str(cfg.frequency_value))
        self.wechat_on.set(cfg.wechat_enabled)
        self.qq_on.set(cfg.qq_enabled)
        self.hook_send_on.set(bool(getattr(cfg, "wechat_hook_enabled", False)))
        self.screen_send_on.set(bool(getattr(cfg, "wechat_screen_send", False)))
        self.auto_inject_on.set(bool(getattr(cfg, "wechat_auto_inject", True)))
        self.lock_input_var.set(bool(getattr(cfg, "wechat_lock_input_when_minimized", False)))
        self.qq_ws.delete(0, tk.END)
        self.qq_ws.insert(0, cfg.qq_onebot_ws_url)
        self.emoji_on.set(cfg.emoji_enabled)
        self.emoji_dir.delete(0, tk.END)
        self.emoji_dir.insert(0, cfg.emoji_images_dir)
        self.split_on.set(bool(getattr(cfg, "reply_split_enabled", True)))
        for _box, _val in ((self.split_min, getattr(cfg, "reply_split_min_chars", 80) or 80),
                           (self.split_max, getattr(cfg, "reply_split_max_parts", 4) or 4)):
            _box.delete(0, tk.END)
            _box.insert(0, str(int(_val)))
        self.auto_save_stickers_var.set(bool(getattr(cfg, "wechat_auto_save_stickers", True)))
        from ..core.paths import platform_memory_dir
        shared_mem = bool(getattr(cfg, "memory_shared", False))
        self.memory_shared_var.set(shared_mem)
        for entry, pv in ((self.memory_wx_dir, "wechat"), (self.memory_qq_dir, "qq")):
            entry.delete(0, tk.END)
            entry.insert(0, platform_memory_dir("wechat" if shared_mem else pv))
        for pv, attr in (("wechat", "ledger_wechat_entry"), ("qq", "ledger_qq_entry")):
            try:
                ent = getattr(self, attr)
                ent.delete(0, tk.END)
                ent.insert(0, self._ledger_obj(pv).dir)
            except Exception:
                pass
        self.memory_budget.delete(0, tk.END)
        self.memory_budget.insert(0, str(getattr(cfg, "memory_compact_budget_chars", 8000) or 8000))
        self.memory_keep.delete(0, tk.END)
        self.memory_keep.insert(0, str(getattr(cfg, "memory_compact_keep_chars", 4000) or 4000))

    def collect(self) -> AppConfig:
        cfg = AppConfig()
        # 保留设置页没有控件的 harness 字段，避免 UI 保存时被默认值覆盖
        base = getattr(self, "_cfg", None)
        if base is not None:
            cfg.harness_enabled = bool(getattr(base, "harness_enabled", True))
            cfg.harness_backend = str(getattr(base, "harness_backend", "native") or "native")
            cfg.harness_url = str(getattr(base, "harness_url", "") or "")
            cfg.harness_timeout = int(getattr(base, "harness_timeout", 30) or 30)
        cfg.wechat_ai = self._collect_ai("wx")
        cfg.qq_ai = self._collect_ai("qq")
        style_name = self.style_var.get()
        cfg.reply_style = ReplyStyle(
            name=style_name,
            instruction=self.instruction_entry.get().strip() if style_name == "自定义" else "",
        )
        cfg.frequency_strategy = _LABEL_STRATEGY.get(self.strategy_var.get(), FrequencyStrategy.INTERVAL)
        try:
            cfg.frequency_value = max(0.1, float(self.freq_value.get()))
        except (ValueError, TypeError):
            cfg.frequency_value = 10.0
        cfg.whitelist = self._collect_whitelist()
        cfg.memory_groups = [dict(g) for g in self._memory_groups]
        cfg.wechat_enabled = self.wechat_on.get()
        cfg.qq_enabled = self.qq_on.get()
        cfg.wechat_hook_enabled = self.hook_send_on.get()
        cfg.wechat_screen_send = self.screen_send_on.get()
        cfg.wechat_auto_inject = self.auto_inject_on.get()
        cfg.wechat_lock_input_when_minimized = self.lock_input_var.get()
        cfg.qq_onebot_ws_url = self.qq_ws.get().strip() or "ws://127.0.0.1:3001"
        cfg.emoji_enabled = self.emoji_on.get()
        cfg.emoji_images_dir = self.emoji_dir.get().strip() or "assets/emoji"
        cfg.reply_split_enabled = bool(self.split_on.get())
        try:
            cfg.reply_split_min_chars = max(20, int(self.split_min.get()))
        except (ValueError, TypeError):
            cfg.reply_split_min_chars = 80
        try:
            cfg.reply_split_max_parts = max(1, min(6, int(self.split_max.get())))
        except (ValueError, TypeError):
            cfg.reply_split_max_parts = 4
        cfg.wechat_auto_save_stickers = self.auto_save_stickers_var.get()
        cfg.memory_file_path = ""
        try:
            cfg.memory_shared = bool(self.memory_shared_var.get())
        except Exception:
            cfg.memory_shared = False
        try:
            cfg.memory_compact_budget_chars = max(1000, int(self.memory_budget.get()))
        except (ValueError, TypeError):
            cfg.memory_compact_budget_chars = 8000
        try:
            cfg.memory_compact_keep_chars = max(200, int(self.memory_keep.get()))
        except (ValueError, TypeError):
            cfg.memory_compact_keep_chars = 4000
        return cfg

    def _save(self) -> None:
        cfg = self.collect()
        if self.on_save:
            self.on_save(cfg)
        else:
            messagebox.showinfo("已保存", "设置已保存")
        try:
            for pv in ("wechat", "qq"):
                self._ledger_obj(pv).record("config.save", "config.json", {
                    "compact_budget": int(getattr(cfg, "memory_compact_budget_chars", 0) or 0),
                    "compact_keep": int(getattr(cfg, "memory_compact_keep_chars", 0) or 0)},
                    platform=pv)
        except Exception:
            pass
