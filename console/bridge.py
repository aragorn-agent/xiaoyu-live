# -*- coding: utf-8 -*-
"""中控台：SSE 事件流 + 状态快照 + 模块热切换（纯 aiohttp，无构建链）。

kureha4 web/ 的 SSE 思想 + 本项目 asyncio 架构的融合：
- /api/events SSE：订阅 EventBus live.* 与 sys.*，断线客户端只丢不补（直播场景）
- /api/state：模块状态 + 流水线统计 + bus 统计
- /api/modules/{name}/toggle：热切换选配开关（写 config → 重启模块）
"""
from __future__ import annotations

import asyncio
import json
import time

from aiohttp import web

from core.events import Event, Topics


class ConsoleBridge:
    def __init__(self, bus, registry, pipeline, config) -> None:
        self.bus = bus
        self.registry = registry
        self.pipeline = pipeline
        self.config = config
        self._queues: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._unsub = bus.subscribe_many(
            [Topics.LIVE_EVENT, Topics.BRAIN_REPLY, Topics.VOICE_READY,
             Topics.VOICE_SPEAKING, Topics.BRAIN_MOOD, Topics.SHOW_MUSIC,
             Topics.STAGE_COMMAND, Topics.SESSION_BOOT],
            self._on_bus_event,
        )

    # -- SSE ------------------------------------------------------------------
    def _on_bus_event(self, ev: Event) -> None:
        if not self._queues:
            return
        try:
            payload = json.dumps(ev.to_dict(), ensure_ascii=False)
        except (TypeError, ValueError):
            payload = json.dumps({"topic": ev.topic, "data": {"unserializable": True},
                                  "source": ev.source, "ts": ev.ts}, ensure_ascii=False)
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None
        for q in list(self._queues):
            if q.full():
                continue  # 慢客户端直接丢帧，不阻塞直播链路
            if self._loop is not None:
                self._loop.call_soon_threadsafe(q.put_nowait, payload)
            else:
                q.put_nowait(payload)

    async def sse_events(self, request):
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._queues.add(q)
        try:
            # 先补最近 20 条历史，再进实时流
            for item in self.bus.history(20):
                await resp.write(b"data: " + json.dumps(item, ensure_ascii=False).encode("utf-8") + b"\n\n")
            hello = json.dumps({"topic": "sys.hello", "data": {"at": time.time()}, "source": "console", "ts": time.time()},
                               ensure_ascii=False)
            await resp.write(b"data: " + hello.encode("utf-8") + b"\n\n")
            await resp.drain()
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")   # 保活
                else:
                    await resp.write(b"data: " + payload.encode("utf-8") + b"\n\n")
                await resp.drain()
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            self._queues.discard(q)
        return resp

    # -- 状态 / 控制 ------------------------------------------------------------
    def state_snapshot(self) -> dict:
        return {
            "app": {"name": "小肥鱼直播间", "ts": time.time()},
            "pipeline": self.pipeline.snapshot(),
            "modules": self.registry.status(),
            "bus": self.bus.stats(),
            "center": dict(self.center_stats()),
        }

    def center_stats(self) -> dict:
        return getattr(self, "_center_stats", lambda: {})()

    def attach_center(self, center) -> None:
        self._center_stats = lambda: dict(center.stats)

    async def toggle_module(self, name: str, enabled: bool, ctx) -> dict:
        handle = self.registry.get(name)
        if handle is None:
            return {"ok": False, "error": f"no module {name}"}
        self.registry.set_enabled(name, enabled)
        self.config.set(f"modules.{name}.enabled", enabled)
        if enabled:
            await self.registry.restart(name, ctx)
        else:
            try:
                result = handle.module.stop()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
            handle.state = "stopped"
        return {"ok": True, "module": name, "enabled": enabled}

    def close(self) -> None:
        if self._unsub:
            self._unsub()


__all__ = ["ConsoleBridge"]
