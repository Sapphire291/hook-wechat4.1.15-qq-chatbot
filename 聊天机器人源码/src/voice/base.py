# -*- coding: utf-8 -*-
"""语音链路接口（预留）。

- VoiceTranscriber：语音 → 文本（ASR）。后续可接 FunASR / faster-whisper / 云端 ASR。
- VoiceSynthesizer：文本 → 语音文件（TTS），为将来的语音回复预留。

当前默认实现为空实现（Null），不依赖任何第三方库；接入时只需实现接口并在
``create_transcriber``/``create_synthesizer`` 中按配置返回对应实现。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from abc import ABC, abstractmethod
from typing import Optional

from ..core.paths import memory_dir


class VoiceTranscriber(ABC):
    """语音转文字接口。"""

    name: str = "null"

    @abstractmethod
    def available(self) -> bool:
        """当前是否可用（模型/服务已就绪）。"""

    @abstractmethod
    def transcribe(self, audio_path: str) -> str:
        """把音频文件转成文本；失败返回空串。"""


class NullTranscriber(VoiceTranscriber):
    name = "null"

    def available(self) -> bool:
        return False

    def transcribe(self, audio_path: str) -> str:
        return ""


class VoiceSynthesizer(ABC):
    """文字转语音接口（预留，用于语音回复）。"""

    name: str = "null"

    @abstractmethod
    def available(self) -> bool:
        ...

    @abstractmethod
    def synthesize(self, text: str, out_path: str) -> str:
        """合成语音文件，返回路径；失败返回空串。"""


class NullSynthesizer(VoiceSynthesizer):
    name = "null"

    def available(self) -> bool:
        return False

    def synthesize(self, text: str, out_path: str) -> str:
        return ""


# --------------------------------------------------------------------------- #
# 转写缓存（按音频 md5 缓存，重复语音不重复识别）
# --------------------------------------------------------------------------- #
def cache_path() -> str:
    d = os.path.join(memory_dir(), "voice_cache")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return os.path.join(d, "asr_cache.json")


def _load_cache() -> dict:
    try:
        with open(cache_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def cache_get(audio_md5: str) -> Optional[str]:
    if not audio_md5:
        return None
    return _load_cache().get(audio_md5)


def cache_put(audio_md5: str, text: str) -> None:
    if not audio_md5 or not text:
        return
    data = _load_cache()
    data[audio_md5] = text
    try:
        p = cache_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception:
        pass


def md5_file(path: str) -> str:
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def create_transcriber(config) -> VoiceTranscriber:
    """按配置创建转写器；当前仅返回空实现（预留接入点）。"""
    backend = str(getattr(config, "voice_asr_backend", "none") or "none").lower()
    if backend in ("", "none", "off", "disabled"):
        return NullTranscriber()
    # 预留：if backend == "funasr": return FunASRTranscriber(...)
    # 预留：if backend == "faster_whisper": return FasterWhisperTranscriber(...)
    # 预留：if backend == "cloud": return CloudASRTranscriber(...)
    return NullTranscriber()


def create_synthesizer(config) -> VoiceSynthesizer:
    return NullSynthesizer()
