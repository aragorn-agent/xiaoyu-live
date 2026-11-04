# -*- coding: utf-8 -*-
"""CDP 弹幕源离线单测：手工构造 protobuf 帧 → decode_frames → 归一化。

不碰真直播间，验证的是我们自己最容易写错的解帧管线。
"""
import gzip
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingress.source_cdp import decode_frames, normalize_room_url  # noqa: E402
from ingress.wss_pb import decode_fields, _encode_varint  # type: ignore  # noqa: E402


def _field(n: int, wt: int) -> bytes:
    return _encode_varint((n << 3) | wt)


def _s(n: int, value: str) -> bytes:
    b = value.encode("utf-8")
    return _field(n, 2) + _encode_varint(len(b)) + b


def _v(n: int, value: int) -> bytes:
    return _field(n, 0) + _encode_varint(value)


def _sub(n: int, body: bytes) -> bytes:
    return _field(n, 2) + _encode_varint(len(body)) + body


def _user(n: int, uid: int, nickname: str, sec_uid: str = "sec123") -> bytes:
    return _sub(n, _v(1, uid) + _s(3, nickname) + _s(38, sec_uid))


def _chat_frame(content: str = "你好小鱼", nickname: str = "路人甲") -> bytes:
    chat = _user(2, 42, nickname) + _s(3, content)
    msg = _s(1, "WebcastChatMessage") + _sub(2, chat)
    resp = _sub(1, msg)
    return _v(1, 7) + _sub(8, gzip.compress(resp)) + _s(9, "gzip")


def _gift_frame(nickname: str = "大哥", gift: str = "跑车", num: int = 3) -> bytes:
    gift_obj = _s(1, gift)
    body = _user(5, 9, nickname) + _sub(12, gift_obj) + _v(22, num)
    msg = _s(1, "WebcastGiftMessage") + _sub(2, body)
    resp = _sub(1, msg)
    return _sub(8, gzip.compress(resp)) + _s(9, "gzip")


def _member_frame(nickname: str = "新朋友", online: int = 88) -> bytes:
    body = _user(2, 7, nickname) + _v(3, online)
    msg = _s(1, "WebcastMemberMessage") + _sub(2, body)
    resp = _sub(1, msg)
    return _sub(8, gzip.compress(resp))


def test_decode_chat_frame():
    seq = [0]
    items = decode_frames(_chat_frame(), seq)
    assert len(items) == 1
    ev = items[0]
    assert ev["msg_type_str"] == "live_comment"
    assert ev["nickname"] == "路人甲"
    assert ev["content"] == "你好小鱼"
    assert ev["sec_open_id"] == "sec123"
    assert ev["msg_id"].startswith("cdp-")


def test_decode_gift_frame():
    items = decode_frames(_gift_frame())
    assert items[0]["msg_type_str"] == "live_gift"
    assert items[0]["gift_name_plain"] == "跑车"
    assert items[0]["gift_num"] == 3


def test_decode_member_frame():
    items = decode_frames(_member_frame())
    assert items[0]["msg_type_str"] == "live_member"
    assert items[0]["member_count"] == 88


def test_unknown_method_dropped():
    msg = _s(1, "WebcastSomethingElse") + _sub(2, b"\x00" * 16)
    resp = _sub(1, msg)
    frame = _sub(8, gzip.compress(resp))
    assert decode_frames(frame) == []


def test_garbage_frame_safe():
    assert decode_frames(b"\xff\xff\xff") == []   # 不抛异常


def test_normalize_room_url():
    assert normalize_room_url("93059149915") == "https://live.douyin.com/93059149915"
    assert normalize_room_url("https://live.douyin.com/123") == "https://live.douyin.com/123"
    assert normalize_room_url("复制这段话 https://v.douyin.com/abc/ 打开抖音")
    try:
        normalize_room_url("不是链接")
        raise AssertionError("should raise")
    except ValueError:
        pass
