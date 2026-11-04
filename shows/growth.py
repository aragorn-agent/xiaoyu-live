# -*- coding: utf-8 -*-
"""养成进化 + 榜一：观众投喂让小鱼长大，贡献榜实时结算。

（XCool 养成/榜一的设计重写；修掉它的两大缺陷：状态全内存重启清零、
进化无实际特效——本模块全量落盘 + 舞台事件驱动特效。）

数据：
- data/growth.json: weight（体重g）、level、contributions{uid: {nickname, diamonds}}
- 档位：体重 500/1500/3500/8000 → 鱼苗/小肥鱼/大肥鱼/雷霆大肥鱼/传说之鱼
- 事件：GROWTH_UPDATE（数值）/ STAGE_COMMAND effect=levelup（升级全屏特效）
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from core.events import Event, Topics

LEVELS = [
    {"name": "鱼苗", "weight": 0},
    {"name": "小肥鱼", "weight": 500},
    {"name": "大肥鱼", "weight": 1500},
    {"name": "雷霆大肥鱼", "weight": 3500},
    {"name": "传说之鱼", "weight": 8000},
]
_WEIGHT = {"like": 1, "follow": 10, "fansclub": 30, "comment": 0, "gift_per_diamond": 5}


class Growth:
    name = "growth"
    default_enabled = False     # 批次2选配：中控台打开即用

    def __init__(self, bus, config) -> None:
        self.bus = bus
        self.config = config
        self.path = Path("data/growth.json")
        self._lock = threading.Lock()
        self._save_timer: threading.Timer | None = None
        self.data = {"weight": 180, "contributions": {}}
        self.load()
        self._unsub = None
        self.stats = {"events": 0, "levelups": 0}

    # ------------------------------------------------------------ 持久化
    def load(self) -> None:
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[growth] load failed: {exc}", flush=True)

    def save(self) -> None:
        self._save_timer = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            print(f"[growth] save failed: {exc}", flush=True)

    def _schedule_save(self) -> None:
        if self._save_timer is None:
            self._save_timer = threading.Timer(1.2, self.save)
            self._save_timer.daemon = True
            self._save_timer.start()

    # ------------------------------------------------------------ 等级
    def level_of(self, weight: int) -> tuple[int, dict, dict]:
        """返回 (等级序号, 当前档, 下一档或None)。"""
        idx = 0
        for i, lv in enumerate(LEVELS):
            if weight >= lv["weight"]:
                idx = i
        return idx, LEVELS[idx], LEVELS[idx + 1] if idx + 1 < len(LEVELS) else None

    # ------------------------------------------------------------ 事件
    async def start(self, ctx) -> None:
        self._unsub = self.bus.subscribe_many(
            [Topics.LIVE_GIFT, Topics.LIVE_LIKE, Topics.LIVE_FOLLOW, Topics.LIVE_FANSCLUB],
            self._on_event)

    async def stop(self) -> None:
        if self._unsub:
            self._unsub()
        self.save()

    def _on_event(self, ev: Event) -> None:
        d = ev.data or {}
        kind = d.get("type", "")
        uid = d.get("user_id") or d.get("nickname") or "anon"
        nickname = d.get("nickname", "观众")
        gain = 0
        diamonds = 0
        if kind == "gift":
            diamonds = int(d.get("total_value", 0))
            gain = diamonds * _WEIGHT["gift_per_diamond"]
        elif kind == "like":
            gain = int(d.get("like_num", 1)) * _WEIGHT["like"]
        elif kind == "follow":
            gain = _WEIGHT["follow"]
        elif kind == "fansclub":
            gain = _WEIGHT["fansclub"]
        if gain <= 0:
            return
        self.stats["events"] += 1
        with self._lock:
            old_idx, old_lv, _ = self.level_of(self.data["weight"])
            self.data["weight"] += gain
            new_idx, new_lv, next_lv = self.level_of(self.data["weight"])
            c = self.data["contributions"].setdefault(uid, {"nickname": nickname, "diamonds": 0})
            c["nickname"] = nickname
            c["diamonds"] += diamonds
            self._schedule_save()
        self.bus.publish("growth.update", {
            "weight": self.data["weight"], "gain": gain, "level": new_lv["name"],
            "next": next_lv["name"] if next_lv else None,
            "next_at": next_lv["weight"] if next_lv else None,
            "by": nickname, "kind": kind,
        }, source="growth")
        if new_idx > old_idx:
            self.stats["levelups"] += 1
            self.bus.publish(Topics.STAGE_COMMAND, {
                "action": "effect", "effect": "levelup", "level": new_lv["name"],
                "weight": self.data["weight"], "by": nickname,
            }, source="growth")
            self.bus.publish(Topics.BRAIN_REPLY, {
                "text": f"！！！进化成{new_lv['name']}了！谢谢{nickname}！",
                "kind": "growth", "emotion": "happy", "viewer": nickname,
                "is_template": True,
                "client_id": f"growth-{int(time.time()*1000)}",
            }, source="growth")

    # ------------------------------------------------------------ 榜一
    def top(self, n: int = 3) -> list[dict]:
        with self._lock:
            items = sorted(self.data["contributions"].items(),
                           key=lambda kv: kv[1]["diamonds"], reverse=True)[:n]
        return [{"uid": uid, **v} for uid, v in items]

    def status(self) -> dict:
        idx, lv, nxt = self.level_of(self.data["weight"])
        return {"weight": self.data["weight"], "level": lv["name"],
                "next": nxt["name"] if nxt else None,
                "next_at": nxt["weight"] if nxt else None,
                "top": self.top(3), **self.stats}


__all__ = ["Growth", "LEVELS"]
