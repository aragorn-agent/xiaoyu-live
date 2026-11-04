# -*- coding: utf-8 -*-
"""节奏控制：让小鱼话多但不刷屏。

来源：adapted from lxllxl233/dafeiyu-ai-live src/dafeiyu/persona/pacing.py (MIT License)。
"""

from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field

from brain.persona import Style


@dataclass
class Pacing:
    style: Style = field(default_factory=Style)
    _last_sent: float = 0.0
    _sent_times: deque = field(default_factory=lambda: deque(maxlen=64))
    _recent: deque = field(default_factory=lambda: deque(maxlen=10))
    _quip_used: dict = field(default_factory=dict)
    _repeat_hits: int = 0

    # ---------------------------------------------------------------- 判断
    def gap_remaining(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return max(0.0, self._last_sent + self.style.min_gap - now)

    def quip_allowed(self, quip: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        window = [t for t in self._sent_times if now - t <= 600]
        if len(window) >= self.style.max_quips_per_10min:
            return False
        return (now - self._quip_used.get(quip, -1e9)) >= self.style.quip_cooldown

    # ---------------------------------------------------------------- 记账
    def note_sent(self, text: str, quips: tuple[str, ...] = (), now: float | None = None) -> dict:
        now = time.time() if now is None else now
        t = (text or "").strip()
        self._last_sent = now
        self._sent_times.append(now)
        if t in self._recent:
            self._repeat_hits += 1
        self._recent.append(t)
        for quip in quips:
            if quip and quip in t:
                self._quip_used[quip] = now
        return {"wait": 0.0, "repeat": self._repeat_hits, "at": now}

    def stats(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        return {
            "min_gap": self.style.min_gap,
            "sent": len(self._sent_times),
            "last_10min": len([t for t in self._sent_times if now - t <= 600]),
            "repeat_hits": self._repeat_hits,
        }


__all__ = ["Pacing"]
