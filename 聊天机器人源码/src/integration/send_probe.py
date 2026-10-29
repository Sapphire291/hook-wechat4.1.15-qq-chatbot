# -*- coding: utf-8 -*-
"""Low-risk send-path probes.

Writes JSON lines to data/send_probe.log:
- thread / stage / target / current chat / donor / text length / hook response / duration
No message text is stored, only length and a short SHA-1 prefix.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time

_LOCK = threading.Lock()


def _path() -> str:
    try:
        from ..core.paths import data_dir
        return os.path.join(data_dir(), "send_probe.log")
    except Exception:
        return ""


def probe(stage: str, **kw) -> None:
    try:
        p = _path()
        if not p:
            return
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "stage": stage,
               "thread": threading.current_thread().name}
        for k, v in kw.items():
            if k == "text":
                s = str(v or "")
                rec["text_len"] = len(s)
                rec["text_sha1"] = hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]
            else:
                rec[k] = v
        line = json.dumps(rec, ensure_ascii=False)
        with _LOCK:
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass
