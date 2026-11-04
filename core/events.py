# -*- coding: utf-8 -*-
"""事件定义：层与层之间只传事件，不互相 import 具体实现。

来源：adapted from lxllxl233/dafeiyu-ai-live src/dafeiyu/core/events.py (MIT License)。
按本项目主题分层做了扩充；保留原版权声明。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class Event:
    """一条直播事件。

    topic 用点号分层，订阅端可以用 ``live.*`` 这样的通配符。
    data 只放可 JSON 序列化的值（要进日志、要推 UI）。
    """

    topic: str
    data: Mapping[str, Any] = field(default_factory=dict)
    source: str = ""
    ts: float = field(default_factory=time.time)

    def with_source(self, source: str) -> "Event":
        return Event(topic=self.topic, data=dict(self.data), source=source, ts=self.ts)

    def to_dict(self) -> dict:
        return {"topic": self.topic, "data": dict(self.data), "source": self.source, "ts": self.ts}


class Topics:
    """订阅用的主题常量（避免各层手打字符串写错）。"""

    # 会话
    SESSION_STARTED = "session.started"
    SESSION_STOPPED = "session.stopped"
    SESSION_BOOT = "session.boot"          # 开播清场

    # 直播互动（由 ingress 归一化后发布）
    LIVE_EVENT = "live.event"              # 全部互动事件（归一化 LiveEvent dict）
    LIVE_CHAT = "live.chat"                # 评论
    LIVE_GIFT = "live.gift"                # 礼物（连击聚合后）
    LIVE_MEMBER = "live.member"            # 进场
    LIVE_FOLLOW = "live.follow"            # 关注
    LIVE_LIKE = "live.like"                # 点赞（窗口聚合后）
    LIVE_FANSCLUB = "live.fansclub"
    LIVE_STATS = "live.stats"              # 在线人数

    # 大脑 / 回复
    BRAIN_REPLY = "brain.reply"            # 一条要念出来的回复 {text, emotion, kind, client_id}
    BRAIN_MOOD = "brain.mood"              # mood 变化 {mood, delta, reason}
    BRAIN_MEMORY = "brain.memory"          # 记忆写入 {viewer, key, value}

    # 语音
    VOICE_STARTED = "voice.started"
    VOICE_READY = "voice.ready"            # {speech_id, url, timings[], text}
    VOICE_FAILED = "voice.failed"
    VOICE_SPEAKING = "voice.speaking"      # true/false（压音用）
    VOICE_SEGMENT = "voice.segment"        # {client_id, seq, text, emotion} 流式切句请求
    VOICE_SEG_READY = "voice.seg_ready"    # {client_id, seq, url, text} 单句合成完毕

    # 节目
    SHOW_MUSIC = "show.music"              # {action: play/stop/skip, song}
    SHOW_STORY = "show.story"              # 故事电台事件
    SHOW_COOLDOWN = "show.cooldown"        # 防冷场触发

    # 舞台 / 渲染
    STAGE_COMMAND = "stage.command"        # {action: motion/express/scene/light, ...}

    # 系统
    SYS_LOG = "sys.log"
    # 所有事件（调试用）
    ALL = "*"


__all__ = ["Event", "Topics"]
