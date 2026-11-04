# -*- coding: utf-8 -*-
"""弹幕小游戏：成语接龙 + 猜数字（轻量冷场救星，零依赖）。

- 接龙：发"接龙"开局，小鱼给成语 → 观众弹幕接（首字=上一条尾字，fuzzy 容错）→ 接不上小鱼认输
- 猜数字：发"猜数字"开局（1-100）→ 弹幕数字 → 太大/太小提示 → 猜中结算
"""
from __future__ import annotations

import random

from core.events import Event, Topics

IDIOMS = ["一心一意", "龙飞凤舞", "五湖四海", "海阔天空", "空中楼阁",
          "眉飞色舞", "画龙点睛", "雪中送炭", "水滴石穿", "金玉良缘"]


class MiniGames:
    name = "minigame"
    default_enabled = False

    def __init__(self, bus, config, brain=None) -> None:
        self.bus = bus
        self.config = config
        self.brain = brain
        self.mode = None             # "idiom" | "number"
        self.last_tail = ""
        self.target = 0
        self.guesses = 0
        self.stats = {"rounds": 0}

    @property
    def memory(self):
        return self.brain.memory if self.brain else None

    async def start(self, ctx) -> None:
        self._unsub = self.bus.subscribe(Topics.LIVE_CHAT, self._on_chat)

    async def stop(self) -> None:
        if self._unsub:
            self._unsub()

    def _say(self, text: str, emotion: str = "happy") -> None:
        self.bus.publish(Topics.BRAIN_REPLY, {
            "text": text[:60], "kind": "game", "emotion": emotion,
            "viewer": "", "is_template": True,
            "client_id": f"mg-{int(time.time()*1000)}-{random.randint(100,999)}",
        }, source="minigame")

    def _on_chat(self, ev: Event) -> None:
        d = ev.data or {}
        text = d.get("content", "").strip()
        nickname = d.get("nickname", "观众")
        if self.mode is None:
            if text == "接龙":
                self.mode = "idiom"
                self.last_tail = random.choice(IDIOMS)
                self.stats["rounds"] += 1
                self._say(f"成语接龙！小鱼先来：{self.last_tail}。下一个用『{self.last_tail[-1]}』开头！")
            elif text == "猜数字":
                self.mode = "number"
                self.target = random.randint(1, 100)
                self.guesses = 0
                self.stats["rounds"] += 1
                self._say("猜数字开！1 到 100，直接弹幕报数字，小鱼告诉你大小！")
            return
        if self.mode == "idiom":
            if text in ("结束", "不玩了"):
                self.mode = None
                self._say("行，接龙结束，你们输了～")
                return
            if text and text[0] == self.last_tail[-1] and len(text) >= 4:
                self.last_tail = text
                # 小鱼接（简单策略：找尾字匹配的词库成语；找不到就认输）
                nxt = next((i for i in IDIOMS if i[0] == text[-1]), None)
                if nxt:
                    self.last_tail = nxt
                    self._say(f"{text}？接得不错！小鱼接：{nxt}。继续！")
                else:
                    self.mode = None
                    self._say(f"{text}……小鱼接不上了！这局你们赢了，厉害！", "shy")
            elif len(text) >= 4:
                self._say(f"{text}？不对哦，要用『{self.last_tail[-1]}』开头才行！")
        elif self.mode == "number":
            if text in ("结束", "不玩了"):
                self.mode = None
                self._say("行，猜数字结束～")
                return
            if text.isdigit():
                self.guesses += 1
                n = int(text)
                if n == self.target:
                    self.mode = None
                    self._say(f"{n}！就是它！{nickname} 用了 {self.guesses} 次猜中，厉害！", "happy")
                    if self.memory:
                        self.memory.mood_adjust(5, "猜数字有人猜中")
                elif n < self.target:
                    self._say(f"{n}？小了！往大猜！")
                else:
                    self._say(f"{n}？大了！往小猜！")

    def status(self) -> dict:
        return {"mode": self.mode, "tail": self.last_tail, **self.stats}


__all__ = ["MiniGames", "IDIOMS"]
