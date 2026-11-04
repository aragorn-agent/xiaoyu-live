# -*- coding: utf-8 -*-
"""HTTP relay 归一化单测。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingress.source_http import normalize_relay


def test_xcool_style_danmu():
    out = normalize_relay({"type": "danmu", "username": "路人甲", "content": "你好"})
    assert out["msg_type"] == 2
    assert out["nickname"] == "路人甲"
    assert out["sec_open_id"].startswith("relay:")


def test_xcool_style_gift():
    out = normalize_relay({"type": "gift", "username": "大哥", "giftName": "跑车", "giftCount": 2})
    assert out["msg_type"] == 3
    assert out["gift_name_plain"] == "跑车"
    assert out["gift_num"] == 2


def test_official_numeric_type():
    out = normalize_relay({"msg_type": 6, "nickname": "A"})
    assert out["msg_type"] == 6


def test_unknown_string_defaults_to_comment():
    out = normalize_relay({"type": "whatever", "nickname": "A", "content": "x"})
    assert out["msg_type"] == 2
