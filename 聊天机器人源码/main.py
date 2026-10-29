# -*- coding: utf-8 -*-
"""「聊天机器人」程序入口：组装所有模块并启动 GUI 主界面。"""
import os
import sys
import threading
import time

from src.ai import DeepSeekClient
from src.core.config import ConfigStore
from src.core.events import EventBus, MessageQueue
from src.core.models import Platform
from src.core.pipeline import MessageProcessor
from src.core.records import RecordsStore
from src.emoji.provider import EmojiProviderImpl
from src.integration import create_adapters
from src.logging.logger import setup_logging
from src.memory import HistoryStore, MemoryStore
from src.policy import create_frequency_policy, create_whitelist_policy
from src.security import create_detectors
from src.core.paths import data_dir, memory_dir


def hide_console() -> None:
    """隐藏本进程的控制台窗口（--console 模式打包时用）。

    需要真实控制台的组件（如注入器）必须有父进程控制台，但控制台窗口可以被隐藏，
    用户只看到 GUI。
    """
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


class BotRuntime:
    """机器人运行时：统一管理各平台适配器与消息处理器的启停。"""

    _AVAIL_TTL = 10.0  # 可用性探测缓存时长（秒），避免 UI 高频轮询卡顿

    def __init__(self, config_store, config, queue, adapters, processor):
        self.config_store = config_store
        self.config = config
        self.queue = queue
        self.adapters = adapters
        self.processor = processor
        self._running = {Platform.WECHAT: False, Platform.QQ: False}
        self._lock = threading.Lock()
        self._avail_cache = {}  # platform.value -> (timestamp, bool)

    # ---- 启停（按平台，可独立控制） ----
    def start_platform(self, platform: Platform):
        if platform not in self.adapters:
            return
        with self._lock:
            if self._running.get(platform):
                return
            self._running[platform] = True
        # 接入层启动可能涉及探测（QQ TCP 探测 / 微信窗口附加），放后台线程避免卡 UI
        threading.Thread(target=self._start_worker, args=(platform,),
                         name=f"bot-start-{platform.value}", daemon=True).start()

    def _start_worker(self, platform: Platform):
        adapter = self.adapters[platform]
        adapter.set_on_message(self.queue.put)
        try:
            adapter.start()
        except Exception:
            pass
        self.processor.start()

    def stop_platform(self, platform: Platform):
        if platform not in self.adapters:
            return
        with self._lock:
            if not self._running.get(platform):
                return
            self._running[platform] = False
        try:
            self.adapters[platform].stop()
        except Exception:
            pass

    def is_running(self, platform):
        return bool(self._running.get(platform))

    def start(self):
        for p in list(self.adapters):
            self.start_platform(p)

    def stop(self):
        for p in list(self.adapters):
            self.stop_platform(p)

    @property
    def running(self):
        return any(self._running.values())

    # ---- 状态 ----
    def _available(self, platform: Platform, adapter) -> bool:
        now = time.time()
        key = platform.value
        cached = self._avail_cache.get(key)
        if cached and (now - cached[0]) < self._AVAIL_TTL:
            return cached[1]
        try:
            val = bool(adapter.available())
        except Exception:
            val = False
        self._avail_cache[key] = (now, val)
        return val

    def status_text(self):
        parts = []
        for p in (Platform.WECHAT, Platform.QQ):
            if p not in self.adapters:
                parts.append(f"{p.value}:未启用")
                continue
            state = "运行中" if self._running.get(p) else "已停止"
            avail = "可用" if self._available(p, self.adapters[p]) else "不可用"
            parts.append(f"{p.value}:{state}({avail})")
        return "  |  ".join(parts)


def build_runtime(config_store, config):
    """组装全部组件，返回 (runtime, records, bus, queue)。"""
    records = RecordsStore()
    bus = EventBus()
    queue = MessageQueue()

    adapters = create_adapters(config)
    generators = {
        Platform.WECHAT: DeepSeekClient(config, Platform.WECHAT),
        Platform.QQ: DeepSeekClient(config, Platform.QQ),
    }
    memo_dir = memory_dir()
    memory_path = config.memory_file_path or os.path.join(memo_dir, "memory.json")
    history_dir = config.history_dir or os.path.join(memo_dir, "history")
    memory_store = MemoryStore(memory_path)
    history_store = HistoryStore(history_dir)

    processor = MessageProcessor(
        config=config,
        queue=queue,
        adapters=adapters,
        whitelist=create_whitelist_policy(config),
        frequency=create_frequency_policy(config),
        detectors=create_detectors(config),
        generators=generators,
        records=records,
        bus=bus,
        emoji=EmojiProviderImpl(config),
        memory=memory_store,
        history_store=history_store,
    )
    runtime = BotRuntime(config_store, config, queue, adapters, processor)
    runtime.memory_store = memory_store
    return runtime, records, bus, queue


