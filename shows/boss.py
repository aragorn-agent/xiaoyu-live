# -*- coding: utf-8 -*-
"""合作打 Boss（ds-live 验证过的节目设计，自实现）：全服观众合力回合制打怪。

规则：弹幕"开Boss"开启 → 20 秒输入窗 → "攻击"(6伤)/"蓄力"(10伤自损1)/"治疗"(回5)
→ Boss 每 1 秒反击（4+回合数/3）→ 血条实时推舞台 → 击杀/团灭结算 → 每观众每回合限 1 次
（内存 Map + 每回合记录双保险）。MVP Boss：愤怒的深海章鱼，血量随观众数。
"""
from __future__ import annotations

import asyncio
import random
import time

from core.events import Event, Topics

ACTIONS = {"攻击": 6, "打": 6, "蓄力": 10, "治疗": -5}


class BossFight:
    name = "boss"
    default_enabled = False

    def __init__(self, bus, config, brain=None) -> None:
        self.bus = bus
        self.config = config
        self.brain = brain
        self.fight = None             # {hp, max_hp, round, used{uid}, last_tick}
        self._tick_task: asyncio.Task | None = None
        self._unsub = None
        self.stats = {"fights": 0, "kills": 0}

    @property
    def memory(self):
        return self.brain.memory if self.brain else None

    async def start(self, ctx) -> None:
        self._unsub = self.bus.subscribe(Topics.LIVE_CHAT, self._on_chat)

    async def stop(self) -> None:
        if self._unsub:
            self._unsub()
        if self._tick_task:
            self._tick_task.cancel()
            self._tick_task = None

    def _on_chat(self, ev: Event) -> None:
        d = ev.data or {}
        text = d.get("content", "").strip()
        uid = d.get("user_id") or d.get("nickname")
        nickname = d.get("nickname", "观众")

        if self.fight is None:
            if text in ("开Boss", "开boss", "打Boss"):
                self._open(nickname)
            return
        act = next((k for k in ACTIONS if text == k or text == "开Boss" and False), None)
        if text in ("攻击", "打"):
            act = "攻击"
        elif text in ("蓄力",):
            act = "蓄力"
        elif text in ("治疗", "奶"):
            act = "治疗"
        if act is None:
            return
        # 每观众每回合 1 次
        key = (self.fight["round"], uid)
        if self.fight["used"].get(key):
            return
        self.fight["used"][key] = nickname
        dmg = ACTIONS[act]
        if act == "治疗":
            self.fight["hp"] = min(self.fight["max_hp"], self.fight["hp"] + 5)  # 治疗失误反向治疗=搞笑
            self.fight["hp"] -= 0
            self._push(f"{nickname} 给 Boss 奶了一口……你是哪边的？Boss +5")
        else:
            extra = 10 if act == "蓄力" else 6
            self.fight["hp"] -= extra
            self._push(f"{nickname} {act}！-{extra}（Boss 剩 {max(0, self.fight['hp'])}）")
        # Boss 反击
        boss_dmg = 4 + self.fight["round"] // 3
        self._push(f"Boss 反击 -{boss_dmg}！大家顶住！")
        self.fight["last_uid"] = uid
        self.fight["last_nick"] = nickname

    def _open(self, opener: str) -> None:
        viewers = max(3, (self.memory.status()["viewers"] if self.memory else 5))
        hp = 40 + viewers * 10
        self.fight = {"hp": hp, "max_hp": hp, "round": 1, "used": {}, "opener": opener,
                      "opened_at": time.time(), "boss": "愤怒的深海章鱼"}
        self.stats["fights"] += 1
        self._push(f"{opener} 唤醒了{self.fight['boss']}！血量 {hp}！弹幕打『攻击』/『蓄力』/『治疗』！")
        self.bus.publish(Topics.STAGE_COMMAND, {
            "action": "boss", "effect": "open", "boss": self.fight["boss"],
            "hp": hp, "max_hp": hp}, source="boss")
        if self._tick_task is None:
            self._tick_task = asyncio.get_running_loop().create_task(self._loop())

    def _push(self, text: str) -> None:
        self.bus.publish(Topics.BRAIN_REPLY, {
            "text": text[:60], "kind": "boss", "emotion": "happy",
            "viewer": "", "is_template": True,
            "client_id": f"boss-{int(time.time()*1000)}-{random.randint(100,999)}",
        }, source="boss")
        if self.fight:
            self.bus.publish(Topics.STAGE_COMMAND, {
                "action": "boss", "effect": "hp", "hp": max(0, self.fight["hp"]),
                "max_hp": self.fight["max_hp"], "round": self.fight["round"]}, source="boss")

    async def _loop(self) -> None:
        input_window = float(self.config.get("boss.input_window", 20))
        try:
            while True:
                await asyncio.sleep(1)
                if not self.fight:
                    continue
                f = self.fight
                if f["hp"] <= 0:
                    self._settle(win=True)
                    continue
                if time.time() - f["opened_at"] > (f["round"] * input_window + 30):
                    self._settle(win=False)
                    continue
        except asyncio.CancelledError:
            raise

    def _settle(self, win: bool) -> None:
        f = self.fight
        self.fight = None
        self.stats["kills"] += 1 if win else 0
        if win:
            text = f"Boss 被击败了！！全场观众合力，{f['round']} 个回合拿下！雷霆认证！"
            effect = "levelup"
        else:
            text = f"时间到，Boss 还剩 {max(0, f['hp'])} 血跑了……下次再来报仇！"
            effect = "wave"
        self.bus.publish(Topics.BRAIN_REPLY, {
            "text": text[:60], "kind": "boss", "emotion": "happy" if win else "shy",
            "viewer": "", "is_template": True,
            "client_id": f"boss-end-{int(time.time()*1000)}",
        }, source="boss")
        self.bus.publish(Topics.STAGE_COMMAND, {
            "action": "boss", "effect": "settle", "win": win, "effect2": effect}, source="boss")
        if self.memory and win:
            self.memory.mood_adjust(10, "击杀Boss")

    def status(self) -> dict:
        return {"fight": self.fight, **self.stats}


__all__ = ["BossFight", "ACTIONS"]
