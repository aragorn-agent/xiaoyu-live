# -*- coding: utf-8 -*-
"""礼物档位：按官方回执的抖币价值定档 → 专属动作 + 感谢词。

纪律（whale-girl plan_gift_reply 设计，自实现）：
- 档位只认事件里的 total_value（平台回执），绝不让模型猜价格
- 模板优先；gift_reply_mode=model 时才让 LLM 润色
- 连击已在 EventCenter 合并（一个 user+gift 一条），这里天然整组结算
"""
from __future__ import annotations

import asyncio
import random
import time

from core.events import Event, Topics


class GiftTiers:
    name = "gift_tiers"
    default_enabled = True

    def __init__(self, bus, config, brain=None) -> None:
        self.bus = bus
        self.config = config
        self.brain = brain          # 懒引用：brain.memory 在其 start() 时才创建
        self.tiers: list[dict] = []
        self.custom: dict[str, dict] = {}
        self.mode = "template"
        self._last_thanks: dict[str, float] = {}   # 同观众同礼物冷却
        self._unsub = None
        self.stats = {"gifts": 0, "by_tier": {}}

    @property
    def memory(self):
        return self.brain.memory if self.brain else None

    async def start(self, ctx) -> None:
        self._load()
        self._unsub = self.bus.subscribe(Topics.LIVE_GIFT, self._on_gift)
        self.config.watch("gift_tiers.", self._load)

    async def stop(self) -> None:
        if self._unsub:
            self._unsub()

    def _load(self) -> None:
        self.tiers = list(self.config.get("gift_tiers.tiers") or [])
        self.custom = dict(self.config.get("gift_tiers.custom") or {})
        self.mode = str(self.config.get("gift_tiers.reply_mode", "template"))
        self.tiers.sort(key=lambda t: t.get("min", 0))

    def _tier_of(self, value: int) -> dict:
        tier = self.tiers[0] if self.tiers else {"name": "普通", "motion": "wave",
                                                 "template": "谢谢 {nickname} 的{gift_name}~"}
        for t in self.tiers:
            if value >= int(t.get("min", 0)):
                tier = t
        return tier

    # ---------------------------------------------------------------- 主链
    def _on_gift(self, ev: Event) -> None:
        d = ev.data
        nickname = d.get("nickname", "观众")
        gift_name = d.get("gift_name", "礼物")
        value = int(d.get("total_value", 0))
        self.stats["gifts"] += 1

        # 专属礼物规则优先（如玫瑰专属台词）
        rule = self.custom.get(gift_name) or {}
        tier = {"name": "专属", "motion": rule.get("motion", "heart"),
                "template": rule.get("template")} if rule else self._tier_of(value)
        tier_name = rule.get("tier_name", tier.get("name", "普通")) if rule else tier.get("name", "普通")
        self.stats["by_tier"][tier_name] = self.stats["by_tier"].get(tier_name, 0) + 1

        # 情绪：大礼多涨
        if self.memory:
            delta = int(self.config.get("brain.mood.gift_big" if value >= 100 else "brain.mood.gift_small", 3))
            old = self.memory.mood
            new = self.memory.mood_adjust(delta, f"礼物{gift_name}")
            self.bus.publish(Topics.BRAIN_MOOD, {"mood": new, "delta": new - old, "reason": "gift"},
                             source="gift_tiers")

        # 舞台动作（渲染引擎订阅）
        self.bus.publish(Topics.STAGE_COMMAND,
                         {"action": "motion", "motion": tier.get("motion", "wave"),
                          "gift": gift_name, "value": value}, source="gift_tiers")

        # 感谢词（限频：同观众同礼物 8s 内不重复谢）
        key = f"{d.get('user_id') or nickname}:{gift_name}"
        now = time.time()
        if now - self._last_thanks.get(key, 0) < 8:
            return
        self._last_thanks[key] = now
        tpl = tier.get("template") or "谢谢 {nickname} 的{gift_name}~"
        text = tpl.format(nickname=nickname, gift_name=gift_name, value=value)
        self.bus.publish(Topics.BRAIN_REPLY, {
            "text": text, "kind": "gift", "emotion": "happy" if value >= 100 else "neutral",
            "viewer": nickname, "is_template": self.mode != "model",
            "client_id": f"gift-{int(time.time()*1000)}-{random.randint(100,999)}",
        }, source="gift_tiers")

    def status(self) -> dict:
        return {"tiers": self.tiers, "mode": self.mode, **self.stats}


__all__ = ["GiftTiers"]
