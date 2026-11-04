# -*- coding: utf-8 -*-
"""恰饭小黑板 + AI 续航：竞品"节目可见层"的数据源。

- 吃碗计数：点赞 200 = 1 碗，礼物 10 抖币 = 1 碗（对标竞品 1 抖币=1碗白饭，可配）
- 下播进度：本场吃碗数 / 目标碗数（目标写 config）
- AI 续航：llm_trace 累计 token × 单价 → 已花费/预算 → 可维持时长
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from core.config import ROOT


def _atomic_write(path: Path, data: str) -> None:
    tmp = path.with_suffix(".tmp")
    for attempt in range(8):
        try:
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.2 * (attempt + 1))
    raise OSError(f"cannot write {path}")


class Blackboard:
    name = "blackboard"
    default_enabled = True

    def __init__(self, bus, config) -> None:
        self.bus = bus
        self.config = config
        self.path = ROOT / "data" / "blackboard.json"
        self._lock = threading.Lock()
        self.data = {"likes": 0, "gift_diamonds": 0, "bowls": 0.0, "session_start": time.time()}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        self._unsub = None
        self.stats = {"events": 0}

    async def start(self, ctx) -> None:
        self._unsub = self.bus.subscribe_many(
            ["live.like", "live.gift"], self._on)

    async def stop(self) -> None:
        if self._unsub:
            self._unsub()
        self.save()

    def _on(self, ev) -> None:
        d = ev.data or {}
        kind = d.get("type")
        with self._lock:
            if kind == "like":
                self.data["likes"] += int(d.get("like_num", 1))
            elif kind == "gift":
                self.data["gift_diamonds"] += int(d.get("total_value", 0))
            per_like = float(self.config.get("blackboard.bowls_per_like", 200))
            per_diamond = float(self.config.get("blackboard.bowls_per_diamond", 10))
            bowls = self.data["likes"] / per_like + self.data["gift_diamonds"] / per_diamond
            self.data["bowls"] = round(bowls, 2)
            self.stats["events"] += 1
        self.save()

    def save(self) -> None:
        with self._lock:
            snapshot = json.dumps(self.data, ensure_ascii=False, indent=1)
        _atomic_write(self.path, snapshot)

    def boot(self) -> None:
        """开播清场：本场计数归零（累计喂饭历史保留在 lifetime）。"""
        with self._lock:
            prev = self.data.get("bowls", 0)
            self.data["likes"] = 0
            self.data["gift_diamonds"] = 0
            self.data["bowls"] = 0.0
            self.data.setdefault("lifetime_bowls", 0)
            self.data["lifetime_bowls"] = round(self.data["lifetime_bowls"] + prev, 2)
            self.data["session_start"] = time.time()
        self.save()

    def status(self) -> dict:
        with self._lock:
            bowls = self.data["bowls"]
        target = float(self.config.get("blackboard.bowls_target", 2000))
        budget = float(self.config.get("blackboard.ai_budget_yuan", 10))
        # LLM 花费：读 trace 尾页累计（粗估；完整账单见 billing 页）
        spent = 0.0
        tokens = 0
        try:
            from brain.llm_trace import Trace
            rows = Trace("logs/llm_trace.jsonl").tail(200)
            for r in rows:
                tokens += (r.get("tokens") or {}).get("prompt", 0) + (r.get("tokens") or {}).get("completion", 0)
            spent = tokens / 1e6 * 2.0    # 按 2 元/百万 粗估
        except Exception:
            pass
        remain = max(0.0, budget - spent)
        return {"bowls": bowls, "target": target, "progress": min(100, bowls / target * 100 if target else 0),
                "likes": self.data["likes"], "diamonds": self.data["gift_diamonds"],
                "ai_spent_yuan": round(spent, 3), "ai_budget_yuan": budget,
                "ai_tokens": tokens}


__all__ = ["Blackboard"]
