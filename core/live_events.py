# -*- coding: utf-8 -*-
"""互动事件中心：各弹幕源统一在这里归一化为 LiveEvent。

来源：adapted from kureha4/dafeiyuzhibodouyinbanlv liveplugin/events.py (MIT License)。
保留原版权声明。本项目改动：
- 聚合定时器改为 asyncio 感知：事件循环内用 loop.call_later，无循环（单测）回退
  threading.Timer，两条路径行为一致
- 新增内容指纹去重：平台不给 msg_id 的来源（http relay）按
  (type+user+content, 15s 窗口) 指纹去重（ds-live 设计，自实现）
- close() 优雅停机，取消所有挂起定时器

对齐官方事件结构（2024.04.01 调整后的 payload）：
msg_type: 1=点赞 live_like, 2=评论 live_comment, 3=礼物 live_gift,
          4=粉丝团变更 live_fansclub, 5=关注变更 live_follow

处理要点：
- msg_id 幂等去重（防重连重放）+ 无 msg_id 时的内容指纹去重
- 点赞二次聚合：同一 sec_open_id 在 like_window 内累加 like_num
- 礼物连击合并：同一 sec_open_id + sec_gift_id 在 combo_window 内累加 gift_num
"""
from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable

from core.gifts import GiftCatalog


class MsgType(IntEnum):
    LIKE = 1
    COMMENT = 2
    GIFT = 3
    FANSCLUB = 4
    FOLLOW = 5
    MEMBER = 6          # WSS 逆向 / mock：进入直播间
    ROOM_STATS = 7      # WSS 逆向 / mock：在线人数


@dataclass
class LiveEvent:
    type: str                      # like / comment / gift / fansclub / follow / member / room_stats
    msg_id: str = ""
    timestamp: float = 0.0         # 秒（由毫秒事件时间戳转换，或本地接收时间）
    user_id: str = ""              # sec_open_id（加密 openid）
    nickname: str = ""
    avatar_url: str = ""
    content: str = ""              # 评论内容
    gift_id: str = ""              # sec_gift_id
    gift_name: str = ""
    gift_num: int = 1
    gift_value: int = 0            # 单价（抖币）
    like_num: int = 0
    fansclub_reason: int = 0       # 1升级 2加团 16退团
    follow_action: int = 0         # 1关注 2取关
    privilege_level: int = 0       # 荣誉等级
    is_follow_anchor: bool = False
    fansclub_level: int = 0
    member_count: int = 0          # 进场/统计消息携带的在线人数
    source: str = ""               # 事件来自哪个源（mock/cdp/http/dyhub）
    raw: dict = field(default_factory=dict)

    @property
    def total_value(self) -> int:
        return self.gift_value * self.gift_num

    def to_dict(self) -> dict:
        return {
            "type": self.type, "msg_id": self.msg_id, "timestamp": self.timestamp,
            "user_id": self.user_id, "nickname": self.nickname, "avatar_url": self.avatar_url,
            "content": self.content, "gift_id": self.gift_id, "gift_name": self.gift_name,
            "gift_num": self.gift_num, "gift_value": self.gift_value,
            "like_num": self.like_num, "fansclub_reason": self.fansclub_reason,
            "follow_action": self.follow_action, "privilege_level": self.privilege_level,
            "is_follow_anchor": self.is_follow_anchor, "fansclub_level": self.fansclub_level,
            "member_count": self.member_count, "source": self.source,
            "total_value": self.total_value,
        }


Listener = Callable[[LiveEvent], None]

_TYPE_BY_STR = {
    "live_like": "like",
    "live_comment": "comment",
    "live_gift": "gift",
    "live_fansclub": "fansclub",
    "live_follow": "follow",
    "live_member": "member",
    "live_room_stats": "room_stats",
}


