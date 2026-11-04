# -*- coding: utf-8 -*-
"""跳舞触发：观众喊「跳舞/极乐净土/来个舞」→ 舞台舞蹈模式（限频 45 秒）。

当前实现：Live2D 舞蹈模式（Tap 动作循环 + 霓虹灯光效 + BGM 加速感）。
three3d GLTF 骨骼引擎（XCool 设计）待用户提供可商用 GLB+动作文件后接入，
接口已留：STAGE_COMMAND {action:"motion", motion:"dance"} 就是它的触发信号。
"""
from __future__ import annotations

import asyncio
import re
import time

from core.events import Event, Topics

_DANCE_RE = re.compile(r"跳舞|来个舞|舞一个|极乐净土|dance", re.I)


class DanceTrigger:
    name = "dance"
    default_enabled = False    # 批次2选配

    def __init__(self, bus, config) -> None:
        self.bus = bus
        self.config = config
        self._last = 0.0
        self._unsub = None
        self.stats = {"triggers": 0, "skipped": 0}

    async def start(self, ctx) -> None:
        self._unsub = self.bus.subscribe(Topics.LIVE_CHAT, self._on_chat)

    async def stop(self) -> None:
        if self._unsub:
            self._unsub()

    def _on_chat(self, ev: Event) -> None:
        d = ev.data or {}
        text = d.get("content", "")
        if not _DANCE_RE.search(text):
            return
        now = time.time()
        cooldown = float(self.config.get("dance.cooldown", 45))
        if now - self._last < cooldown:
            self.stats["skipped"] += 1
            return
        self._last = now
        self.stats["triggers"] += 1
        seconds = int(self.config.get("dance.seconds", 25))
        self.bus.publish(Topics.STAGE_COMMAND, {
            "action": "motion", "motion": "dance", "seconds": seconds,
            "by": d.get("nickname", ""), "text": text,
        }, source="dance")
        self.bus.publish(Topics.BRAIN_REPLY, {
            "text": "音乐呢！雷霆舞王模式启动！",
            "kind": "command", "emotion": "happy", "viewer": d.get("nickname", ""),
            "is_template": True, "client_id": f"dance-{int(now*1000)}",
        }, source="dance")

    def status(self) -> dict:
        return {"last_ago": round(time.time() - self._last, 1) if self._last else None, **self.stats}


__all__ = ["DanceTrigger", "_DANCE_RE"]
