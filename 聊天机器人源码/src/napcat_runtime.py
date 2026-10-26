# -*- coding: utf-8 -*-
"""NapCat 运行时管理：解压、启动、监控登录、二维码、自动 token、自动配置 OneBot。

让 exe 内置 NapCat（合法复用其编译好的协议模块），运行时自动拉起、自动注入 QQ、
自动登录、自动配好 OneBot 端口，做到「不依赖外部挂载、不用开网页、不用手动填 token」。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import subprocess
import threading
import time
import urllib.request
import zipfile
from typing import Optional, Tuple

from .core.paths import data_dir

logger = logging.getLogger(__name__)


class NapCatRuntime:
    """管理一个内置 NapCat 实例的生命周期。"""

    def __init__(self, config, bundled_zip: Optional[str] = None, qq_exe: str = ""):
        self.config = config
        self.bundled_zip = bundled_zip
        self.qq_exe = qq_exe or self._find_qq()
        # 放在 %APPDATA%\ChatBot\napcat（已验证可正常注入；工作区路径会因
        # Documents 重定向等导致注入器失败）。路径为纯 ASCII。
        appdata = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        self.napcat_dir = os.path.join(appdata, "ChatBot", "napcat")
        self._proc = None
        self.phase = "idle"   # idle/starting/need_login/configuring/connected/failed

    # ------------------------------------------------------------------ #
    # 定位 QQ
    # ------------------------------------------------------------------ #
    @staticmethod
    def _find_qq() -> str:
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
            )
            val, _ = winreg.QueryValueEx(key, "UninstallString")
            winreg.CloseKey(key)
            d = os.path.dirname(val.strip('"'))
            qq = os.path.join(d, "QQ.exe")
            if os.path.isfile(qq):
                return qq
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------ #
    # 解压
    # ------------------------------------------------------------------ #
    def is_extracted(self) -> bool:
        return os.path.isfile(os.path.join(self.napcat_dir, "napcat.mjs"))

    def ensure_extracted(self) -> bool:
        if self.is_extracted():
            return True
        if not self.bundled_zip or not os.path.isfile(self.bundled_zip):
            return False
        os.makedirs(self.napcat_dir, exist_ok=True)
        try:
            with zipfile.ZipFile(self.bundled_zip) as z:
                z.extractall(self.napcat_dir)
        except Exception:
            return False
        return self.is_extracted()

    # ------------------------------------------------------------------ #
    # 基础配置（token / 自动登录）
    # ------------------------------------------------------------------ #
    def _cfg_dir(self) -> str:
        d = os.path.join(self.napcat_dir, "config")
        os.makedirs(d, exist_ok=True)
        return d

    def configure_base(self) -> None:
        logger.info("configure_base: 开始")
        cfg_dir = self._cfg_dir()
        logger.info("configure_base: config 目录=%s", cfg_dir)
        napcat_path = os.path.join(cfg_dir, "napcat.json")
        # napcat.json 是静态配置：已存在就跳过写入（避免残留句柄锁导致 PermissionError）
        if os.path.isfile(napcat_path):
            logger.info("configure_base: napcat.json 已存在，跳过写入")
            return
        napcat_cfg = {
            "fileLog": False, "consoleLog": True, "fileLogLevel": "debug",
            "consoleLogLevel": "info", "packetBackend": "auto", "packetServer": "",
            "o3HookMode": 1,
            "bypass": {"hook": False, "window": False, "module": False,
                       "process": False, "container": False, "js": False},
        }
        try:
            _write_json(napcat_path, napcat_cfg)
            logger.info("configure_base: napcat.json 已写入")
        except Exception:
            # 写入失败也不能阻断启动（NapCat 会自行生成默认配置）
            logger.warning("configure_base: 写 napcat.json 失败，继续启动", exc_info=True)
        # webui.json 不预写：由 NapCat 首次运行时自动生成（含随机 token），
        # 之后 webui_token() 直接读取该 token，用户无需手动填写。

    def webui_token(self) -> str:
        """读取 NapCat 自动生成的 WebUI token，无需用户手动填写。"""
        try:
            with open(os.path.join(self._cfg_dir(), "webui.json"), "r", encoding="utf-8") as f:
                return str(json.load(f).get("token", "") or "")
        except Exception:
            return ""

    # ------------------------------------------------------------------ #
    # WebUI API（判断 QQ 是否真正登录，而不是只看配置文件是否存在）
    # ------------------------------------------------------------------ #
    def _webui_request(self, method: str, path: str, payload=None,
                       credential: str = "", timeout: float = 3.0):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:6099" + path,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        if credential:
            req.add_header("Authorization", "Bearer " + credential)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")

    def _webui_credential(self) -> str:
        try:
            token = self.webui_token()
            if not token:
                return ""
            digest = hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()
            out = self._webui_request("POST", "/api/auth/login",
                                      {"hash": digest, "totpCode": ""}) or {}
            data = out.get("data") or {}
            return str(data.get("Credential") or data.get("credential") or data.get("token") or "")
        except Exception:
            return ""

    def is_qq_online(self) -> bool:
        """通过 NapCat WebUI 判断 QQ 是否已真正登录（配置文件存在不代表已登录）。"""
        try:
            cred = self._webui_credential()
            if not cred:
                return False
            info = self._webui_request("GET", "/api/QQLogin/GetQQLoginInfo",
                                       credential=cred) or {}
            data = info.get("data") or {}
            uin = str(data.get("uin") or data.get("uid") or data.get("account") or "")
            status = data.get("online")
            if status is None:
                status = data.get("isOnline")
            if status is None:
                status = data.get("status")
            if isinstance(status, bool):
                return status
            return bool(uin)
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # 启动 / 停止
    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        logger.info("start() 进入: is_extracted=%s", self.is_extracted())
        if not self.is_extracted():
            logger.error("start(): napcat 未解压")
            return False
        if not self.qq_exe or not os.path.isfile(self.qq_exe):
            logger.error("start(): QQ.exe 未找到: %s", self.qq_exe)
            return False
        # NapCatWinBootMain.exe 是控制台程序，必须给它真实控制台才能完成注入。
        # 复刻官方 launcher-user.bat：用 %cd% 引用 napcat 目录（bat 内容保持纯 ASCII，
        # 避免 UTF-8 中文路径被 cmd 按 GBK 读取而乱码）。
        bat_path = os.path.join(self.napcat_dir, "launch.bat")
        lines = [
            "@echo off",
            "setlocal DisableDelayedExpansion",
            'cd /d "%~dp0"',
            'set "NAPCAT_PATCH_PACKAGE=%cd%\\qqnt.json"',
            'set "NAPCAT_LOAD_PATH=%cd%\\loadNapCat.js"',
            'set "NAPCAT_INJECT_PATH=%cd%\\NapCatWinBootHook.dll"',
            'set "NAPCAT_LAUNCHER_PATH=%cd%\\NapCatWinBootMain.exe"',
            'set "NAPCAT_MAIN_PATH=%cd%\\napcat.mjs"',
            f'"%NAPCAT_LAUNCHER_PATH%" "{self.qq_exe}" "%NAPCAT_INJECT_PATH%"',
        ]
        try:
            with open(bat_path, "w", encoding="ascii", newline="\r\n") as f:
                f.write("\n".join(lines) + "\n")
            logger.info("已写入启动脚本: %s", bat_path)
        except Exception:
            # 无写权限时退而使用已有的启动脚本
            if os.path.isfile(bat_path):
                logger.warning("写启动脚本失败，使用已有脚本", exc_info=True)
            else:
                logger.exception("写启动脚本失败且无已有脚本")
                return False
        try:
            # 父进程是 --console 模式（启动时隐藏控制台窗口，但控制台真实存在）。
            # 子进程 cmd 继承该真实控制台，NapCatWinBootMain.exe 才能正常完成注入。
            subprocess.Popen(["cmd", "/c", bat_path], cwd=self.napcat_dir)
            logger.info("已通过 cmd 启动 launch.bat")
            return True
        except Exception:
            logger.exception("启动 launch.bat 失败")
            return False

    def stop(self) -> None:
        """关闭 NapCat 注入的 QQ 实例与注入器。"""
        try:
            import subprocess as sp
            sp.run(["taskkill", "/F", "/IM", "NapCatWinBootMain.exe"],
                   capture_output=True, timeout=10)
        except Exception:
            pass
        try:
            import subprocess as sp
            sp.run(["taskkill", "/F", "/FI", "IMAGENAME eq QQ.exe",
                    "/FI", "COMMANDLINE eq *enable-logging*"],
                   capture_output=True, timeout=15)
        except Exception:
            pass

    def refresh_login(self) -> bool:
        """调用 NapCat WebUI API 刷新登录二维码（应对二维码过期）。

        鉴权流程：hash = SHA256(webui_token + ".napcat") -> POST /api/auth/login 拿
        Credential -> 带 Bearer 调 /api/QQLogin/RefreshQRcode。
        """
        import hashlib
        import urllib.request

        try:
            token = self.webui_token()
            if not token:
                logger.warning("刷新二维码失败：未找到 webui token")
                return False
            digest = hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()
            base = "http://127.0.0.1:6099/api"
            body = _json_dump_bytes({"hash": digest, "totpCode": ""})
            req = urllib.request.Request(
                base + "/auth/login", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                login_data = json.loads(resp.read().decode("utf-8"))
            if login_data.get("code") != 0:
                logger.warning("刷新二维码：登录 NapCat WebUI 失败 %s", login_data.get("message"))
                return False
            credential = (login_data.get("data") or {}).get("Credential", "")
            if not credential:
                logger.warning("刷新二维码：未拿到 Credential")
                return False
            req2 = urllib.request.Request(
                base + "/QQLogin/RefreshQRcode", data=b"{}",
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + credential},
                method="POST")
            with urllib.request.urlopen(req2, timeout=60) as resp2:
                refresh_data = json.loads(resp2.read().decode("utf-8"))
            ok = refresh_data.get("code") == 0
            logger.info("刷新二维码结果: code=%s msg=%s",
                        refresh_data.get("code"), refresh_data.get("message"))
            return ok
        except Exception:
            logger.warning("刷新二维码失败", exc_info=True)
            return False

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def is_onebot_ready(self, timeout: float = 0.5) -> bool:
        try:
            s = socket.create_connection(("127.0.0.1", 3001), timeout=timeout)
            s.close()
            return True
        except Exception:
            return False

    def is_webui_ready(self, timeout: float = 0.5) -> bool:
        """NapCat 是否已在运行（WebUI 6099 端口）。"""
        try:
            s = socket.create_connection(("127.0.0.1", 6099), timeout=timeout)
            s.close()
            return True
        except Exception:
            return False

    def qrcode_path(self) -> str:
        return os.path.join(self.napcat_dir, "cache", "qrcode.png")

    def account_config_path(self) -> str:
        """返回当前账号的 onebot11_<uin>.json 路径（登录后生成）。"""
        cfg_dir = self._cfg_dir()
        for name in os.listdir(cfg_dir):
            if name.startswith("onebot11_") and name.endswith(".json"):
                return os.path.join(cfg_dir, name)
        return ""

    # ------------------------------------------------------------------ #
    # 自动配置 OneBot（登录后）
    # ------------------------------------------------------------------ #
    def configure_onebot(self, port: int = 3001) -> bool:
        """在账号的 onebot11_<uin>.json 里写入正向 WebSocket 服务器。"""
        path = self.account_config_path()
        if not path:
            return False
        cfg = _read_json(path) or {}
        ws_servers = [{
            "name": "默认正向WS", "enable": True, "host": "127.0.0.1", "port": port,
            "messagePostFormat": "array", "reportSelfMessage": False, "token": "",
            "enableForcePushEvent": True, "debug": False, "heartInterval": 30000,
        }]
        cfg.setdefault("network", {})["websocketServers"] = ws_servers
        cfg.setdefault("network", {}).setdefault("httpServers", [])
        cfg.setdefault("network", {}).setdefault("httpClients", [])
        cfg.setdefault("network", {}).setdefault("websocketClients", [])
        cfg.setdefault("network", {}).setdefault("httpSseServers", [])
        _write_json(path, cfg)
        return True

    def set_auto_login(self, uin: str) -> None:
        """登录成功后写入自动登录账号，下次启动免扫码。"""
        path = os.path.join(self._cfg_dir(), "webui.json")
        cfg = _read_json(path) or {}
        cfg["autoLoginAccount"] = str(uin)
        _write_json(path, cfg)

    # ------------------------------------------------------------------ #
    # 生命周期（后台线程入口）
    # ------------------------------------------------------------------ #
    def run_lifecycle(self) -> None:
        """启动 NapCat → 等扫码登录 → 自动配 OneBot → 重启 → 等端口。"""
        try:
            self._run_lifecycle()
        except Exception:
            # frozen --windowed 下 stderr 为 None，线程异常会被静默丢弃；
            # 这里显式捕获并写入日志，避免「卡在启动中」无法定位。
            logger.exception("NapCat 生命周期异常")
            self.phase = "failed"

    def _run_lifecycle(self) -> None:
        self.phase = "starting"
        logger.info("NapCat 生命周期启动: napcat_dir=%s qq_exe=%s", self.napcat_dir, self.qq_exe)
        if not self.ensure_extracted():
            logger.error("NapCat 解压失败: %s", self.napcat_dir)
            self.phase = "failed"
            return
        logger.info("ensure_extracted 完成")
        self.configure_base()
        logger.info("configure_base 完成")
        if self.is_webui_ready(0.5):
            # 启动脚本（启动.bat）已经拉起 NapCat，无需重复启动
            logger.info("NapCat 已在运行（WebUI 6099 可达），跳过启动")
        else:
            if not self.start():
                logger.error("NapCat 启动失败（QQ 未找到或注入器启动失败）")
                self.phase = "failed"
                return
            logger.info("start() 返回成功，进入监控循环")

        deadline = time.time() + 180
        restart_attempted = False
        last_online_check = 0.0
        while time.time() < deadline:
            # 已就绪（历史配置）
            if self.is_onebot_ready(0.3):
                self.phase = "connected"
                return
            acct = self.account_config_path()
            now = time.time()
            qq_online = False
            # 限频：NapCat WebUI 登录接口有 loginRate 限制（默认 10 次/分），
            # 这里最多每 10 秒查一次登录态，避免把配额打满导致二维码刷新失败。
            if acct and (now - last_online_check) >= 10.0:
                last_online_check = now
                try:
                    qq_online = bool(self.is_qq_online())
                except Exception:
                    qq_online = False
            if qq_online and not restart_attempted:
                # 已登录，但 OneBot 尚未配置：写入配置并重启一次（仅一次，避免反复扫码）
                restart_attempted = True
                self.phase = "configuring"
                logger.info("检测到 QQ 已登录，写入 OneBot 配置并重启: %s", acct)
                uin = os.path.basename(acct)[len("onebot11_"):-len(".json")]
                self.set_auto_login(uin)
                self.configure_onebot(3001)
                self.stop()
                time.sleep(2)
                self.start()
                t2 = time.time() + 90
                while time.time() < t2:
                    if self.is_onebot_ready(0.5):
                        self.phase = "connected"
                        return
                    time.sleep(1)
                self.phase = "failed"
                logger.error("OneBot 配置后 90 秒内端口未就绪，请检查 NapCat 日志")
                return
            has_qr = os.path.isfile(self.qrcode_path())
            if has_qr and self.phase != "need_login":
                logger.info("NapCat 已生成二维码，等待扫码（登录后自动写入 OneBot 配置）")
            self.phase = "need_login" if has_qr else "starting"
            time.sleep(1.5)

        self.phase = "failed"
        logger.warning("NapCat 登录等待超时（180 秒）")


# ---------------------------------------------------------------------- #
# 辅助
# ---------------------------------------------------------------------- #
def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _json_dump_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")
