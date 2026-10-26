# -*- coding: utf-8 -*-
"""长期记忆查看/编辑窗口：可查看、添加、修改、删除长期记忆，并可设置记忆文件路径。"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Callable, Optional


class MemoryWindow(tk.Toplevel):
    def __init__(self, master, memory_store,
                 platform_prefix: str = "",
                 on_path_saved: Optional[Callable[[str], None]] = None):
        super().__init__(master)
        self.title("长期记忆")
        self.geometry("820x500")
        self.memory_store = memory_store
        self.platform_prefix = platform_prefix
        self.on_path_saved = on_path_saved
        self.chat_key: Optional[str] = None
        self._chats = []
        self._entries = []
        self._build()
        self._reload_chats()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        bar = ttk.Frame(self, padding=(8, 8, 8, 0))
        bar.pack(fill="x")
        ttk.Label(bar, text="记忆文件路径").pack(side="left")
        self.path_var = tk.StringVar(value=self.memory_store.path)
        ttk.Entry(bar, textvariable=self.path_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(bar, text="应用并重载", command=self._apply_path).pack(side="left")

        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body)
        left.pack(side="left", fill="y", padx=(0, 8))
        ttk.Label(left, text="会话").pack(anchor="w")
        self.chat_list = tk.Listbox(left, width=30, exportselection=False)
        self.chat_list.pack(fill="y", expand=True)
        self.chat_list.bind("<<ListboxSelect>>", lambda e: self._on_chat_select())
        ttk.Button(left, text="新建会话记忆", command=self._new_chat).pack(fill="x", pady=(4, 0))

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        cols = ("idx", "text")
        self.tree = ttk.Treeview(right, columns=cols, show="headings")
        self.tree.heading("idx", text="序号")
        self.tree.column("idx", width=50, anchor="center")
        self.tree.heading("text", text="记忆内容（双击可编辑）")
        self.tree.column("text", width=440, anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self._edit_selected())

        btn_bar = ttk.Frame(right)
        btn_bar.pack(fill="x", pady=(6, 0))
        ttk.Button(btn_bar, text="添加", command=self._add).pack(side="left")
        ttk.Button(btn_bar, text="编辑选中", command=self._edit_selected).pack(side="left", padx=6)
        ttk.Button(btn_bar, text="删除选中", command=self._delete_selected).pack(side="left")
        ttk.Button(btn_bar, text="刷新", command=self._refresh_all).pack(side="right")

    # ------------------------------------------------------------------ #
    def _reload_chats(self) -> None:
        self.chat_list.delete(0, "end")
        chats = self.memory_store.chats()
        if self.platform_prefix:
            chats = [c for c in chats if c.startswith(self.platform_prefix)]
        self._chats = sorted(chats)
        for c in self._chats:
            self.chat_list.insert("end", c)

    def _on_chat_select(self) -> None:
        sel = self.chat_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if 0 <= idx < len(self._chats):
            self.chat_key = self._chats[idx]
            self._reload_entries()

    def _reload_entries(self) -> None:
        self.tree.delete(*self.tree.get_children())
        if not self.chat_key:
            return
        self._entries = self.memory_store.entries(self.chat_key)
        for i, entry in enumerate(self._entries):
            self.tree.insert("", "end", values=(i + 1, entry.get("text", "")))

    def _refresh_all(self) -> None:
        self.memory_store.reload()
        self._reload_chats()
        self._reload_entries()

    # ------------------------------------------------------------------ #
    def _apply_path(self) -> None:
        new_path = self.path_var.get().strip()
        if not new_path:
            messagebox.showwarning("提示", "路径不能为空", parent=self)
            return
        if new_path == self.memory_store.path:
            return
        self.memory_store.path = new_path
        self.memory_store.reload()
        if self.on_path_saved is not None:
            try:
                self.on_path_saved(new_path)
            except Exception:
                pass
        self.chat_key = None
        self._reload_chats()
        self.tree.delete(*self.tree.get_children())
        messagebox.showinfo("已应用", "记忆文件路径已切换并重新加载。", parent=self)

    def _new_chat(self) -> None:
        key = simpledialog.askstring(
            "新建会话记忆", "请输入会话标识（格式：平台:会话ID，例如 qq:1234567890）", parent=self)
        if not key:
            return
        key = key.strip()
        if key not in self.memory_store.chats():
            self.memory_store.add(key, "（占位，可编辑或删除）")
        self._reload_chats()
        self._select_chat(key)

    def _select_chat(self, key: str) -> None:
        if key in self._chats:
            idx = self._chats.index(key)
            self.chat_list.selection_clear(0, "end")
            self.chat_list.selection_set(idx)
            self.chat_list.see(idx)
            self.chat_key = key
            self._reload_entries()

    def _add(self) -> None:
        if not self.chat_key:
            messagebox.showwarning("提示", "请先在左侧选择一个会话", parent=self)
            return
        text = simpledialog.askstring("添加长期记忆", "请输入要记住的内容：", parent=self)
        if text:
            self.memory_store.add(self.chat_key, text)
            self._reload_entries()

    def _selected_index(self) -> int:
        sel = self.tree.selection()
        if not sel:
            return -1
        try:
            return int(self.tree.item(sel[0], "values")[0]) - 1
        except Exception:
            return -1

    def _edit_selected(self) -> None:
        if not self.chat_key:
            return
        idx = self._selected_index()
        if idx < 0 or idx >= len(self._entries):
            return
        old = self._entries[idx].get("text", "")
        text = simpledialog.askstring("编辑长期记忆", "修改内容：", initialvalue=old, parent=self)
        if text is not None:
            self.memory_store.update(self.chat_key, idx, text)
            self._reload_entries()

    def _delete_selected(self) -> None:
        if not self.chat_key:
            return
        idx = self._selected_index()
        if idx < 0 or idx >= len(self._entries):
            return
        if messagebox.askyesno("确认", "确定删除这条长期记忆吗？", parent=self):
            self.memory_store.delete(self.chat_key, idx)
            self._reload_entries()
