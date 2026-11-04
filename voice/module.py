# -*- coding: utf-8 -*-
"""语音模块：订阅 BRAIN_REPLY → VoiceRouter 合成 → 发布 VOICE_READY / VOICE_SPEAKING。

同一时刻只合成/播报一条（单飞），新的 BRAIN_REPLY 取消在途的旧任务（Dsh_douyin
speakToken 抢占思想的编排侧实现）。
"""
from __future__ import annotations

import asyncio
import time

from core.events import Event, Topics
from voice.cosyvoice import HttpCloneVoice
from voice.edge_tts import EdgeVoice
from voice.gptsovits import GPTSoVITSVoice
from voice.livetalking import HttpLiveTalkingVoice
from voice.router import VoiceRouter


class VoiceModule:
    name = "voice"
    default_enabled = True

    def __init__(self, bus, config) -> None:
        self.bus = bus
        self.config = config
        self.router: VoiceRouter | None = None
        self._task: asyncio.Task | None = None
        self._chain_task: asyncio.Task | None = None
        self._token = 0                # 抢占令牌（整句模式）
        self._unsub = None
        self.stats = {"requests": 0, "ready": 0, "failed": 0, "superseded": 0, "segments": 0}

    async def start(self, ctx) -> None:
        self._build()
        self._unsub = self.bus.subscribe_many(
            [Topics.BRAIN_REPLY, Topics.VOICE_SEGMENT],
            self._on_voice_request)
        self.config.watch("voice.", self._rebuild)

    async def stop(self) -> None:
        if self._unsub:
            self._unsub()
        self._token += 1               # 作废在途任务
        for t in (self._chain_task, self._task):
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        self._chain_task = self._task = None

    def _build(self) -> None:
        cfg = self.config
        cache_dir = cfg.get("voice.cache_dir", "data/cache/tts")
        cache_size = int(cfg.get("voice.cache_size", 300))
        voices = {
            "edge": EdgeVoice(voice=cfg.get("voice.edge.voice", "zh-CN-XiaoyiNeural"),
                              rate=cfg.get("voice.edge.rate", "+0%"),
                              pitch=cfg.get("voice.edge.pitch", "+0Hz"),
                              cache_dir=cache_dir, cache_size=cache_size),
            "gptsovits": GPTSoVITSVoice(base_url=cfg.get("voice.gptsovits.url", "http://127.0.0.1:9880/tts"),
                                        ref_audio=cfg.get("voice.gptsovits.ref_audio", "characters/voices/jia.wav"),
                                        prompt_text=cfg.get("voice.gptsovits.prompt_text", ""),
                                        cache_dir=cache_dir, cache_size=cache_size,
                                        speed=float(cfg.get("voice.gptsovits.speed", 1.0))),
            "cosyvoice": HttpCloneVoice(url=cfg.get("voice.cosyvoice.url", "http://127.0.0.1:9880/tts"),
                                        voice_dir=cfg.get("voice.cosyvoice.voice_dir", "characters/voices"),
                                        cache_dir=cache_dir,
                                        extra=cfg.get("voice.cosyvoice.extra") or None),
            "livetalking": HttpLiveTalkingVoice(base_url=cfg.get("voice.livetalking.base_url", "http://127.0.0.1:8010"),
                                                voiceid=cfg.get("voice.livetalking.voiceid", "zh-CN-XiaoyiNeural"),
                                                cache_dir=cache_dir),
        }
        self.router = VoiceRouter(voices,
                                  primary=cfg.get("voice.primary", "edge"),
                                  fallback=cfg.get("voice.fallback", "edge"))

    def _rebuild(self) -> None:
        self._build()

    # ---------------------------------------------------------------- 入口分流
    def _on_voice_request(self, ev: Event) -> None:
        if ev.topic == Topics.VOICE_SEGMENT:
            self._on_segment(ev)
            return
        d = ev.data or {}
        if d.get("voice") is False:
            return                     # 流式聊天路径：语音由 voice.segment 负责
        text = (d.get("text") or "").strip()
        if not text:
            return
        self.stats["requests"] += 1
        self._token += 1
        if self._task and not self._task.done():
            self.stats["superseded"] += 1
            self._task.cancel()
        token = self._token
        self._task = asyncio.get_running_loop().create_task(self._speak(text, token))

    # ---------------------------------------------------------------- 分段链路
    def _on_segment(self, ev: Event) -> None:
        d = ev.data or {}
        text = (d.get("text") or "").strip()
        if not text:
            return
        self.stats["segments"] += 1
        client_id = d.get("client_id") or "chain"
        seq = int(d.get("seq", 0))
        emotion = d.get("emotion", "neutral")
        self._chain_task = asyncio.get_running_loop().create_task(
            self._synth_segment(client_id, seq, text, emotion))

    async def _synth_segment(self, client_id: str, seq: int, text: str, emotion: str) -> None:
        try:
            result = await self.router.speak(text)
            payload = {"client_id": client_id, "seq": seq, "text": text, "emotion": emotion}
            if result.ok:
                self.stats["ready"] += 1
                payload.update({"url": result.url, "voice": result.voice,
                                "duration": result.duration, "cached": result.cached})
            else:
                self.stats["failed"] += 1
                payload.update({"url": "", "error": result.error})
            self.bus.publish(Topics.VOICE_SEG_READY, payload, source="voice")
        except asyncio.CancelledError:
            raise

    # ---------------------------------------------------------------- 整句链路
    def _on_reply(self, ev: Event) -> None:
        text = (ev.data or {}).get("text", "")
        if not text:
            return
        self.stats["requests"] += 1
        self._token += 1
        if self._task and not self._task.done():
            self.stats["superseded"] += 1
            self._task.cancel()
        token = self._token
        self._task = asyncio.get_running_loop().create_task(self._speak(text, token))

    async def _speak(self, text: str, token: int) -> None:
        try:
            self.bus.publish(Topics.VOICE_SPEAKING, {"speaking": True, "text": text}, source="voice")
            result = await self.router.speak(text)
            if token != self._token:      # 已被更新的回复抢占
                self.stats["superseded"] += 1
                return
            if result.ok:
                self.stats["ready"] += 1
                self.bus.publish(Topics.VOICE_READY, {
                    "speech_id": f"s{token}-{int(time.time()*1000) % 100000}",
                    "url": result.url, "duration": result.duration,
                    "segments": result.segments, "text": text,
                    "voice": result.voice, "cached": result.cached,
                }, source="voice")
            else:
                self.stats["failed"] += 1
                self.bus.publish(Topics.VOICE_FAILED, {"text": text, "error": result.error},
                                 source="voice")
        except asyncio.CancelledError:
            raise
        finally:
            if token == self._token:
                self.bus.publish(Topics.VOICE_SPEAKING, {"speaking": False}, source="voice")

    def status(self) -> dict:
        return {"stats": dict(self.stats),
                "router": self.router.status() if self.router else {}}


__all__ = ["VoiceModule"]
