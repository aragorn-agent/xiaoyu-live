# -*- coding: utf-8 -*-
"""EdgeTTS 适配器：免费微软云 TTS，主备链里的兜底引擎。

缓存与多引擎设计来自 Dsh_douyin lib/voice.mjs（设计重写，非复制代码）：
- SHA1(engine|voice|rate|pitch|text) 内容寻址，命中即秒回
- 缓存目录只保留最新 N 个（默认 300），超出删最旧
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path

from core.config import CACHE_DIR
from voice.base import VoiceResult

try:
    import edge_tts
    _IMPORT_OK = True
except ImportError:
    edge_tts = None
    _IMPORT_OK = False

# 中文语速估算：约 4.2 字/秒（rate +0% 基准）；句间停顿 ~260ms
_MS_PER_CHAR = 1000.0 / 4.2
_PAUSE_MS = 260


def estimate_segments(sentences: list[str]) -> list[dict]:
    """按字数比例估句级字幕 timings（廉价方案，后续可换 ASR 对齐）。"""
    segs, t = [], 0.0
    for s in sentences:
        dur = max(0.45, len(s) * _MS_PER_CHAR / 1000.0)
        segs.append({"text": s, "start_ms": int(t * 1000), "end_ms": int((t + dur) * 1000)})
        t += dur + _PAUSE_MS / 1000.0
    return segs


class EdgeVoice:
    name = "edge"

    def __init__(self, voice: str = "zh-CN-XiaoyiNeural", rate: str = "+0%",
                 pitch: str = "+0Hz", cache_dir: str = "data/cache/tts",
                 cache_size: int = 300) -> None:
        self.voice_name = voice
        self.rate = rate
        self.pitch = pitch
        self.cache_dir = Path(cache_dir)
        self.cache_size = max(50, cache_size)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._fail_until = 0.0     # 连续失败后的熔断冷却

    def available(self) -> bool:
        return _IMPORT_OK and time.time() >= self._fail_until

    def _key(self, text: str) -> str:
        raw = f"edge|{self.voice_name}|{self.rate}|{self.pitch}|{text}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]

    async def synth(self, text: str, **opts) -> VoiceResult:
        if not _IMPORT_OK:
            return VoiceResult(ok=False, voice=self.name, error="edge-tts 未安装")
        key = self._key(text)
        out = self.cache_dir / f"{key}.mp3"
        cached = out.exists() and out.stat().st_size > 2048
        if not cached:
            try:
                communicate = edge_tts.Communicate(text, self.voice_name,
                                                   rate=self.rate, pitch=self.pitch)
                await asyncio.wait_for(communicate.save(str(out) + ".tmp"), timeout=25)
                Path(str(out) + ".tmp").replace(out)
                self._prune()
            except Exception as exc:
                self._fail_until = time.time() + 20   # 熔断 20s，主备链立刻走下一引擎
                return VoiceResult(ok=False, voice=self.name, error=f"{type(exc).__name__}: {str(exc)[:80]}")
        from brain.lint import split_sentences
        sentences = split_sentences(text)
        return VoiceResult(ok=True, voice=self.name, url=f"/tts/{out.name}",
                           path=str(out), cached=cached,
                           duration=sum((s["end_ms"] - s["start_ms"]) for s in estimate_segments(sentences)) / 1000,
                           segments=estimate_segments(sentences))

    def _prune(self) -> None:
        files = sorted(self.cache_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
        for old in files[:-self.cache_size]:
            try:
                old.unlink()
            except OSError:
                pass

    def status(self) -> dict:
        return {"name": self.name, "voice": self.voice_name, "available": self.available()}


__all__ = ["EdgeVoice", "estimate_segments", "CACHE_DIR"]
