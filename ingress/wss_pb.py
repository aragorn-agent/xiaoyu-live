# -*- coding: utf-8 -*-
"""webcast protobuf 最小解码器（零 .proto / 零 protobuf 库依赖）。

来源：adapted from kureha4/dafeiyuzhibodouyinbanlv liveplugin/wss_pb.py (MIT License)。
保留原版权声明；字段号为 2026 现网实测校准。

只做 wire-format 级别的按字段号提取，够把常见文本/整数/嵌套 user 结构取出来。
若后续拿到官方 webcast.proto，可整体替换为强类型解析，本模块接口不变。
"""
from __future__ import annotations


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("varint out of range")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def _encode_varint(v: int) -> bytes:
    out = b""
    while True:
        b = v & 0x7F
        v >>= 7
        out += bytes([b | (0x80 if v else 0)])
        if not v:
            return out


def decode_fields(buf: bytes) -> dict[int, list]:
    """把一段 protobuf 拆成 {field_number: [values...]}。
    varint→int, 定长字节→bytes, 字符串字段由调用方按需 decode。"""
    out: dict[int, list] = {}
    pos = 0
    n = len(buf)
    while pos < n:
        try:
            key, pos = _read_varint(buf, pos)
        except ValueError:
            break
        field_no, wt = key >> 3, key & 7
        if wt == 0:
            val, pos = _read_varint(buf, pos)
        elif wt == 1:
            val = buf[pos:pos + 8]
            pos += 8
        elif wt == 2:
            ln, pos = _read_varint(buf, pos)
            val = buf[pos:pos + ln]
            pos += ln
        elif wt == 5:
            val = buf[pos:pos + 4]
            pos += 4
        else:
            break        # 不支持的 wire type（如 group），保守停止
        out.setdefault(field_no, []).append(val)
    return out


# ---------------------------------------------------------------------------
# PushFrame（服务端 ws 帧外层信封）
#   field1 seq(varint) field2 logid(str) field7 method(str)
#   field8 payload(bytes) field9 payload_encoding(str)
# ---------------------------------------------------------------------------
def decode_push_frame(raw: bytes) -> dict:
    f = decode_fields(raw)
    def s(field_no: int) -> str:
        v = f.get(field_no, [])
        return v[0].decode("utf-8", "replace") if v and isinstance(v[0], bytes) else ""
    return {
        "seq": f.get(1, [0])[0] if f.get(1) else 0,
        "log_id": s(2),
        "method": s(7),
        "payload": f.get(8, [b""])[0] if f.get(8) else b"",
        "payload_encoding": s(9) or "utf-8",
    }


# ---------------------------------------------------------------------------
# Response（PushFrame.payload 解压后的内层）
#   field1 messages(repeated, 每条含 method + payload)
# ---------------------------------------------------------------------------
def decode_response(payload: bytes) -> dict:
    out: dict = {"messages": []}
    if not payload:
        return out
    f = decode_fields(payload)
    for blob in f.get(1, []):
        mf = decode_fields(blob)
        method = mf.get(1, [b""])[0].decode("utf-8", "replace") if mf.get(1) else ""
        body = mf.get(2, [b""])[0] if mf.get(2) else b""
        out["messages"].append({"method": method, "payload": body})
    return out


# ---------------------------------------------------------------------------
# 常见消息体字段速查（2026 现网实测，live.douyin.com web 端）：
#   ChatMessage:   field2=User, field3=content
#   MemberMessage: field2=User, field3=member_count(在线人数)
#   LikeMessage:   field2=count, field5=User
#   Social(follow):field2/5=User
#   GiftMessage:   field3=gift_id, field4=count, field5=User, field12=gift(GiftObj)
#                  field22=repeat_count/total
#   User:   field1=uid(int), field3=nickname, field9/12=avatar(Image), field38=sec_uid
# 历史/其他端字段号不同，故保留候选列表兜底。field1 恒为 method envelope，不参与 User 候选。
# ---------------------------------------------------------------------------
def _decode_user(blob: bytes) -> dict:
    f = decode_fields(blob)
    urls = [v.decode("utf-8", "replace") for v in _sub(f.get(9, []) + f.get(12, []), 3)]
    if not urls:                       # 新结构 avatar 直接是 image，url 在 sub field1
        urls = [v.decode("utf-8", "replace") for v in _sub(f.get(9, []), 1)]
    uid = ""
    if f.get(1) and isinstance(f[1][0], int):
        uid = str(f[1][0])
    return {
        "uid": uid,
        "nickname": _str(f, 3),
        "sec_uid": _str(f, 38),
        "avatar_thumb": {"url_list": urls},
    }


def _sub(blobs: list, field_no: int) -> list:
    out = []
    for blob in blobs:
        if not isinstance(blob, (bytes, bytearray)):
            continue
        out.extend(decode_fields(blob).get(field_no, []))
    return out


def _str(f: dict, field_no: int) -> str:
    v = f.get(field_no, [])
    return v[0].decode("utf-8", "replace") if v and isinstance(v[0], bytes) else ""


def _int(f: dict, field_no: int, default: int = 0) -> int:
    v = f.get(field_no, [])
    return v[0] if v and isinstance(v[0], int) else default


def decode_message_fields(payload: bytes) -> dict:
    """把单条业务消息解成浅层 dict（source_cdp._convert 的输入）。"""
    if not payload:
        return {}
    f = decode_fields(payload)
    out: dict = {}
    for user_fno in (2, 5, 6, 8, 10, 7):
        blobs = f.get(user_fno, [])
        if blobs and isinstance(blobs[0], (bytes, bytearray)) and len(blobs[0]) > 8:
            u = _decode_user(blobs[0])
            if u["nickname"]:
                out["user"] = u
                break
    out["content"] = _str(f, 3) or _str(f, 5) or _str(f, 12)
    out["count"] = _int(f, 2) or _int(f, 3) or _int(f, 5)          # Like
    out["total_count"] = _int(f, 22) or _int(f, 4) or _int(f, 3)   # Gift 累计
    out["gift_num"] = _int(f, 4) or _int(f, 12)
    out["member_count"] = _int(f, 3)                               # Member 在线人数
    for gift_fno in (12, 5, 7, 3):                                 # Gift 对象
        gb = f.get(gift_fno, [])
        if gb and isinstance(gb[0], (bytes, bytearray)) and len(gb[0]) > 4:
            gname = _str(decode_fields(gb[0]), 1)
            if gname:
                out["gift"] = {"name": gname}
                break
    out["uid"] = _str(f, 9)
    out["online_user_count"] = _int(f, 7) or _int(f, 2)            # RoomUserSeq
    out["sec_uid"] = out.get("user", {}).get("sec_uid", "")
    return out


__all__ = ["decode_fields", "decode_push_frame", "decode_response", "decode_message_fields"]
