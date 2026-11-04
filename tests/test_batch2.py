# -*- coding: utf-8 -*-
"""批次2单测：养成等级/榜一、网易云 probe、伴侣IPC mock、跳舞正则。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.bus import EventBus
from core.config import Config
from core.gifts import GiftCatalog
from core.live_events import EventCenter


def _cfg(tmp_path, extra=""):
    (tmp_path / "config.yaml").write_text(extra or "app: {}\n", encoding="utf-8")
    return Config(config_dir=tmp_path)


def test_growth_levels_and_persist(tmp_path):
    from shows.growth import Growth
    g = Growth(EventBus(), _cfg(tmp_path))
    g.path = tmp_path / "growth.json"          # 重定向落盘
    g.data = {"weight": 180, "contributions": {}}   # 隔离真实运行数据
    old_idx, old_lv, _ = g.level_of(g.data["weight"])
    class E:  # 最小事件壳
        data = {"type": "gift", "nickname": "大哥", "user_id": "u1", "total_value": 200}
    g._on_event(E())
    assert g.data["weight"] >= 180 + 1000      # 200抖币 × 5
    assert g.top(1)[0]["nickname"] == "大哥"
    g.save()
    from shows.growth import Growth as G2
    g2 = G2(EventBus(), _cfg(tmp_path))
    g2.path = tmp_path / "growth.json"
    g2.load()
    assert g2.data["weight"] == g.data["weight"]   # 重启不丢（修 XCool 缺陷）


def test_growth_levelup_event(tmp_path):
    from shows.growth import Growth
    bus = EventBus()
    g = Growth(bus, _cfg(tmp_path))
    g.path = tmp_path / "growth.json"
    g.data = {"weight": 180, "contributions": {}}
    fired = []
    bus.subscribe("stage.command", lambda e: fired.append(e.data))
    bus.subscribe("brain.reply", lambda e: fired.append({"reply": e.data["text"]}))
    class E:
        data = {"type": "gift", "nickname": "A", "user_id": "ua", "total_value": 2000}
    g._on_event(E())   # 180 + 10000 → 直上传说之鱼
    assert any(f.get("effect") == "levelup" for f in fired)
    assert any("传说之鱼" in str(f.get("reply", "")) for f in fired)


def test_partner_ipc_mock_roundtrip():
    import time as _t
    from core.live_events import EventCenter
    from output.partner_ipc import PartnerIpcIngress, MockPipeClient
    center = EventCenter(gift_catalog=GiftCatalog())
    got = []
    center.on_event(got.append)
    p = PartnerIpcIngress(center, _cfg(Path(__file__).parent, "app: {}"), GiftCatalog())
    p.client = MockPipeClient()
    p.client.set_event_handler(p._on_partner_event)
    p.inject(2, nickname="A", sec_open_id="ua", content="ipc你好", msg_id="ipc1")
    p.inject(3, nickname="A", sec_open_id="ua", gift_name_plain="玫瑰", gift_num=2, msg_id="ipc2")
    _t.sleep(3.4)   # 礼物连击窗口（3s）到点才发出
    types = [e.type for e in got]
    assert "comment" in types and "gift" in types
    gift = [e for e in got if e.type == "gift"][0]
    assert gift.gift_name == "玫瑰" and gift.gift_num == 2


def test_dance_regex():
    from shows.dance import _DANCE_RE
    assert _DANCE_RE.search("来个舞吧")
    assert _DANCE_RE.search("极乐净土！")
    assert not _DANCE_RE.search("今天吃什么")


def test_netease_provider_shape(tmp_path):
    from shows.music import NeteaseProvider
    # 离线环境返回空列表不抛异常即可
    p = NeteaseProvider()
    assert isinstance(p.search("夜曲"), list)
