# -*- coding: utf-8 -*-
"""线程安全的事件总线：层之间唯一的耦合点。

来源：adapted from lxllxl233/dafeiyu-ai-live src/dafeiyu/core/bus.py (MIT License)。
保留原版权声明；仅做命名与注释适配。

用法::

    bus = EventBus()
    bus.subscribe("live.*", on_live)
    bus.publish(Topics.LIVE_CHAT, {"user": "阿拉贡", "text": "来啦"}, source="ingress")

处理器是同步调用的（在发布线程里），所以处理器要足够轻；重活自己扔线程/任务。
"""

from __future__ import annotations

import fnmatch
import sys
import threading
import time
from collections import deque
from typing import Any, Callable, Iterable

from core.events import Event

Handler = Callable[[Event], Any]


class EventBus:
    def __init__(self, history: int = 256) -> None:
        self._subs: list[tuple[str, Handler]] = []
        self._lock = threading.RLock()
        self._history: deque[Event] = deque(maxlen=max(16, history))
        self.errors: deque[str] = deque(maxlen=50)
        self._published = 0

    # ---------------------------------------------------------------- 订阅
    def subscribe(self, pattern: str, handler: Handler) -> Callable[[], None]:
        """订阅一个主题（支持 ``live.*`` 通配）。返回取消订阅的函数。"""
        entry = (str(pattern), handler)
        with self._lock:
            self._subs.append(entry)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subs.remove(entry)
                except ValueError:
                    pass

        return unsubscribe

    def subscribe_many(self, patterns: Iterable[str], handler: Handler) -> Callable[[], None]:
        if isinstance(patterns, str):
            patterns = [patterns]
        unsubs = [self.subscribe(p, handler) for p in patterns]

        def unsubscribe() -> None:
            for fn in unsubs:
                fn()

        return unsubscribe

    # ---------------------------------------------------------------- 发布
    def publish(self, topic: Event | str, data: dict | None = None, source: str = "") -> Event:
        ev = topic if isinstance(topic, Event) else Event(topic=str(topic), data=dict(data or {}), source=source)
        with self._lock:
            handlers = [h for pat, h in self._subs if self._matches(pat, ev.topic)]
            self._history.append(ev)
            self._published += 1
        for handler in handlers:
            try:
                handler(ev)
            except Exception as exc:  # 一个订阅者崩了不能拖垮整条直播链路
                msg = f"{ev.topic}: {type(exc).__name__}: {exc}"
                with self._lock:
                    self.errors.append(msg)
                print(f"[bus] handler error {msg}", file=sys.stderr, flush=True)
        return ev

    @staticmethod
    def _matches(pattern: str, topic: str) -> bool:
        if pattern in ("*", "", None):
            return True
        return fnmatch.fnmatchcase(topic, pattern)

    # ---------------------------------------------------------------- 观测
    def history(self, limit: int = 20, pattern: str | None = None) -> list[dict]:
        with self._lock:
            items = list(self._history)
        if pattern:
            items = [e for e in items if self._matches(pattern, e.topic)]
        return [e.to_dict() for e in items[-max(0, limit):]]

    def stats(self) -> dict:
        with self._lock:
            return {
                "subscribers": len(self._subs),
                "published": self._published,
                "history": len(self._history),
                "errors": len(self.errors),
                "last_error": self.errors[-1] if self.errors else "",
            }

    def wait_for(self, topic: str, timeout: float = 5.0, predicate: Callable[[Event], bool] | None = None) -> Event | None:
        """阻塞等一条事件（测试/CLI 用，不进入主链路）。"""
        deadline = time.time() + timeout
        box: list[Event] = []

        def _on(ev: Event) -> None:
            if predicate is None or predicate(ev):
                box.append(ev)

        unsub = self.subscribe(topic, _on)
        try:
            while time.time() < deadline:
                if box:
                    return box[0]
                time.sleep(0.01)
            return box[0] if box else None
        finally:
            unsub()


__all__ = ["EventBus", "Handler"]
