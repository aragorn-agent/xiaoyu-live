# -*- coding: utf-8 -*-
"""语音路由：首选 + 回退，保证「直播间永远有声音」。

来源：adapted from lxllxl233/dafeiyu-ai-live src/dafeiyu/voice/router.py (MIT License)。
改动：适配 async synth；result 直接透传 VoiceResult。
"""
from __future__ import annotations

import asyncio
import threading
import time

from voice.base import VoiceResult


class VoiceRouter:
    def __init__(self, voices: dict, primary: str = "edge", fallback: str = "edge") -> None:
        self.voices = dict(voices)
        self.primary = primary
        self.fallback = fallback
        self._lock = threading.Lock()
        self._counts = {"attempts": 0, "ok": 0, "fallbacks": 0, "failed": 0}
        self._last: dict = {}

    def _order(self, voice: str | None) -> list[str]:
        first = (voice or self.primary or "").lower()
        order: list[str] = []
        for name in (first, self.primary, self.fallback):
            if name and name not in order:
                order.append(name)
        return order

    async def speak(self, text: str, voice: str | None = None) -> VoiceResult:
        if (voice or self.primary) in ("off", "none", "silent"):
            return VoiceResult(ok=False, voice="off", error="语音已关闭")
        errors: list[str] = []
        for name in self._order(voice):
            impl = self.voices.get(name)
            if impl is None:
                errors.append(f"{name}: 未安装")
                continue
            try:
                ok = impl.available()
            except Exception:
                ok = False
            if not ok:
                errors.append(f"{name}: 离线")
                continue
            try:
                result = await impl.synth(text)
            except Exception as exc:
                result = VoiceResult(ok=False, voice=name, error=f"{type(exc).__name__}: {exc}")
            if result.ok:
                with self._lock:
                    self._counts["attempts"] += 1
                    self._counts["ok"] += 1
                    if name != (voice or self.primary):
                        self._counts["fallbacks"] += 1
                    self._last = {"voice": name, "duration": round(result.duration, 3),
                                  "cached": result.cached, "at": time.time()}
                return result
            errors.append(f"{name}: {result.error or '合成失败'}")
        with self._lock:
            self._counts["attempts"] += 1
            self._counts["failed"] += 1
            self._last = {"voice": "", "error": "；".join(errors), "at": time.time()}
        return VoiceResult(ok=False, voice="", error="；".join(errors) or "没有可用语音")

    def status(self) -> dict:
        voices = {}
        for name, impl in self.voices.items():
            try:
                voices[name] = impl.status()
            except Exception as exc:  # 状态查询不能弄崩直播
                voices[name] = {"name": name, "available": False, "error": str(exc)}
        with self._lock:
            counts = dict(self._counts)
            last = dict(self._last)
        return {
            "primary": self.primary, "fallback": self.fallback,
            "active": self.primary if voices.get(self.primary, {}).get("available") else self.fallback,
            "voices": voices, "counts": counts, "last": last,
        }


__all__ = ["VoiceRouter"]
