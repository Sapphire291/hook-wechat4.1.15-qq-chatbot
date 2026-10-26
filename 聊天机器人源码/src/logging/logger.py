# -*- coding: utf-8 -*-
"""日志初始化：同时输出到控制台与 data/app.log 文本文件。"""
from __future__ import annotations

import logging
import os

from ..core.paths import data_dir


def setup_logging(level: int = logging.INFO) -> str:
    """配置日志，返回日志文件路径。"""
    log_path = os.path.join(data_dir(), "app.log")
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )
    return log_path
