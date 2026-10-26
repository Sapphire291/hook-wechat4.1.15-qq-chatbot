# -*- coding: utf-8 -*-
"""插件面板：唯一「插件」入口。

布局：
- 大标题「插件」
- 左侧：mod/ 内可读取运行的插件列表（可滚动）
- 右侧：该插件的 API 接口（含刷新模型/保存/自检）+ 插件内容（可滚动）+ 预留可滚动对话框
"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk
from typing import List, Optional

from ..plugins.api import PluginBase


class PluginPanel(ttk.Frame):
    def __init__(self, master, plugins: Optional[List[PluginBase]] = None, **kwargs):
        super().__init__(master, **kwargs)
        self.plugins: List[PluginBase] = list(plugins or [])
        self.current: Optional[PluginBase] = None
        self._qr_photo = None
        self._api_widgets = {}
        self._content_holder = None
        self._build()
        self._fill_list()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        ttk.Label(self, text="插件", font=("", 16, "bold")).pack(anchor="w", pady=(0, 8))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        # ---- 左侧：插件列表（可滚动） ----
        left = ttk.Frame(body)
        left.pack(side="left", fill="y", padx=(0, 10))
        ttk.Label(left, text="可用插件").pack(anchor="w")
        list_frame = ttk.Frame(left)
        list_frame.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(list_frame, width=24, exportselection=False)
        lsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=lsb.set)
        self.listbox.pack(side="left", fill="y", expand=True)
        lsb.pack(side="right", fill="y")
        self.listbox.bind("<<ListboxSelect>>", lambda e: self._on_select())
        ttk.Button(left, text="刷新插件", command=self.refresh).pack(fill="x", pady=(6, 0))

        # ---- 右侧 ----
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)

        self.info_label = ttk.Label(right, text="请在左侧选择插件", foreground="#666")
        self.info_label.pack(anchor="w")

        # API 接口区
        self.api_frame = ttk.LabelFrame(right, text="API 接口")
        self.api_frame.pack(fill="x", pady=(6, 6))
        self._build_api_section(self.api_frame)

        # 插件内容（可滚动）
        ttk.Label(right, text="插件内容").pack(anchor="w")
        content_outer = ttk.Frame(right)
        content_outer.pack(fill="both", expand=True)
        self._content_canvas = tk.Canvas(content_outer, highlightthickness=0, height=180)
        csb = ttk.Scrollbar(content_outer, orient="vertical", command=self._content_canvas.yview)
        self._content_canvas.configure(yscrollcommand=csb.set)
        csb.pack(side="right", fill="y")
        self._content_canvas.pack(side="left", fill="both", expand=True)
        self._content_holder = ttk.Frame(self._content_canvas, padding=6)
        win = self._content_canvas.create_window((0, 0), window=self._content_holder, anchor="nw")
        self._content_holder.bind(
            "<Configure>",
            lambda e: self._content_canvas.configure(scrollregion=self._content_canvas.bbox("all")))
        self._content_canvas.bind(
            "<Configure>",
            lambda e: self._content_canvas.itemconfigure(win, width=e.width))

        # 对话框（可滚动，预留）
        ttk.Label(right, text="对话框（预留，可滚动）").pack(anchor="w", pady=(6, 0))
        dialog_frame = ttk.Frame(right)
        dialog_frame.pack(fill="both", expand=True, pady=(2, 0))
        self.dialog = tk.Text(dialog_frame, height=6, wrap="word", state="disabled")
        dsb = ttk.Scrollbar(dialog_frame, orient="vertical", command=self.dialog.yview)
        self.dialog.configure(yscrollcommand=dsb.set)
        self.dialog.pack(side="left", fill="both", expand=True)
        dsb.pack(side="right", fill="y")

    # ------------------------------------------------------------------ #
    def _build_api_section(self, parent) -> None:
        grid = ttk.Frame(parent, padding=6)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="API Key").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=2)
        key_entry = ttk.Entry(grid, show="*")
        key_entry.grid(row=0, column=1, sticky="ew", pady=2)

        ttk.Label(grid, text="Base URL").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=2)
        base_entry = ttk.Entry(grid)
        base_entry.grid(row=1, column=1, sticky="ew", pady=2)

        ttk.Label(grid, text="模型").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=2)
        model_box = ttk.Combobox(grid)
        model_box.grid(row=2, column=1, sticky="ew", pady=2)

        self._api_widgets = {"key": key_entry, "base": base_entry, "model": model_box}
        btn_row = ttk.Frame(grid)
        btn_row.grid(row=0, column=2, rowspan=3, sticky="ns", padx=(8, 0))
        ttk.Button(btn_row, text="保存 API", command=self._save_api).pack(fill="x", pady=(0, 3))
        ttk.Button(btn_row, text="刷新模型", command=self._refresh_models).pack(fill="x", pady=(0, 3))
        ttk.Button(btn_row, text="自检", command=self._self_check).pack(fill="x")

        self.status_label = ttk.Label(parent, text="", foreground="#666", padding=(6, 0, 6, 6))
        self.status_label.pack(anchor="w")

    # ------------------------------------------------------------------ #
    def _fill_list(self) -> None:
        self.listbox.delete(0, "end")
        for plugin in self.plugins:
            self.listbox.insert("end", getattr(plugin, "title", None) or getattr(plugin, "name", "插件"))
        if self.plugins:
            self.listbox.selection_set(0)
            self._on_select()

    def refresh(self) -> None:
        self._fill_list()

    def _on_select(self) -> None:
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx < 0 or idx >= len(self.plugins):
            return
        self._load_plugin(self.plugins[idx])

    def _load_plugin(self, plugin: PluginBase) -> None:
        self.current = plugin
        title = getattr(plugin, "title", None) or getattr(plugin, "name", "")
        desc = getattr(plugin, "description", "")
        self.info_label.config(text=f"{title}  v{getattr(plugin, 'version', '0.0.0')}  {desc}")
        self._api_widgets["key"].delete(0, tk.END)
        self._api_widgets["key"].insert(0, plugin.api.get("api_key", ""))
        self._api_widgets["base"].delete(0, tk.END)
        self._api_widgets["base"].insert(0, plugin.api.get("base_url", ""))
        self._api_widgets["model"].set(plugin.api.get("model", ""))
        self._clear_content()
        try:
            plugin.build_content(self._content_holder)
        except Exception:
            ttk.Label(self._content_holder, text="（插件内容加载失败）", foreground="#b33").pack(anchor="w")
        self.log(f"已加载插件：{title}")

    def _clear_content(self) -> None:
        if self._content_holder is None:
            return
        for child in self._content_holder.winfo_children():
            child.destroy()

    # ------------------------------------------------------------------ #
    def log(self, text: str) -> None:
        try:
            self.dialog.configure(state="normal")
            self.dialog.insert(tk.END, text + "\n")
            self.dialog.see(tk.END)
            self.dialog.configure(state="disabled")
        except Exception:
            pass

    def _read_api(self) -> None:
        if self.current is None:
            return
        self.current.api["api_key"] = self._api_widgets["key"].get().strip()
        self.current.api["base_url"] = self._api_widgets["base"].get().strip()
        self.current.api["model"] = self._api_widgets["model"].get().strip()

    def _save_api(self) -> None:
        if self.current is None:
            return
        self._read_api()
        self.current.save_api()
        self._set_status("API 已保存", ok=True)

    def _set_status(self, text: str, ok: bool = True) -> None:
        try:
            self.status_label.config(text=text, foreground="#2a7d2a" if ok else "#b33")
        except Exception:
            pass
        self.log(text)

    def _refresh_models(self) -> None:
        if self.current is None:
            return
        self._read_api()
        try:
            from ..ai.deepseek import fetch_models
            models = fetch_models(self.current.api.get("api_key", ""), self.current.api.get("base_url", ""))
        except Exception as exc:
            self._set_status(f"获取模型失败：{exc}", ok=False)
            return
        if models:
            self._api_widgets["model"].configure(values=models)
            self._api_widgets["model"].set(models[0])
            self._set_status(f"已获取 {len(models)} 个模型", ok=True)
        else:
            self._set_status("未获取到模型列表（检查 API Key / Base URL）", ok=False)

    def _self_check(self) -> None:
        if self.current is None:
            return
        try:
            ok, msg = self.current.check()
        except Exception as exc:
            ok, msg = False, f"自检异常：{exc}"
        self._set_status(msg, ok=ok)

    # ------------------------------------------------------------------ #
    def poll(self) -> None:
        if self.current is not None:
            try:
                poll_fn = getattr(self.current, "poll", None)
                if callable(poll_fn):
                    poll_fn()
            except Exception:
                pass
