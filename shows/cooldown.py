# -*- coding: utf-8 -*-
"""防冷场：无人互动超阈值 → mood 衰减 → 主动搭话/切节目。

比固定 20 秒随机暖场（XCool 设计）进一步：接 mood 情绪机，
衰减到低阈值时主动搭话并发布 SHOW_COOLDOWN（M6 的故事电台可接管）。
"""
from __future__ import annotations

import asyncio
import random
import time

from core.events import Event, Topics

_IDLE_LINES = [
    "咦，人都去哪了……鱼、鱼才没有在等你们！",
    "泡泡好无聊……要不要听小鱼讲讲深海的事？",
    "（吐泡泡）……再不来互动我就要睡啦！",
    "喂——弹幕区是死了嘛！哼！",
]


class CooldownGuard:
    name = "cooldown_guard"
    default_enabled = True

    _BY_TYPE = {
        "thread": "对了，{reason}——后来怎么样了？",
        "date": "今天{reason}，你们知道吗？",
        "morning": "早啊……鱼、鱼才不是刚醒！",
        "night": "这么晚了还不睡？讲个深海鬼故事？",
        "evening": "晚饭吃了吗？鱼在啃小鱼干。",
        "low": "……感觉今天有点丧。随便聊聊吧。",
        "share": "诶你们知道吗，鱼刚刷到一个事——",
    }

    def __init__(self, bus, config, brain=None) -> None:
        self.bus = bus
        self.config = config
        self.brain = brain          # 懒引用：brain.memory 在其 start() 时才创建
        self.last_activity = time.time()
        self._task: asyncio.Task | None = None
        self._unsub = None
        self.stats = {"triggered": 0}

    @property
    def memory(self):
        return self.brain.memory if self.brain else None

    async def start(self, ctx) -> None:
        self._unsub = self.bus.subscribe(Topics.LIVE_EVENT, self._on_any)
        self._task = asyncio.create_task(self._loop(), name="cooldown_guard")

    async def stop(self) -> None:
        if self._unsub:
            self._unsub()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _on_any(self, ev: Event) -> None:
        d = ev.data or {}
        if d.get("type") in ("comment", "gift", "member", "follow", "like"):
            self.last_activity = time.time()

    def touch(self) -> None:
        """外部（如 brain 回复后）也可刷新活跃时间。"""
        self.last_activity = time.time()

    async def _loop(self) -> None:
        idle_s = float(self.config.get("cooldown_guard.idle_seconds", 20))
        check = float(self.config.get("cooldown_guard.check_interval", 8))
        try:
            while True:
                await asyncio.sleep(check)
                idle = time.time() - self.last_activity
                # mood 冷场衰减（按分钟）
                if self.memory and idle > 60:
                    self.memory.mood_decay(idle / 60.0)
                if idle < idle_s:
                    continue
                low = int(self.config.get("brain.mood.low_threshold", 25))
                mood = self.memory.mood if self.memory else 60
                # 活人感：先问 companion"她为什么想开口"
                cand = None
                if self.brain and self.brain.companion:
                    cand = self.brain.companion.candidate(idle_minutes=idle / 60.0)
                if cand:
                    text = self._BY_TYPE.get(cand["type"], random.choice(_IDLE_LINES)).format(
                        reason=cand.get("reason", ""))
                elif mood < low:
                    text = random.choice(_IDLE_LINES)
                    self.bus.publish(Topics.BRAIN_REPLY, {
                        "text": text, "kind": "cooldown", "emotion": "shy",
                        "viewer": "", "is_template": True,
                        "client_id": f"cd-{int(time.time()*1000)}",
                    }, source="cooldown")
                    self.stats["triggered"] += 1
                    self.touch()
                else:
                    # 情绪尚可：只发舞台信号（M6 故事电台接管）
                    self.bus.publish(Topics.SHOW_COOLDOWN, {"idle_s": round(idle, 1), "mood": mood},
                                     source="cooldown")
                    self.touch()
        except asyncio.CancelledError:
            raise

    def status(self) -> dict:
        return {"idle_s": round(time.time() - self.last_activity, 1), **self.stats}


__all__ = ["CooldownGuard"]
