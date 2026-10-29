# -*- coding: utf-8 -*-
"""Process-wide bridge to the active native DSH-compatible core.

Legacy helper modules (emoji descriptors, sticker picker, image notes) call
these functions so every AI request goes through the in-exe native core.
"""
from __future__ import annotations

import threading
from typing import Dict, Optional

_lock = threading.RLock()
_core = None
_team = None


def set_core(core) -> None:
    global _core
    with _lock:
        _core = core


def clear_core() -> None:
    global _core, _team
    with _lock:
        _core = None
        _team = None


def set_team(team) -> None:
    global _team
    with _lock:
        _team = team


def get_core():
    with _lock:
        return _core


def get_team():
    with _lock:
        return _team


def available() -> bool:
    return get_core() is not None


def ask(system_prompt: str, user_message: str, platform="wechat",
        max_tokens: int = 600, images=None) -> Optional[str]:
    core = get_core()
    if core is None:
        return None
    try:
        return core.complete(system_prompt, user_message, platform=platform,
                             max_tokens=max_tokens, images=images)
    except Exception:
        return None


def vision(system_prompt: str, image_path: str, platform="wechat",
           max_tokens: int = 500) -> Optional[str]:
    core = get_core()
    if core is None:
        return None
    try:
        return core.vision(system_prompt, image_path, platform=platform,
                           max_tokens=max_tokens)
    except Exception:
        return None


def sticker_describe(image_path: str, platform="wechat", hint: str = "") -> Optional[Dict]:
    """Agent-team sticker labeling (action/expression + tags/text), 30~50 chars."""
    team = get_team()
    if team is None:
        return None
    try:
        return team.describe_sticker(image_path, hint=hint, platform=platform)
    except Exception:
        return None
