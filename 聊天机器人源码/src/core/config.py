# -*- coding: utf-8 -*-
"""配置读写：默认配置 + JSON 持久化（原子写，线程安全）。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Optional

from .models import AppConfig
from .paths import config_path


class ConfigStore:
    """线程安全的配置存取。"""

    def __init__(self, path: Optional[str] = None):
        self.path = path or config_path()
        self._lock = threading.RLock()

    def load(self) -> AppConfig:
        with self._lock:
            if not os.path.exists(self.path):
                cfg = AppConfig()
                self._save_unlocked(cfg)
                return cfg
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return AppConfig.from_dict(data)
            except Exception:
                # 配置损坏时备份并回退默认
                self._backup_corrupt()
                cfg = AppConfig()
                self._save_unlocked(cfg)
                return cfg

    def save(self, cfg: AppConfig) -> None:
        with self._lock:
            self._save_unlocked(cfg)

    # ---- 内部 ----
    def _save_unlocked(self, cfg: AppConfig) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg.to_dict(), f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def _backup_corrupt(self) -> None:
        try:
            bak = self.path + ".corrupt"
            if os.path.exists(self.path):
                os.replace(self.path, bak)
        except Exception:
            pass
