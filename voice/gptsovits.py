# -*- coding: utf-8 -*-
"""GPT-SoVITS api_v2 适配器（v2 选配，MIT 许可的引擎）。

对接 GPT-SoVITS 一键包的 api_v2：
POST {base_url}/tts  {"text", "ref_audio_path", "prompt_text",
                      "text_lang", "prompt_lang", "streaming_mode", "media_type"}
→ audio/wav

参考音频 = characters/voices/jia.wav（16 秒夹子音录音），prompt_text 是它的
文字稿（SenseVoice 转写已核对）。引擎没起时 available()=False，VoiceRouter
自动落 edge 兜底——直播间永远有声音。
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from pathlib import Path

from core.config import ROOT
from voice.base import VoiceResult
from voice.edge_tts import estimate_segments


class GPTSoVITSVoice:
    name = "gptsovits"

    def __init__(self, base_url: str = "http://127.0.0.1:9880/tts",
                 ref_audio: str = "characters/voices/jia.wav",
                 prompt_text: str = "", cache_dir: str = "data/cache/tts",
                 cache_size: int = 300, speed: float = 1.0) -> None:
        self.url = base_url
        self.ref_path = (ROOT / ref_audio).resolve() if not Path(ref_audio).is_absolute() else Path(ref_audio)
        self.prompt_text = prompt_text
        self.cache_dir = Path(cache_dir)
        self.cache_size = max(50, cache_size)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.speed = speed
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
            # api_v2 对空参数会 4xx，但只要端口有响应就算引擎在线
            urllib.request.urlopen(self.url, timeout=1)
            self._probe_ok_until = time.time() + 10   # 10s 探测缓存，防阻塞事件循环
            return True
        except urllib.error.HTTPError:
            self._probe_ok_until = time.time() + 10
            return True
        except Exception:
            self._probe_fail_until = time.time() + 30   # 死端口也缓存，防阻塞状态轮询
            return False

    def _key(self, text: str) -> str:
        return hashlib.sha1(f"gsv|{self.ref_path.name}|{self.speed}|{text}".encode("utf-8")).hexdigest()[:20]

    async def synth(self, text: str, **opts) -> VoiceResult:
        key = self._key(text)
        out = self.cache_dir / f"{key}.wav"
        cached = out.exists() and out.stat().st_size > 2048
        if not cached:
            body = json.dumps({
                "text": text, "text_lang": "zh",
                "ref_audio_path": str(self.ref_path),
                "prompt_text": self.prompt_text, "prompt_lang": "zh",
                "speed_factor": self.speed,
                "streaming_mode": False, "media_type": "wav",
            }).encode("utf-8")
            req = urllib.request.Request(self.url, data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
            try:
                import asyncio
                loop = asyncio.get_running_loop()
                resp = await loop.run_in_executor(
                    None, lambda: urllib.request.urlopen(req, timeout=60))
                with resp:
                    data = resp.read()
                if data[:4] != b"RIFF" and b"error" in data[:200].lower():
                    raise RuntimeError(data[:160].decode("utf-8", "replace"))
                out.write_bytes(data)
                self._prune()
            except Exception as exc:
                self._fail_until = time.time() + 15    # 熔断 15s，主备链落 edge
                return VoiceResult(ok=False, voice=self.name,
                                   error=f"{type(exc).__name__}: {str(exc)[:80]}")
        from brain.lint import split_sentences
        segs = estimate_segments(split_sentences(text))
        return VoiceResult(ok=True, voice=self.name, url=f"/tts/{out.name}",
                           path=str(out), cached=cached,
                           duration=sum(s["end_ms"] - s["start_ms"] for s in segs) / 1000,
                           segments=segs)

    def _prune(self) -> None:
        files = sorted(self.cache_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)
        for old in files[:-self.cache_size]:
            try:
                old.unlink()
            except OSError:
                pass

    def status(self) -> dict:
        return {"name": self.name, "url": self.url,
                "ref": self.ref_path.name, "available": self.available()}


__all__ = ["GPTSoVITSVoice"]
