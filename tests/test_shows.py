# -*- coding: utf-8 -*-
"""节目模块单测：礼物档位（回执定档）/ 点歌队列规则。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.bus import EventBus
from core.config import Config


class FakeBrain:
    def __init__(self):
        from brain.memory import MemoryStore
        self.memory = MemoryStore(data_file=str(Path(__file__).parent / "_tmp_mem.json"))


def make_tiers(tmp_path, tiers_yaml=""):
    (tmp_path / "config.yaml").write_text(
        "gift_tiers:\n" + tiers_yaml + "\nmusic:\n  lib_dir: 'characters/music'\n",
        encoding="utf-8")
    cfg = Config(config_dir=tmp_path)
    bus = EventBus()
    from shows.gift_tiers import GiftTiers
    g = GiftTiers(bus, cfg, brain=FakeBrain())
    g._load()
    return g, bus


def test_tier_by_receipt_value(tmp_path):
    g, bus = make_tiers(tmp_path, """  tiers:
    - {name: 普通, min: 0, motion: wave, template: "谢谢 {nickname} 的{gift_name}"}
    - {name: 惊喜, min: 100, motion: dance, template: "{nickname} 大佬！"}
    - {name: 排面, min: 1000, motion: dance, template: "{nickname} 排面！"}""")
    assert g._tier_of(1)["name"] == "普通"
    assert g._tier_of(100)["name"] == "惊喜"
    assert g._tier_of(30000)["name"] == "排面"


def test_custom_gift_rule_wins(tmp_path):
    g, bus = make_tiers(tmp_path, """  tiers:
    - {name: 普通, min: 0, motion: wave, template: "谢谢 {nickname} 的{gift_name}"}
  custom:
    玫瑰: {motion: heart, template: "玫瑰专属台词 {nickname}"}""")
    replies, commands = [], []
    bus.subscribe("brain.reply", lambda e: replies.append(e.data))
    bus.subscribe("stage.command", lambda e: commands.append(e.data))
    g._on_gift(type("E", (), {"data": {"nickname": "A", "gift_name": "玫瑰",
                                       "total_value": 1, "user_id": "u1", "gift_num": 1}})())
    assert replies and "玫瑰专属台词" in replies[0]["text"]
    assert commands[0]["motion"] == "heart"


def test_music_order_and_rules(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "music:\n  max_pending: 2\n  max_pending_per_user: 1\n  replay_cooldown: 600\n",
        encoding="utf-8")
    cfg = Config(config_dir=tmp_path)
    bus = EventBus()
    from shows.music import MusicService
    m = MusicService(bus, cfg)
    # 假曲库
    m.lib = [{"id": "a.mp3", "title": "夜曲", "artist": "周杰伦", "path": "x"},
             {"id": "b.mp3", "title": "晴天", "artist": "周杰伦", "path": "y"}]
    r1 = m.order("夜曲", user_id="u1", nickname="A")
    assert "排上了" in r1
    r2 = m.order("晴天", user_id="u1", nickname="A")
    assert "生气" in r2                      # 每人上限 1
    r3 = m.order("夜曲", user_id="u2", nickname="B")
    assert "排上了" in r3                      # 队列第 2 首，未满
    r4 = m.order("晴天", user_id="u3", nickname="C")
    assert "满啦" in r4                       # 队列上限 2 → 拒绝


def test_intercept_regex():
    from shows.music import _ORDER_RE, _SKIP_RE
    assert _ORDER_RE.match("点歌 夜曲")
    assert _ORDER_RE.match("来一首晴天").group(1) == "晴天"
    assert not _ORDER_RE.match("你好")
    assert _SKIP_RE.match("切歌")
