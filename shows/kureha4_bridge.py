# -*- coding: utf-8 -*-
"""kureha4 三件套接线：点歌盒 jukebox / 记账 ledger / 礼物音效 sfx（MIT 直搬）。

dafeiyu 式适配：cfg_getter 闭包读本项目 config；sfx 资产指 characters/sfx。
- jukebox：弹幕点歌（冷却/黑名单/BGM 垫乐/淡入淡出），mpv 播放——可选依赖
- ledger：米币记账（礼物收入 + token 成本）
- sfx：礼物音效（assets_dir 放 wav/mp3，按礼物名命中即播）
"""
from __future__ import annotations

from core.config import ROOT


class Kureha4Bridge:
    name = "kureha4_shows"
    default_enabled = False     # 选配开关：modules.kureha4_shows

    def __init__(self, bus, config, gifts) -> None:
        self.bus = bus
        self.config = config
        self.gifts = gifts
        self.jukebox = None
        self.ledger = None
        self.sfx = None
        self._unsub = None
        self.stats = {"sfx_played": 0}

    def _cfg(self, key, default=None):
        return self.config.get(key, default)

    async def start(self, ctx) -> None:
        from shows.jukebox import Jukebox
        from shows.ledger import RiceLedger
        from shows.sfx import GiftSfx
        self.ledger = RiceLedger(str(ROOT / "data" / "ledger.json"),
                                 lambda k, d=None: self._cfg(k, d))
        self.sfx = GiftSfx(lambda k, d=None: self._cfg(k, d),
                           str(ROOT / "characters" / "sfx"), print)
        self.jukebox = Jukebox(lambda k, d=None: self._cfg(k, d), print,
                               on_speak=self._on_speak)
        self._unsub = self.bus.subscribe_many(
            ["live.gift", "live.comment"], self._on_event)

    async def stop(self) -> None:
        if self._unsub:
            self._unsub()

    def _on_speak(self, text: str) -> None:
        from core.events import Topics
        self.bus.publish(Topics.BRAIN_REPLY, {
            "text": text[:60], "kind": "command", "emotion": "happy",
            "viewer": "", "is_template": True,
            "client_id": f"jb-{int(time*1000)}",
        }, source="jukebox")

    def _on_event(self, ev) -> None:
        d = ev.data or {}
        kind = d.get("type")
        if kind == "gift":
            value = int(d.get("total_value", 0))
            if self.ledger:
                self.ledger.income(value, d.get("gift_num", 1))
            if self.sfx:
                self.sfx.play()
                self.stats["sfx_played"] += 1
        elif kind == "comment" and self.jukebox:
            # 点歌拦截：返回 True 表示已处理（jukebox 内部直接播报）
            if self.jukebox.handle_comment(d.get("nickname", "观众"), d.get("content", "")):
                self.stats["sfx_played"] += 0

    def status(self) -> dict:
        return {"ledger": self.ledger.snapshot() if self.ledger else {},
                "jukebox": bool(self.jukebox), "sfx_dir": bool(self.sfx)}


import time  # noqa: E402
__all__ = ["Kureha4Bridge"]
