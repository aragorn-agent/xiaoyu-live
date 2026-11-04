# -*- coding: utf-8 -*-
"""CosyVoice / GPT-SoVITS 适配器（选配，GPU 侧服务）。

对接自建 HTTP 服务：POST {url} {"text": ..., "text_lang": "zh", ...} → wav/mp3。
兼容 GPT-SoVITS api_v2（kureha4 tts.py 同款端点，MIT 参考）与
旧项目 CosyVoice2 的 OpenAI 式包装——只要返回音频字节都行。
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from pathlib import Path

from voice.base import VoiceResult
from voice.edge_tts import estimate_segments


class HttpCloneVoice:
    """通用 HTTP 声音克隆客户端。probe /tts 一次判断可用性。"""
    name = "cosyvoice"

    def __init__(self, url: str = "http://127.0.0.1:9880/tts",
                 voice_dir: str = "characters/voices", voice: str = "default",
                 cache_dir: str = "data/cache/tts", extra: dict | None = None) -> None:
        self.url = url
        self.voice = voice
        self.voice_dir = Path(voice_dir)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.extra = extra or {}
        self._fail_until = 0.0
        self._probe_ok_until = 0.0
        self._probe_fail_until = 0.0

    def available(self) -> bool:
        if time.time() < self._fail_until:
            return False
        if time.time() < self._probe_fail_until:
            return False
        if time.time() < self._probe_ok_until:
            return True
        try:
            req = urllib.request.Request(self.url, method="OPTIONS")
            urllib.request.urlopen(req, timeout=1)
            self._probe_ok_until = time.time() + 10
            return True
        except urllib.error.HTTPError:
            self._probe_ok_until = time.time() + 10
            return True
        except Exception:
            self._probe_fail_until = time.time() + 30   # 死端口也缓存，防阻塞状态轮询
            return False

    def _key(self, text: str) -> str:
        raw = f"cosy|{self.voice}|{text}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]

    async def synth(self, text: str, **opts) -> VoiceResult:
        key = self._key(text)
        ext = ".mp3"
        out = self.cache_dir / f"{key}{ext}"
        cached = out.exists() and out.stat().st_size > 2048
        if not cached:
            body = json.dumps({"text": text, "text_lang": "zh",
                               "media_type": ext.lstrip("."), "streaming_mode": False,
                               **self.extra}).encode("utf-8")
            req = urllib.request.Request(self.url, data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
            try:
                loop = __import__("asyncio").get_running_loop()
                resp = await loop.run_in_executor(
                    None, lambda: urllib.request.urlopen(req, timeout=120))
                with resp:
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    data = resp.read()
                if "wav" in ctype or data[:4] == b"RIFF":
                    ext = ".wav"
                out = self.cache_dir / f"{key}{ext}"
                out.write_bytes(data)
            except Exception as exc:
                self._fail_until = time.time() + 20
                return VoiceResult(ok=False, voice=self.name,
                                   error=f"{type(exc).__name__}: {str(exc)[:80]}")
        from brain.lint import split_sentences
        segs = estimate_segments(split_sentences(text))
        return VoiceResult(ok=True, voice=self.name, url=f"/tts/{out.name}",
                           path=str(out), cached=cached,
                           duration=sum(s["end_ms"] - s["start_ms"] for s in segs) / 1000,
                           segments=segs)

    def status(self) -> dict:
        return {"name": self.name, "url": self.url, "voice": self.voice,
                "available": self.available()}


__all__ = ["HttpCloneVoice"]
