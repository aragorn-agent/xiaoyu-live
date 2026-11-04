# -*- coding: utf-8 -*-
"""纪念品系统（ds-live 验证的设计，自实现）：大礼赠签名/礼物永久归档，观众收藏页。

- 送出惊喜档（≥100 抖币）礼物 → 生成"小鱼签名卡"归档（SVG 贝塞尔笔迹可复现）
- 每位观众的名片页 /souvenir?uid=xxx 可翻自己收到的纪念品
- 归档落盘 data/souvenirs.json
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from core.events import Event, Topics

_TIER_MIN = 100   # 纪念品门槛（抖币）


def _signature_svg(text: str, signer: str = "小鱼") -> str:
    """确定性贝塞尔笔迹签名（ds-live 设计：同文本可复现）。"""
    seed = int(hashlib.md5(text.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)
    paths = []
    x = 30
    for ch in text[:12]:
        w = 28 + rng.randint(0, 14)
        dy = rng.randint(-8, 8)
        paths.append(f"M{x},42 q{w//3},{dy} {w//2},{dy//2} t{w//2},{-dy//2}")
        x += w + 6
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{x+40}" height="90">'
            f'<rect width="100%" height="100%" fill="#0c1b33" rx="10"/>'
            f'<path d="{" ".join(paths)}" stroke="#5eead4" stroke-width="2.5" fill="none" '
            f'stroke-linecap="round"/>'
            f'<text x="20" y="75" fill="#dce6f5" font-size="13" font-family="sans-serif">'
            f'小鱼 亲笔 · {signer} 周年纪念</text></svg>')


import random  # noqa: E402


class Souvenirs:
    name = "souvenir"
    default_enabled = False

    def __init__(self, bus, config) -> None:
        self.bus = bus
        self.config = config
        self.path = Path("data/souvenirs.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = {"items": []}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        self._unsub = None
        self.stats = {"archived": 0}

    async def start(self, ctx) -> None:
        self._unsub = self.bus.subscribe(Topics.LIVE_GIFT, self._on_gift)

    async def stop(self) -> None:
        if self._unsub:
            self._unsub()

    def _on_gift(self, ev: Event) -> None:
        d = ev.data or {}
        value = int(d.get("total_value", 0))
        if value < _TIER_MIN:
            return
        uid = d.get("user_id") or d.get("nickname") or "anon"
        nickname = d.get("nickname", "观众")
        gift = d.get("gift_name", "礼物")
        sig_text = f"{nickname}·{gift}·{time.strftime('%m-%d')}"
        svg = _signature_svg(sig_text, nickname)
        sig_name = f"sig_{hashlib.md5(sig_text.encode()).hexdigest()[:10]}.svg"
        sig_dir = ROOT / "data" / "souvenir_svg"
        sig_dir.mkdir(parents=True, exist_ok=True)
        (sig_dir / sig_name).write_text(svg, encoding="utf-8")
        item = {"id": f"s{int(time.time()*1000)}", "uid": uid, "nickname": nickname,
                "gift": gift, "value": value, "signature": f"/souvenir_svg/{sig_name}",
                "ts": time.time()}
        self.data["items"].append(item)
        self.data["items"] = self.data["items"][-500:]
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        self.stats["archived"] += 1
        self.bus.publish(Topics.BRAIN_REPLY, {
            "text": f"{nickname}！这份{gift}小鱼做成了永久纪念品，签名卡已经收进宝库啦！",
            "kind": "souvenir", "emotion": "happy", "viewer": nickname,
            "is_template": True, "client_id": f"sv-{int(time.time()*1000)}",
        }, source="souvenir")

    def for_viewer(self, uid: str) -> list[dict]:
        return [i for i in self.data["items"] if i["uid"] == uid]

    def status(self) -> dict:
        return {"items": len(self.data["items"]), **self.stats}


from core.config import ROOT  # noqa: E402

__all__ = ["Souvenirs", "_signature_svg"]