def make_chat_fetcher(config):
    """返回一个「按平台拉取可选会话列表」的回调，供设置界面白名单选择。"""

    def fetch(platform: Platform):
        if platform == Platform.QQ:
            from src.integration.qq import fetch_qq_chats
            return fetch_qq_chats(config.qq_onebot_ws_url)
        from src.integration.wechat import fetch_wechat_chats
        return fetch_wechat_chats()

    return fetch


def make_sticker_actions(config, adapters):
    """返回 (fetch_faces, favorite_last) 两个回调，供表情包管理界面使用。"""

    def fetch_faces(platform: Platform):
        if platform == Platform.QQ:
            from src.integration.qq import fetch_custom_faces
            return fetch_custom_faces(config.qq_onebot_ws_url)
        from src.integration.wechat import fetch_wechat_faces
        return fetch_wechat_faces()

    def favorite_last(platform: Platform):
        if platform == Platform.QQ:
            adapter = adapters.get(Platform.QQ)
            if adapter is None or not adapter.last_image_message_id:
                return False, "还没有收到过可收藏的表情"
            from src.integration.qq import mark_favorite
            ok = mark_favorite(config.qq_onebot_ws_url, adapter.last_image_message_id)
            return ok, ("已收藏" if ok else "收藏失败")
        adapter = adapters.get(Platform.WECHAT)
        if adapter is None or adapter.last_image_msg is None:
            return False, "还没有收到过可收藏的表情"
        ok = adapter.mark_last_favorite()
        return ok, ("已收藏" if ok else "收藏失败（可能需要微信客户端内确认）")

    return fetch_faces, favorite_last


def find_mod_dir() -> str:
    """返回插件目录：数据根下 mod/（优先），否则 exe 同级 mod/、源码目录 mod/。"""
    from src.core.paths import data_home
    candidates = [os.path.join(data_home(), "mod")]
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(os.path.dirname(sys.executable), "mod"))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mod"))
    for cand in candidates:
        try:
            if cand and os.path.isdir(cand):
                return cand
        except Exception:
            continue
    d = os.path.join(data_home(), "mod")
    os.makedirs(d, exist_ok=True)
    return d


def load_plugins(config, config_store, bus, queue, adapters, memory_store):
    """加载 mod/ 下全部插件；任何失败都不影响主程序。"""
    try:
        from src.plugins import PluginContext, load_plugins as _load
        mod_dir = find_mod_dir()
        context = PluginContext(
            config=config, config_store=config_store, bus=bus, queue=queue,
            adapters=adapters, memory_store=memory_store,
            data_dir=data_dir(), mod_dir=mod_dir,
        )
        return _load(mod_dir, context)
    except Exception:
        return []


def _prepare_wechat(config, config_store) -> None:
    """exe 启动时：自动注入 hook、扫描联系人并同步白名单 ID。"""
    try:
        if bool(getattr(config, "wechat_auto_inject", True)):
            from src.integration.hook_injector import ensure_hook
            ensure_hook()
    except Exception:
        pass
    try:
        from src.integration.wechat_contacts import scan_wechat_contacts, resolve_contact
        scan_wechat_contacts(persist=True)
        changed = False
        for e in (config.whitelist or []):
            if e.platform != Platform.WECHAT:
                continue
            hit = resolve_contact(e.chat_name or e.chat_id)
            if not hit:
                continue
            if e.chat_id != hit.get("id") or (hit.get("name") and e.chat_name != hit.get("name")):
                e.chat_id = str(hit.get("id") or e.chat_id)
                e.chat_name = str(hit.get("name") or e.chat_name)
                e.is_group = bool(hit.get("is_group"))
                changed = True
        if changed:
            config_store.save(config)
    except Exception:
        pass


