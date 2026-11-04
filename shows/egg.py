# -*- coding: utf-8 -*-
"""关键词彩蛋：零 token 的即时角色反应，填补 LLM 延迟期的互动空白。

设计原则（对齐"傲娇小肥鱼"人设）：
- 每个彩蛋有全局冷却 + 每人冷却，防止刷屏
- 只在 LLM 回复忙不过来时插队也安全：彩蛋走 BRAIN_REPLY(is_template)，语音链路统一
- 触发词是观众玩梗的抓手（这是留人手段，不是玩具）
"""
from __future__ import annotations

import random
import re
import time

from core.events import Event, Topics

# (正则, 回复列表, 动作, 表情, 全局冷却秒, 权重)
_EGGS = [
    (r"摸鱼|摸摸鱼头|rua", ["喂！鱼头也是你能摸的吗……算、算你了。", "再摸就要从鱼变咸鱼了哼！"],
     "heart", "shy", 60, 3),
    (r"晚安|睡了", ["哼，晚安……梦里别欺负小鱼。", "去吧去吧，鱼才不会想你。"],
     "wave", "shy", 90, 2),
    (r"哈哈{2,}|笑死|哈哈哈哈", ["笑什么笑！鱼今天很好笑吗？！……好吧确实。", "哼，笑吧笑吧，雷霆不跟你们计较。"],
     "wave", "happy", 45, 2),
    (r"来了来了|刚来|路过", ["坐好坐好，故事马上开讲～", "来的都是缘，坐下别走呀。"],
     "wave", "happy", 60, 1),
    (r"真的假的|不信", ["不信就看着，鱼用雷霆担保！", "爱信不信，反正鱼很行。"],
     "heart", "tsundere", 60, 1),
    (r"主播|小鱼好可爱|小鱼好看", ["才、才没有很可爱！……心情值暴露了。", "眼睛还挺尖的嘛。"],
     "heart", "shy", 90, 2),
]


class EggTrigger:
    name = "egg"
    default_enabled = True

    def __init__(self, bus, config) -> None:
        self.bus = bus
        self.config = config
        self._last_global: dict[str, float] = {}
        self._last_user: dict[tuple, float] = {}
        self._unsub = None
        self.stats = {"hits": 0}

    async def start(self, ctx) -> None:
        self._unsub = self.bus.subscribe(Topics.LIVE_CHAT, self._on_chat)

    async def stop(self) -> None:
        if self._unsub:
            self._unsub()

    def _on_chat(self, ev: Event) -> None:
        d = ev.data or {}
        text = d.get("content", "")
        uid = d.get("user_id") or d.get("nickname") or "anon"
        now = time.time()
        for pat, replies, motion, emotion, gcool, weight in _EGGS:
            if not re.search(pat, text, re.I):
                continue
            if now - self._last_global.get(pat, 0) < gcool:
                return                      # 任何彩蛋命中即结束本轮（不叠加）
            ukey = (pat, uid)
            if now - self._last_user.get(ukey, 0) < gcool * 3:
                return
            self._last_global[pat] = now
            self._last_user[ukey] = now
            self.stats["hits"] += 1
            self.bus.publish(Topics.STAGE_COMMAND, {"action": "motion", "motion": motion},
                             source="egg")
            self.bus.publish(Topics.BRAIN_REPLY, {
                "text": random.choice(replies), "kind": "egg", "emotion": emotion,
                "viewer": d.get("nickname", ""), "is_template": True,
                "client_id": f"egg-{int(now*1000)}-{random.randint(100,999)}",
            }, source="egg")
            return

    def status(self) -> dict:
        return dict(self.stats)


__all__ = ["EggTrigger", "_EGGS"]
