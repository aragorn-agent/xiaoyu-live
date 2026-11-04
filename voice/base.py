# -*- coding: utf-8 -*-
"""Voice 协议与结果。

来源：adapted from lxllxl233/dafeiyu-ai-live src/dafeiyu/voice/base.py (MIT License)。
本项目改为 async（TTS 走网络/子进程，编排在 asyncio 循环里）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class VoiceResult:
    ok: bool
    voice: str = ""
    url: str = ""                # /static/tts/<file> 之类可直接播放的地址
    path: str = ""               # 本地文件路径
    duration: float = 0.0        # 估算秒数（无解码依赖，按字数估）
    cached: bool = False
    error: str = ""
    segments: list = field(default_factory=list)   # [{text, start_ms, end_ms}]

    def to_dict(self) -> dict:
        return {"ok": self.ok, "voice": self.voice, "url": self.url,
                "duration": round(self.duration, 3), "cached": self.cached,
                "error": self.error, "segments": self.segments}


@runtime_checkable
class Voice(Protocol):
    name: str

    def available(self) -> bool: ...
    async def synth(self, text: str, **opts) -> VoiceResult: ...
    def status(self) -> dict: ...


def now_ms() -> float:
    return time.time() * 1000


__all__ = ["Voice", "VoiceResult", "now_ms"]
