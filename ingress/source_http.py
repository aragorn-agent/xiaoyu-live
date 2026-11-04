# -*- coding: utf-8 -*-
"""HTTP relay 弹幕入口：兼容任意第三方抓取工具（XCool 的边界设计）。

约定 POST /api/ingest  {msg_type|type, nickname, content, ...}
    type 可用数字（1点赞/2评论/3礼物/4粉丝团/5关注/6进房/7统计）
    也接受字符串 "like"/"comment"/"gift"/"fansclub"/"follow"/"member"/"stats"
    以及 XCool relay.js 风格 {type:"danmu"|"gift"|"join"|"like", username, content}
"""
from __future__ import annotations

from core.live_events import MsgType

_STR_TYPE = {
    "like": MsgType.LIKE, "comment": MsgType.COMMENT, "chat": MsgType.COMMENT,
    "danmu": MsgType.COMMENT, "message": MsgType.COMMENT,
    "gift": MsgType.GIFT, "fansclub": MsgType.FANSCLUB,
    "follow": MsgType.FOLLOW, "social": MsgType.FOLLOW,
    "member": MsgType.MEMBER, "join": MsgType.MEMBER, "enter": MsgType.MEMBER,
    "stats": MsgType.ROOM_STATS, "room_stats": MsgType.ROOM_STATS,
}


def normalize_relay(payload: dict) -> dict:
    """把第三方 relay 的载荷归一化成官方 payload 同构 dict。不认识的字段保留在 raw。"""
    out: dict = dict(payload)

    t = payload.get("msg_type", payload.get("type", 2))
    if isinstance(t, str):
        mt = _STR_TYPE.get(t.strip().lower()) or MsgType.COMMENT
    else:
        try:
            mt = MsgType(int(t))
        except (TypeError, ValueError):
            mt = MsgType.COMMENT
    out["msg_type"] = int(mt)

    # XCool relay 风格字段 → 官方字段
    if payload.get("username") and not out.get("nickname"):
        out["nickname"] = payload["username"]
    if mt == MsgType.GIFT and payload.get("giftName") and not payload.get("gift_name_plain"):
        out["gift_name_plain"] = payload["giftName"]
    if mt == MsgType.GIFT and payload.get("giftCount") and not payload.get("gift_num"):
        out["gift_num"] = int(payload["giftCount"] or 1)
    if not payload.get("sec_open_id") and out.get("nickname"):
        out["sec_open_id"] = f"relay:{out['nickname']}"

    out["msg_id"] = str(payload.get("msg_id") or payload.get("eventId") or "")  # 空则走指纹去重
    return out


class HttpIngress:
    """不是后台任务，是被 server 路由调用的处理器集合。"""
    name = "ingress_http"
    default_enabled = True

    def __init__(self, center) -> None:
        self.center = center

    async def start(self, ctx) -> None:  # noqa: ARG002
        return None

    async def stop(self) -> None:
        return None

    def handle_payload(self, payload: dict) -> int:
        item = normalize_relay(payload)
        self.center.ingest(item, source="http")
        return int(item["msg_type"])


__all__ = ["HttpIngress", "normalize_relay"]