def _check_paths() -> int:
    """--check-paths：打印/写出当前解析到的所有关键路径（无控制台时写 data/路径自检.txt）。"""
    from src.core.paths import (assets_dir, config_path, data_dir, data_home, emoje_dir,
                                memory_dir, platform_memory_dir, project_root, resolve_path,
                                wid_dir)
    from src.core.config import ConfigStore
    lines = []

    def add(k, v):
        lines.append("%-14s %s" % (k, v))

    add("project_root", project_root())
    add("data_home", data_home())
    add("config", config_path())
    add("data_dir", data_dir())
    add("memory_dir", memory_dir())
    add("wechatMM", platform_memory_dir("wechat"))
    add("QQMM", platform_memory_dir("qq"))
    add("WID", wid_dir())
    add("EMOJE", emoje_dir())
    add("assets", assets_dir())
    try:
        add("mod", find_mod_dir())
    except Exception as exc:
        add("mod", "ERR %r" % (exc,))
    try:
        cfg = ConfigStore().load()
        add("emoji_cfg", str(getattr(cfg, "emoji_images_dir", "")))
        add("emoji_cfg_abs", resolve_path(getattr(cfg, "emoji_images_dir", ""), "assets/emoji"))
        try:
            from src.emoji.provider import EmojiProviderImpl
            add("emoji_effective", EmojiProviderImpl(cfg).images_dir)
        except Exception as exc:
            add("emoji_effective", "ERR %r" % (exc,))
    except Exception as exc:
        add("config_load", "ERR %r" % (exc,))
    try:
        from src.integration.hook_injector import _candidate_dll
        add("hook_dll", _candidate_dll() or "(not found)")
    except Exception as exc:
        add("hook_dll", "ERR %r" % (exc,))
    report = "\n".join(lines) + "\n"
    try:
        p = os.path.join(data_dir(), "路径自检.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(report)
    except Exception:
        pass
    try:
        if sys.stdout is not None:
            print(report)
    except Exception:
        pass
    return 0


def _inject_hook_cli() -> int:
    """--inject-hook：手动注入微信 hook（供 hook\\注入Hook.bat 调用；无控制台时写日志）。"""
    ok = False
    detail = ""
    try:
        from src.integration.hook_injector import ensure_hook
        r = ensure_hook() or {}
        ok = bool(r.get("ok"))
        detail = repr(r)[:600]
    except Exception as exc:
        detail = "ERR %r" % (exc,)
    try:
        from src.core.paths import data_dir
        with open(os.path.join(data_dir(), "hook_inject_cli.log"), "a", encoding="utf-8") as f:
            f.write("%s ok=%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), ok, detail))
    except Exception:
        pass
    try:
        if sys.stdout is not None:
            print("hook inject ok=%s %s" % (ok, detail))
    except Exception:
        pass
    return 0 if ok else 1


def main() -> int:
    if "--check-paths" in sys.argv:
        return _check_paths()
    if "--inject-hook" in sys.argv:
        return _inject_hook_cli()
    if os.environ.get("CHATBOT_HIDE_CONSOLE", "1") != "0":
        hide_console()
    setup_logging()
    config_store = ConfigStore()
    config = config_store.load()
    runtime, records, bus, _queue = build_runtime(config_store, config)
    # 启动即准备：注入 hook + 扫描全部好友/群并保存 ID（后台，不阻塞界面）
    threading.Thread(target=_prepare_wechat, args=(config, config_store),
                     name="wechat-prepare", daemon=True).start()

    try:
        from src.ui.app import BotController, run_app
    except Exception as exc:
        print("无法启动图形界面：", exc)
        print("已进入无界面模式。按 Ctrl+C 退出。")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return 0

    controller = BotController(
        start_fn=runtime.start_platform,
        stop_fn=runtime.stop_platform,
        is_running=runtime.is_running,
        status_fn=runtime.status_text,
    )
    fetch_faces, favorite_last = make_sticker_actions(config, runtime.adapters)

    # 微信适配器自动启动（避免打开程序后忘记点“启动微信”导致不回复）
    if config.wechat_enabled:
        def _autostart_wechat():
            time.sleep(2)
            try:
                runtime.start_platform(Platform.WECHAT)
            except Exception:
                pass
        threading.Thread(target=_autostart_wechat, name="wechat-autostart", daemon=True).start()

    def _memory_group_action(action, group):
        try:
            agent = getattr(getattr(runtime, "processor", None), "harness", None)
            agent = getattr(agent, "memory_agent", None)
            if agent is None:
                return ""
            if action == "merge":
                return agent.merge_now(group)
            return agent.split_now(group)
        except Exception as e:
            return "记忆组操作失败：%r" % (e,)

    run_app(config_store, records, bus, controller, config,
            chat_fetcher=make_chat_fetcher(config),
            sticker_fetcher=fetch_faces,
            sticker_favoriter=favorite_last,
            memory_store=getattr(runtime, "memory_store", None),
            memory_group_action=_memory_group_action,
            plugins=load_plugins(config, config_store, bus, _queue,
                                 runtime.adapters, getattr(runtime, "memory_store", None)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
