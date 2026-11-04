# -*- coding: utf-8 -*-
"""Mock 弹幕源：本地全链路开发/验收用，不碰真直播间。

模拟真实节奏：进房→弹幕→点赞连击→关注→礼物连击，循环播放剧本。
配置：config.modules.ingress_mock.enabled + ingress_mock.interval
"""
from __future__ import annotations

import asyncio
import random

SCRIPT = [
    {"msg_type": 6, "nickname": "阿拉贡", "sec_open_id": "u_alagon"},        # 进房
    {"msg_type": 2, "nickname": "阿拉贡", "sec_open_id": "u_alagon",
     "content": "小鱼今天状态不错啊"},
    {"msg_type": 6, "nickname": "路人甲", "sec_open_id": "u_a1"},
    {"msg_type": 2, "nickname": "路人甲", "sec_open_id": "u_a1",
     "content": "主播声音好好听"},
    {"msg_type": 1, "nickname": "路人甲", "sec_open_id": "u_a1", "like_num": 3},
    {"msg_type": 1, "nickname": "路人甲", "sec_open_id": "u_a1", "like_num": 2},
    {"msg_type": 5, "nickname": "路人甲", "sec_open_id": "u_a1",
     "user_follow_action": 1},                                              # 关注
    {"msg_type": 3, "nickname": "阿拉贡", "sec_open_id": "u_alagon",
     "gift_name_plain": "玫瑰", "gift_num": 1},                             # 礼物
    {"msg_type": 3, "nickname": "阿拉贡", "sec_open_id": "u_alagon",
     "gift_name_plain": "玫瑰", "gift_num": 2},                             # 连击+2
    {"msg_type": 3, "nickname": "阿拉贡", "sec_open_id": "u_alagon",
     "gift_name_plain": "跑车", "gift_num": 1},                             # 大礼
    {"msg_type": 2, "nickname": "阿拉贡", "sec_open_id": "u_alagon",
     "content": "记住我最喜欢的歌是夜曲"},                                   # 显式记忆
    {"msg_type": 7, "member_count": 66},                                    # 在线人数
]


class MockSource:
    name = "ingress_mock"
    default_enabled = True

    def __init__(self, center) -> None:
        self.center = center
        self._task: asyncio.Task | None = None
        self._running = False
        self.interval = 2.5          # 事件间隔（秒）
        self.once = False            # True=剧本播完即停

    def configure(self, interval: float, once: bool = False) -> None:
        self.interval = max(0.3, float(interval))
        self.once = bool(once)

    async def start(self, ctx) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="ingress_mock")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        i = 0
        try:
            while self._running:
                item = dict(SCRIPT[i % len(SCRIPT)])
                i += 1
                item.setdefault("msg_id", f"mock-{i}-{random.randint(1000, 9999)}")
                self.center.ingest(item, source="mock")
                if self.once and i >= len(SCRIPT):
                    break
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            raise


__all__ = ["MockSource", "SCRIPT"]
