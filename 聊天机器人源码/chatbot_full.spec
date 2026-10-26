# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：把 wechatauto 及其重依赖完整收集进 onedir 产物。"""
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

# wechatauto 本体与重依赖（uiautomation / winsdk / cv2 / pyautogui 等）
for pkg in ("wechatauto", "uiautomation", "winsdk", "cv2", "pyautogui", "pyscreeze",
            "zstandard", "cryptography", "comtypes", "wxauto4"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

datas += [("assets", "assets"), ("napcat_bundle.zip", "."), ("hook", "hook")]
hiddenimports += [
    "src.integration.wechat_contacts",
    "src.integration.hook_injector",
    "src.integration.chat_stickers",
    "src.integration.input_guard",
    "src.integration.wechat_isolator",
    "src.web.search",
    "src.web.fetch",
    "src.web.quote",
    "src.web.web_context",
    "psutil",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="聊天机器人",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="聊天机器人",
)
