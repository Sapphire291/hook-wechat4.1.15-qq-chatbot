# -*- coding: utf-8 -*-
"""主界面：回复记录 + 预警事件 + 设置，以及启动/停止机器人。"""
from __future__ import annotations

import datetime
import os
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable

from ..core.config import ConfigStore
from ..core.events import EventBus
from ..core.models import AppConfig, Platform, ReplyRecord, SecurityEvent
from ..core.records import RecordsStore
from ..core.paths import data_dir
from .alerts import AlertWindow
from .settings import SettingsPanel


class BotController:
    """由 main.py 注入的机器人运行时控制接口（按平台独立控制）。"""

    def __init__(self, start_fn, stop_fn, is_running, status_fn):
        self._start = start_fn          # start_fn(platform)
        self._stop = stop_fn            # stop_fn(platform)
        self._is_running = is_running   # is_running(platform) -> bool
        self._status = status_fn

    def start(self, platform) -> None:
        self._start(platform)

    def stop(self, platform) -> None:
        self._stop(platform)

    def is_running(self, platform) -> bool:
        return bool(self._is_running(platform))

    def status_text(self) -> str:
        return self._status()


class ChatBotApp:
    """主窗口应用。"""

    def __init__(self, root: tk.Tk, config_store: ConfigStore, records: RecordsStore,
                 bus: EventBus, bot: BotController, initial_config: AppConfig, chat_fetcher=None,
                 sticker_fetcher=None, sticker_favoriter=None, memory_store=None,
                 plugins=None, memory_group_action=None):
        self.root = root
        self.config_store = config_store
        self.records = records
        self.bus = bus
        self.bot = bot
        self.config = initial_config
        self.chat_fetcher = chat_fetcher
        self.sticker_fetcher = sticker_fetcher
        self.sticker_favoriter = sticker_favoriter
        self.memory_store = memory_store
        self.memory_group_action = memory_group_action
        self.plugins = plugins or []
        self.alert_window = None
        self.input_guard = None
        self._wechat_autostart_pending = True

        self.root.title("聊天机器人")
        self.root.geometry("860x560")

        self._build_menu()
        self._build_notebook()
        self._build_statusbar()

        # 订阅事件总线：实时刷新 + 预警弹窗
        self.bus.on_reply(self._on_new_reply)
        self.bus.on_alert(self._on_new_alert)

        # 载入历史
        self._refresh_replies()
        self._refresh_alerts()
        self._refresh_status()
        self._apply_input_guard()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- 主窗口关闭 / 输入隔离 ----
    def _input_guard_log(self, msg: str) -> None:
        try:
            with open(os.path.join(data_dir(), "input_guard.log"), "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (__import__("time").strftime("%Y-%m-%d %H:%M:%S"), msg))
        except Exception:
            pass

    def _apply_input_guard(self) -> None:
        enabled = bool(getattr(self.config, "wechat_lock_input_when_minimized", False))
        if enabled and self.input_guard is None:
            try:
                from ..integration.wechat_isolator import WeChatInputIsolator
                self.input_guard = WeChatInputIsolator(
                    lambda: bool(getattr(self.config, "wechat_lock_input_when_minimized", False)),
                    self._input_guard_log)
                self.input_guard.start()
            except Exception as exc:
                self._input_guard_log("start failed %r" % (exc,))
                self.input_guard = None
        elif not enabled and self.input_guard is not None:
            try:
                self.input_guard.stop()
            except Exception:
                pass
            self.input_guard = None

    def _on_close(self) -> None:
        try:
            if self.input_guard is not None:
                self.input_guard.stop()
        except Exception:
            pass
        self.root.destroy()

    # ---- 构建 ----
    def _build_menu(self) -> None:
        bar = tk.Menu(self.root)
        view_menu = tk.Menu(bar, tearoff=0)
        view_menu.add_command(label="打开预警窗口", command=self._open_alert_window)
        view_menu.add_command(label="刷新记录", command=self._refresh_all)
        bar.add_cascade(label="视图", menu=view_menu)
        self.root.config(menu=bar)

    def _build_notebook(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=6)

        # 标签页 1：回复记录
        self.reply_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.reply_frame, text="回复记录")
        cols = ("time", "chat", "content")
        self.reply_tree = ttk.Treeview(self.reply_frame, columns=cols, show="headings")
        for c, title, w in [("time", "时间", 150), ("chat", "目标会话", 220), ("content", "回复内容", 420)]:
            self.reply_tree.heading(c, text=title)
            self.reply_tree.column(c, width=w, anchor="w", stretch=(c == "content"))
        self.reply_tree.pack(side="left", fill="both", expand=True)
        rsb = ttk.Scrollbar(self.reply_frame, orient="vertical", command=self.reply_tree.yview)
        self.reply_tree.configure(yscrollcommand=rsb.set)
        rsb.pack(side="right", fill="y")

        # 标签页 2：预警事件
        self.alert_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.alert_frame, text="预警事件")
        acols = ("time", "type", "sender", "chat", "content")
        self.alert_tree = ttk.Treeview(self.alert_frame, columns=acols, show="headings")
        for c, title, w in [("time", "时间", 140), ("type", "类型", 90), ("sender", "发起人", 110),
                            ("chat", "会话", 140), ("content", "内容", 360)]:
            self.alert_tree.heading(c, text=title)
            self.alert_tree.column(c, width=w, anchor="w", stretch=(c == "content"))
        self.alert_tree.pack(side="left", fill="both", expand=True)
        asb = ttk.Scrollbar(self.alert_frame, orient="vertical", command=self.alert_tree.yview)
        self.alert_tree.configure(yscrollcommand=asb.set)
        asb.pack(side="right", fill="y")

        # 标签页 3：设置
        self.settings_panel = SettingsPanel(self.notebook, on_save=self._save_settings,
                                            chat_fetcher=self.chat_fetcher,
                                            sticker_fetcher=self.sticker_fetcher,
                                            sticker_favoriter=self.sticker_favoriter,
                                                                            memory_store=self.memory_store,
                                            wechat_plugin=self._find_wechat_plugin(),
                                            bot_controller=self.bot,
                                            memory_group_action=self.memory_group_action)
        self.notebook.add(self.settings_panel, text="设置")
        self.settings_panel.load(self.config)
        try:
            self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        except Exception:
            pass

        # 单一「插件」标签页（在「设置」右侧）：左侧插件列表 + 右侧 API/自检/对话框
        self.plugin_panel = None
        try:
            from .plugin_panel import PluginPanel
            self.plugin_panel = PluginPanel(self.notebook, self.plugins)
            self.notebook.add(self.plugin_panel, text="插件")
        except Exception:
            self.plugin_panel = None

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self.root)
        bar.pack(side="bottom", fill="x", padx=6, pady=4)

        ttk.Label(bar, text="微信/QQ 的启动、停止按钮已移到「设置」页底部，保存设置上方。",
                  anchor="w", foreground="#888").pack(side="left")
        ttk.Button(bar, text="预警窗口", command=self._open_alert_window).pack(side="right")

    # ---- 回调 ----
    def _on_new_reply(self, rec: ReplyRecord) -> None:
        self.reply_tree.insert("", 0, values=(
            datetime.datetime.fromtimestamp(rec.timestamp).strftime("%Y-%m-%d %H:%M:%S"),
            f"[{rec.platform.value}] {rec.chat_name or rec.chat_id}",
            rec.content,
        ))

    def _on_new_alert(self, evt: SecurityEvent) -> None:
        # 主界面「预警事件」标签页实时插入
        self.alert_tree.insert("", 0, values=self._alert_values(evt))
        # 独立预警窗口：同步刷新并置前
        win = self._get_alert_window()
        win.refresh(self.records.recent_alerts())
        win.deiconify()
        win.lift()

    def _alert_values(self, evt: SecurityEvent):
        return (
            datetime.datetime.fromtimestamp(evt.timestamp).strftime("%Y-%m-%d %H:%M:%S"),
            "提示词注入" if evt.event_type.value == "injection" else "隐私刺探",
            evt.sender_name or evt.sender_id,
            evt.chat_name or evt.chat_id,
            evt.content,
        )

    def _get_alert_window(self) -> AlertWindow:
        if self.alert_window is None or not self.alert_window.winfo_exists():
            win = AlertWindow(self.root)
            win.protocol("WM_DELETE_WINDOW", win.withdraw)
            self.alert_window = win
        return self.alert_window

    def _open_alert_window(self) -> None:
        win = self._get_alert_window()
        win.refresh(self.records.recent_alerts())
        win.deiconify()
        win.lift()

    def _toggle_platform(self, platform) -> None:
        if self.bot.is_running(platform):
            self.bot.stop(platform)
        else:
            self.bot.start(platform)
        self._refresh_status()

    def _on_tab_changed(self, _evt=None) -> None:
        """切回「设置」页时从 config.json 重新载入，保证界面与文件一致（反之亦然）。"""
        try:
            idx = self.notebook.index(self.notebook.select())
            if self.notebook.tab(idx, "text") != "设置":
                return
            disk = self.config_store.load()
            self.config.update_from(disk)
            self.settings_panel.load(self.config)
        except Exception:
            pass

    def _save_settings(self, cfg: AppConfig) -> None:
        # 原地更新共享配置对象：运行中的各组件持有同一引用，改动即时生效
        self.config.update_from(cfg)
        self.config_store.save(self.config)
        self._apply_input_guard()
        messagebox.showinfo("已保存", "设置已保存，即时生效（提示词/风格/频率/白名单/API Key 等）。")

    # ---- 刷新 ----
    def _refresh_replies(self) -> None:
        for i in self.reply_tree.get_children():
            self.reply_tree.delete(i)
        for rec in reversed(self.records.recent_replies()):
            self.reply_tree.insert("", 0, values=(
                datetime.datetime.fromtimestamp(rec.timestamp).strftime("%Y-%m-%d %H:%M:%S"),
                f"[{rec.platform.value}] {rec.chat_name or rec.chat_id}",
                rec.content,
            ))

    def _refresh_alerts(self) -> None:
        for i in self.alert_tree.get_children():
            self.alert_tree.delete(i)
        for evt in reversed(self.records.recent_alerts()):
            self.alert_tree.insert("", 0, values=self._alert_values(evt))

    def _refresh_all(self) -> None:
        self._refresh_replies()
        self._refresh_alerts()

    def _refresh_status(self) -> None:
        try:
            self.settings_panel.refresh_platform_status()
        except Exception:
            pass

    def _find_wechat_plugin(self):
        """返回 wechat_hook 插件（供「设置 → 微信 AI」页做连接）。"""
        for plugin in self.plugins:
            if getattr(plugin, "name", "") == "wechat_hook":
                return plugin
        return None

    def poll(self) -> None:
        """周期刷新状态（mainloop 定时调用）。"""
        self._refresh_status()
        self._poll_wechat_autostart()
        if getattr(self, "plugin_panel", None) is not None:
            try:
                self.plugin_panel.poll()
            except Exception:
                pass
        self.root.after(1000, self.poll)

    def _poll_wechat_autostart(self) -> None:
        """程序启动时微信还没开：等微信进程出现后自动启动适配器一次。"""
        if not self._wechat_autostart_pending:
            return
        if not bool(getattr(self.config, "wechat_enabled", True)):
            self._wechat_autostart_pending = False
            return
        try:
            if self.bot.is_running(Platform.WECHAT):
                self._wechat_autostart_pending = False
                return
        except Exception:
            pass
        try:
            import psutil
            exists = any((p.info.get("name") or "").lower() == "weixin.exe"
                         for p in psutil.process_iter(["pid", "name"]))
        except Exception:
            exists = False
        if not exists:
            return
        try:
            self.bot.start(Platform.WECHAT)
        except Exception:
            pass
        self._wechat_autostart_pending = False


def run_app(config_store: ConfigStore, records: RecordsStore, bus: EventBus,
            bot: BotController, config: AppConfig, chat_fetcher=None,
            sticker_fetcher=None, sticker_favoriter=None,
            memory_store=None, plugins=None, memory_group_action=None) -> None:
    """启动 GUI 主循环。"""
    root = tk.Tk()
    app = ChatBotApp(root, config_store, records, bus, bot, config, chat_fetcher=chat_fetcher,
                     sticker_fetcher=sticker_fetcher, sticker_favoriter=sticker_favoriter,
                     memory_store=memory_store, plugins=plugins,
                     memory_group_action=memory_group_action)
    app.poll()  # 启动周期状态刷新
    root.mainloop()
