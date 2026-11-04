# -*- coding: utf-8 -*-
"""Pipeline：EventCenter(LiveEvent) 与 EventBus(主题事件) 之间的桥。

- 把聚合后的 LiveEvent 转成 Event 发到 live.* 主题
- 同时发一条 live.event 全量事件（中控台/调试订阅用）
- 维护会话统计（开播以来收到的各类事件数、抖币收入）
"""
from __future__ import annotations

import threading
import time

from core.events import Event, Topics
from core.live_events import LiveEvent

_TYPE_TOPIC = {
    "comment": Topics.LIVE_CHAT,
    "gift": Topics.LIVE_GIFT,
    "member": Topics.LIVE_MEMBER,
    "follow": Topics.LIVE_FOLLOW,
    "like": Topics.LIVE_LIKE,
    "fansclub": Topics.LIVE_FANSCLUB,
    "room_stats": Topics.LIVE_STATS,
}


class Pipeline:
    def __init__(self, bus) -> None:
        self.bus = bus
        self._lock = threading.Lock()
        self.reset_stats()

    def reset_stats(self) -> None:
        with self._lock:
            self.stats = {
                "boot_at": time.time(),
                "by_type": {},
                "income_diamonds": 0,
                "viewers_seen": set(),
            }

    def on_live_event(self, ev: LiveEvent) -> None:
        with self._lock:
            self.stats["by_type"][ev.type] = self.stats["by_type"].get(ev.type, 0) + 1
            if ev.type == "gift":
                self.stats["income_diamonds"] += ev.total_value
            if ev.nickname:
                self.stats["viewers_seen"].add(ev.user_id or ev.nickname)
        self.bus.publish(Topics.LIVE_EVENT, ev.to_dict(), source=f"ingress:{ev.source}")
        topic = _TYPE_TOPIC.get(ev.type)
        if topic:
            self.bus.publish(topic, ev.to_dict(), source=f"ingress:{ev.source}")

    # -- 中控台 ------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "boot_at": self.stats["boot_at"],
                "uptime_s": round(time.time() - self.stats["boot_at"], 1),
                "by_type": dict(self.stats["by_type"]),
                "income_diamonds": self.stats["income_diamonds"],
                "viewers": len(self.stats["viewers_seen"]),
            }

    def boot(self) -> None:
        """开播清场：统计归零并广播。"""
        self.reset_stats()
        self.bus.publish(Topics.SESSION_BOOT, {"at": time.time()}, source="pipeline")


__all__ = ["Pipeline"]
