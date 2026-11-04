# -*- coding: utf-8 -*-
"""猜歌盲盒（XCool 验证过的互动设计）：放前奏 → 观众弹幕抢答 → fuzzy 匹配 → 奖励。

流程：观众发"猜歌" → 从本地曲库随机选一首 → 播 15 秒前奏（低音量）→
弹幕里含歌名即猜中 → 满分奖励心情/口头表扬；超时公布答案。
"""
from __future__ import annotations

import asyncio
import difflib
import random
import time

from core.events import Event, Topics


class GuessSong:
    name = "guess_song"
    default_enabled = False

    def __init__(self, bus, config, music=None, brain=None) -> None:
        self.bus = bus
        self.config = config
        self.music = music
        self.brain = brain
        self.active = None            # {title, artist, path, started_at}
        self._task: asyncio.Task | None = None
        self._unsub = None
        self.stats = {"rounds": 0, "winners": 0}

    async def start(self, ctx) -> None:
        self._unsub = self.bus.subscribe_many(
            [Topics.LIVE_CHAT, Topics.VOICE_SEG_READY], self._on)
        self.config.watch("guess_song.", lambda: None)

    async def stop(self) -> None:
        if self._unsub:
            self._unsub()

    @property
    def memory(self):
        return self.brain.memory if self.brain else None

    def _on(self, ev: Event) -> None:
        if ev.topic == Topics.LIVE_CHAT:
            self._on_chat(ev)
        else:
            self._on_song_end(ev)

    def _on_chat(self, ev: Event) -> None:
        d = ev.data or {}
        text = d.get("content", "")
        uid = d.get("user_id") or d.get("nickname")
        if self.active is None:
            if text.strip() == "猜歌" and time.time() - self.stats.get("_last", 0) > 120:
                self._start_round()
            return
        # 抢答：fuzzy 匹配歌名
        title = self.active["title"]
        ratio = difflib.SequenceMatcher(None, text, title).ratio()
        hit = title[:2] in text or ratio > 0.45
        if not hit:
            return
        elapsed = time.time() - self.active["started_at"]
        self.stats["winners"] += 1
        winner = d.get("nickname", "观众")
        self._publish(f"答对了！答案是《{title}》！{winner} 用了 {elapsed:.0f} 秒，耳朵绝了！", "happy", "dance")
        if self.memory:
            self.memory.mood_adjust(6, "猜歌有人答对")
        self._end()

    def _on_song_end(self, ev: Event) -> None:
        if self.active and ev.data.get("action") == "ended":
            pass   # 前奏播放结束事件（stage 侧），超时兜底在 _task

    def _start_round(self) -> None:
        if not self.music or not self.music.lib:
            self._publish("曲库是空的，猜不了歌～老板先放几首歌进来嘛。", "shy", "wave")
            return
        song = random.choice(self.music.lib)
        self.active = {"title": song["title"], "artist": song["artist"], "path": song["path"],
                       "started_at": time.time()}
        self.stats["rounds"] += 1
        self.stats["_last"] = time.time()
        self.bus.publish(Topics.SHOW_MUSIC, {
            "action": "guess", "path": f"/music/{song['id']}",
            "seconds": int(self.config.get("guess_song.preview_seconds", 15)),
        }, source="guess")
        self._publish("猜歌盲盒开！听前奏，猜中直接喊歌名！", "happy", "dance")
        self._task = asyncio.get_running_loop().create_task(self._timeout(15))

    async def _timeout(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
            if self.active:
                self._publish(f"时间到！都没猜中……答案是《{self.active['title']}》，下一首争取猜中！", "shy", "wave")
                self._end()
        except asyncio.CancelledError:
            raise

    def _end(self) -> None:
        self.active = None
        if self._task:
            self._task.cancel()
            self._task = None
        self.bus.publish(Topics.SHOW_MUSIC, {"action": "stop_guess"}, source="guess")

    def _publish(self, text: str, emotion: str, motion: str) -> None:
        self.bus.publish(Topics.BRAIN_REPLY, {
            "text": text[:60], "kind": "guess", "emotion": emotion,
            "viewer": "", "is_template": True,
            "client_id": f"guess-{int(time.time()*1000)}-{random.randint(100,999)}",
        }, source="guess")
        self.bus.publish(Topics.STAGE_COMMAND, {"action": "motion", "motion": motion}, source="guess")

    def status(self) -> dict:
        return {"active": self.active is not None, **self.stats}


__all__ = ["GuessSong"]