class EventCenter:
    def __init__(self, gift_catalog: GiftCatalog | None = None,
                 like_window: float = 2.0, combo_window: float = 3.0,
                 dedup_size: int = 4096, fingerprint_window: float = 15.0) -> None:
        self.gifts = gift_catalog or GiftCatalog()
        self._listeners: list[Listener] = []
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._dedup_size = dedup_size
        self._like_window = like_window
        self._combo_window = combo_window
        self._fingerprint_window = fingerprint_window
        self._like_pending: dict[tuple, dict] = {}
        self._gift_pending: dict[tuple, dict] = {}
        self._fingerprints: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.RLock()  # 可重入：_fire 顺延路径会再进 _schedule_fire
        self._timers: set = set()          # threading.Timer 或 loop TimerHandle
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self.stats = {"received": 0, "dropped_dup": 0, "emitted": 0}

    # -- 生命周期 --------------------------------------------------------------
    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """在应用启动时挂上主事件循环；聚合定时器将走 call_later。"""
        self._loop = loop

    def close(self) -> None:
        self._closed = True
        with self._lock:
            timers, self._timers = set(self._timers), set()
        for t in timers:
            t.cancel()

    # -- 对外接口 ------------------------------------------------------------
    def on_event(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def handle_partner_event(self, event_name: str, params: dict, source: str = "partner") -> None:
        """官方伴侣插件入口：OPEN_LIVE_DATA payload。"""
        if event_name == "OPEN_LIVE_DATA":
            for item in params.get("payload", []) or []:
                self.ingest(item, source=source)

    def ingest(self, item: dict, source: str = "") -> None:
        """任一弹幕源的归一化入口：item 为官方 payload 或同构 dict。"""
        with self._lock:
            if self._closed:
                return
            self.stats["received"] += 1
            msg_id = str(item.get("msg_id", ""))

        tstr = item.get("msg_type_str") or {
            1: "live_like", 2: "live_comment", 3: "live_gift",
            4: "live_fansclub", 5: "live_follow",
            6: "live_member", 7: "live_room_stats"}.get(int(item.get("msg_type", 0)), "")
        if tstr not in _TYPE_BY_STR:
            return
        etype = _TYPE_BY_STR[tstr]

        ts = float(item.get("timestamp", 0)) / 1000.0 or time.time()
        user_id = str(item.get("sec_open_id", "")) or str(item.get("user_id", ""))
        content = item.get("content", "") or ""
        gift_name_plain = item.get("gift_name_plain", "") or item.get("gift_name", "")

        # -- 去重：有 msg_id 用平台 id；没有则内容指纹（ds-live 设计，自实现） --
        with self._lock:
            if msg_id:
                if msg_id in self._seen:
                    self.stats["dropped_dup"] += 1
                    return
                self._seen[msg_id] = None
                while len(self._seen) > self._dedup_size:
                    self._seen.popitem(last=False)
            else:
                fp_raw = f"{etype}|{user_id}|{content}|{gift_name_plain}|{int(ts / self._fingerprint_window)}"
                fp = hashlib.sha256(fp_raw.encode("utf-8")).hexdigest()[:24]
                last = self._fingerprints.get(fp)
                if last is not None and time.time() - last < self._fingerprint_window:
                    self.stats["dropped_dup"] += 1
                    return
                self._fingerprints[fp] = time.time()
                while len(self._fingerprints) > self._dedup_size:
                    self._fingerprints.popitem(last=False)

        ev = LiveEvent(
            type=etype, msg_id=msg_id, timestamp=ts, source=source,
            user_id=user_id,
            nickname=item.get("nickname", "") or "观众",
            avatar_url=item.get("avatar_url", ""),
            content=content,
            gift_id=item.get("sec_gift_id", ""),
            gift_num=int(item.get("gift_num", 1) or 1),
            like_num=int(item.get("like_num", 1) or 1),
            fansclub_reason=int(item.get("fansclub_reason_type", 0) or 0),
            follow_action=int(item.get("user_follow_action", 0) or 0),
            privilege_level=int(item.get("user_privilege_level", 0) or 0),
            is_follow_anchor=bool(item.get("is_follow_anchor", False)),
            fansclub_level=int(item.get("fansclub_level", 0) or 0),
            member_count=int(item.get("member_count", 0) or 0),
            raw=item,
        )
        if etype == "gift":
            if gift_name_plain:
                # WSS 逆向来源：协议内直接带礼物名（无 sec_gift_id）
                ev.gift_name = gift_name_plain
                if not ev.gift_id:
                    self.gifts.register_name(gift_name_plain)
                    ev.gift_value = self.gifts.value_by_name(gift_name_plain)
            else:
                ev.gift_name = self.gifts.name_of(ev.gift_id)
                ev.gift_value = self.gifts.value_of(ev.gift_id) if ev.gift_id else 0

        now = time.time()
        if etype == "like":
            self._debounce(self._like_pending, ("like", ev.user_id), ev, now,
                           self._like_window)
        elif etype == "gift":
            self._debounce(self._gift_pending, ("gift", ev.user_id, ev.gift_id, ev.gift_name),
                           ev, now, self._combo_window)
        else:
            self._emit(ev)
        self._flush_stale(now)

    # -- 聚合 ----------------------------------------------------------------
    def _debounce(self, store: dict, key, ev: LiveEvent,
                  now: float, window: float) -> None:
        """滑动窗口聚合：窗口内同类事件累加；每个 key 只挂一个到期定时器，
        到期时若事件仍在窗口内活跃则顺延，否则取出发送。"""
        with self._lock:
            if key in store:
                entry = store[key]
                entry["acc"].like_num += ev.like_num
                entry["acc"].gift_num += ev.gift_num
                entry["acc"].timestamp = now
                entry["last"] = now
                entry["deadline"] = now + window
                return
            entry = {"acc": ev, "last": now, "deadline": now + window}
            store[key] = entry
        self._schedule_fire(window, store, key, window)

    def _schedule_fire(self, delay: float, store: dict, key, window: float) -> None:
        """asyncio 感知定时器：循环内 call_later，无循环回退 threading.Timer。"""
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                handle = loop.call_later(delay, self._fire, store, key, window)
                with self._lock:
                    self._timers.add(handle)
                return
            except RuntimeError:
                pass
        t = threading.Timer(delay, self._fire, args=(store, key, window))
        t.daemon = True
        with self._lock:
            self._timers.add(t)
        t.start()

    def _fire(self, store: dict, key, window: float) -> None:
        postpone: float | None = None
        ev: LiveEvent | None = None
        with self._lock:
            entry = store.get(key)
            if entry is not None:
                if entry["deadline"] > time.time():
                    # 还在滑动窗口内，记下剩余时间，出锁后再顺延（避免锁内重入）
                    postpone = entry["deadline"] - time.time()
                else:
                    store.pop(key, None)
                    ev = entry["acc"]
        if postpone is not None:
            self._schedule_fire(postpone, store, key, window)
        elif ev is not None:
            self._emit(ev)

    def _flush_stale(self, now: float) -> None:
        """兜底：定时器之外，直接发超期未触达的聚合（>10s 视为异常）。"""
        for store in (self._like_pending, self._gift_pending):
            with self._lock:
                expired = [k for k, e in store.items() if now - e["last"] > 10.0]
                popped = [(k, store.pop(k)) for k in expired]
            for _k, entry in popped:
                self._emit(entry["acc"])

    # -- 分发 ----------------------------------------------------------------
    def _emit(self, ev: LiveEvent) -> None:
        self.stats["emitted"] += 1
        for listener in list(self._listeners):
            try:
                listener(ev)
            except Exception:
                pass


__all__ = ["MsgType", "LiveEvent", "EventCenter"]
