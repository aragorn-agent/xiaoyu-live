# -*- coding: utf-8 -*-
"""EventCenter 单测：去重（msg_id / 内容指纹）、点赞与礼物连击聚合、直通事件。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.gifts import GiftCatalog
from core.live_events import EventCenter

LIKE_W = 0.4
COMBO_W = 0.5


def make_center() -> tuple[EventCenter, list]:
    got: list = []
    c = EventCenter(gift_catalog=GiftCatalog(), like_window=LIKE_W, combo_window=COMBO_W)
    c.on_event(got.append)
    return c, got


def wait_flush(window: float = COMBO_W) -> None:
    time.sleep(window + 0.35)


def test_msg_id_dedup():
    c, got = make_center()
    item = {"msg_type": 2, "msg_id": "m1", "nickname": "A", "sec_open_id": "ua", "content": "hi"}
    c.ingest(item, source="mock")
    c.ingest(dict(item), source="mock")
    assert len(got) == 1
    assert c.stats["dropped_dup"] == 1
    assert got[0].type == "comment"


def test_fingerprint_dedup_without_msg_id():
    c, got = make_center()
    item = {"msg_type": 2, "nickname": "A", "sec_open_id": "ua", "content": "hello",
            "timestamp": int(time.time() * 1000)}
    c.ingest(item, source="http")
    c.ingest(dict(item), source="http")  # 同内容同时间窗 → 指纹去重
    assert len(got) == 1
    item2 = dict(item)
    item2["content"] = "different"
    c.ingest(item2, source="http")       # 不同内容放行
    assert len(got) == 2


def test_gift_combo_postpone_does_not_deadlock():
    """回归：窗口内二次到达触发顺延路径，不得死锁（曾用非重入锁导致事件循环冻结）。"""
    c, got = make_center()
    base = {"msg_type": 3, "nickname": "A", "sec_open_id": "ua", "gift_name_plain": "玫瑰"}
    c.ingest({**base, "gift_num": 1, "msg_id": "c1"}, source="mock")
    time.sleep(COMBO_W * 0.8)            # 定时器即将到期
    c.ingest({**base, "gift_num": 1, "msg_id": "c2"}, source="mock")  # deadline 顺延
    wait_flush(1.2)                      # 等顺延后的定时器
    assert len(got) == 1
    assert got[0].gift_num == 2


def test_gift_combo_aggregation():
    c, got = make_center()
    base = {"msg_type": 3, "nickname": "大哥", "sec_open_id": "ub", "gift_name_plain": "玫瑰"}
    for n in (1, 2, 3):
        c.ingest({**base, "gift_num": n, "msg_id": f"g{n}"}, source="mock")
    assert not got                       # 还在窗口内
    wait_flush()
    assert len(got) == 1
    ev = got[0]
    assert ev.type == "gift"
    assert ev.gift_num == 6              # 1+2+3 合并
    assert ev.gift_name == "玫瑰"
    assert ev.gift_value > 0             # 名字兜底估价生效


def test_like_aggregation():
    c, got = make_center()
    base = {"msg_type": 1, "nickname": "A", "sec_open_id": "ua"}
    for n in (2, 3, 5):
        c.ingest({**base, "like_num": n, "msg_id": f"l{n}"}, source="mock")
    wait_flush(LIKE_W)
    assert len(got) == 1
    assert got[0].like_num == 10         # 2+3+5


def test_member_passes_through_immediately():
    c, got = make_center()
    c.ingest({"msg_type": 6, "nickname": "A", "sec_open_id": "ua", "member_count": 12}, source="mock")
    assert len(got) == 1
    assert got[0].type == "member"
    assert got[0].member_count == 12


def test_unknown_type_dropped():
    c, got = make_center()
    c.ingest({"msg_type": 99, "nickname": "A"}, source="mock")
    assert got == []


def test_close_cancels_pending():
    c, got = make_center()
    c.ingest({"msg_type": 3, "nickname": "A", "sec_open_id": "ua", "gift_name_plain": "玫瑰", "gift_num": 1}, source="mock")
    c.close()
    wait_flush()
    assert got == []                     # 关闭后不再发出
