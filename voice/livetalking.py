# -*- coding: utf-8 -*-
"""LiveTalking 适配器（选配）：把 LiveTalking（lipku，Apache-2.0）作为语音+口型引擎接入。

两种用法：
1) 仅语音：LiveTalking 的 /human 接口按文本合成音频（含其内部 TTS 音色），
   作为 VoiceRouter 里的一个 Voice 实现（本文件 HttpLiveTalkingVoice）。
2) 全接管：舞台切到 LiveTalking 自带的 WebRTC 画面（口型由它驱动），
   本项目只负责弹幕→文本。配置 render.livetalking.base_url 后，
   舞台页提供 ?engine=livetalking 嵌入其 webrtc 页（render/livetalking 模块）。

依赖：真机上先单独跑起 LiveTalking 服务（见其 README，需 GPU）。
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from pathlib import Path

from voice.base import VoiceResult
from voice.edge_tts import estimate_segments


class HttpLiveTalkingVoice:
    """POST {base_url}/human  {mode,image,text,voiceid,...} → wav 音频。"""
    name = "livetalking"

    def __init__(self, base_url: str = "http://127.0.0.1:8010",
                 voiceid: str = "zh-CN-XiaoyiNeural", image: str = "",
                 cache_dir: str = "data/cache/tts", extra: dict | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.voiceid = voiceid
        self.image = image
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
            urllib.request.urlopen(self.base_url + "/human", timeout=1, data=b"")
            self._probe_ok_until = time.time() + 10
            return True
        except urllib.error.HTTPError:
            self._probe_ok_until = time.time() + 10
            return True
        except Exception:
            # 405/400 也算服务在线（说明端点存在）
            try:
                urllib.request.urlopen(self.base_url + "/human", timeout=1)
                self._probe_ok_until = time.time() + 10
                return True
            except urllib.error.HTTPError:
                self._probe_ok_until = time.time() + 10
                return True
            except Exception:
                self._probe_fail_until = time.time() + 30
                return False

    def _key(self, text: str) -> str:
        return hashlib.sha1(f"lt|{self.voiceid}|{text}".encode()).hexdigest()[:20]

    async def synth(self, text: str, **opts) -> VoiceResult:
        key = self._key(text)
        out = self.cache_dir / f"{key}.wav"
        cached = out.exists() and out.stat().st_size > 2048
        if not cached:
            body = json.dumps({"mode": "websocket", "image": self.image,
                               "text": text, "voiceid": self.voiceid,
                               "audiotype": "local", **self.extra}).encode("utf-8")
            req = urllib.request.Request(self.base_url + "/human", data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
            try:
                import asyncio
                loop = asyncio.get_running_loop()
                data = await loop.run_in_executor(
                    None, lambda: urllib.request.urlopen(req, timeout=120).read())
                if data[:4] not in (b"RIFF", b"OggS", b"\x1f\x8b") and b"audio" not in str(data[:200]):
                    # 返回 JSON 错误而非音频
                    raise RuntimeError(data[:120].decode("utf-8", "replace"))
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
        return {"name": self.name, "url": self.base_url, "available": self.available()}


class LiveTalkingRender:
    """渲染适配器（全接管模式）：舞台页跳转/内嵌 LiveTalking 的 WebRTC 页。"""
    name = "render_livetalking"
    default_enabled = False

    def __init__(self, config) -> None:
        self.config = config

    def page_url(self) -> str:
        base = str(self.config.get("render.livetalking.base_url", "http://127.0.0.1:8010"))
        return f"{base}/human?voiceid={self.config.get('render.livetalking.voiceid', 'zh-CN-XiaoyiNeural')}"

    async def start(self, ctx) -> None:
        print(f"[livetalking] 全接管模式：舞台请嵌入 {self.page_url()}", flush=True)

    async def stop(self) -> None:
        return None

    def status(self) -> dict:
        return {"page": self.page_url()}


__all__ = ["HttpLiveTalkingVoice", "LiveTalkingRender"]
