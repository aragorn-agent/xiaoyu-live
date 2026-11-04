# -*- coding: utf-8 -*-
"""陪伴状态与主动开口打分。

来源：adapted from lxllxl233/dafeiyu-ai-live src/dafeiyu/live/companion.py (MIT License)。
直播间映射：peers=高频观众，threads=没聊完的话题，dates=观众提到的日子。
candidate() 打分表原样保留——给防冷场提供"她为什么主动开口"的由头。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

_POSITIVE = ("谢谢", "喜欢", "好看", "可爱", "厉害", "牛", "爱你")
_NEGATIVE = ("垃圾", "无聊", "难听", "滚", "烦")


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


class Companion:
    def __init__(self, path: str = "data/life/companion.json") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.data = {
            "self": {"mood": 0.6, "energy": 1.0, "mood_reason": ""},
            "peers": {},        # uid -> {nickname, closeness, streak, turns_today, last_seen, goodnight_at}
            "threads": [],      # 未完话头 {id, peer, text, due_at, asked, done}
            "dates": [],        # {peer, date_text, ymd}
        }
        self.load()

    # ------------------------------------------------------------ 持久化
    def load(self) -> None:
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass

    def save(self) -> None:
        with self._lock:
            _atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=1))

    # ------------------------------------------------------------ 动力学
    def decay(self, hours: float) -> None:
        """挂机：能量回复、心情回归基线。"""
        s = self.data["self"]
        s["energy"] = min(1.0, s["energy"] + 0.06 * hours)
        base = 0.55
        s["mood"] += (base - s["mood"]) * min(1.0, 0.04 * max(hours, 0.0167))
        self.save()

    def note_user(self, uid: str, nickname: str, text: str) -> None:
        """观众说话：亲密度/心情映射（正面 0.78、负面 0.15……）。"""
        now = time.time()
        s = self.data["self"]
        p = self.data["peers"].setdefault(uid, {"nickname": nickname, "closeness": 10,
                                                "streak": 0, "turns_today": 0, "last_seen": 0})
        p["nickname"] = nickname or p["nickname"]
        p["last_seen"] = now
        if now - (p.get("last_seen_prev") or 0) > 3600 * 20:
            p["streak"] += 1            # 隔天再来 = 连续来访
        p["last_seen_prev"] = now
        p["turns_today"] += 1
        p["closeness"] = min(100, p["closeness"] + 2)
        text_low = (text or "").lower()
        if any(w in text for w in _POSITIVE):
            target = 0.78
        elif any(w in text for w in _NEGATIVE):
            target = 0.15
        else:
            target = 0.45
        s["mood"] += (target - s["mood"]) * 0.4
        s["mood_reason"] = f"{nickname} 说了句话"
        self._note_dates(uid, nickname, text)
        self.save()

    def note_gift(self, uid: str, nickname: str, diamonds: int) -> None:
        p = self.data["peers"].setdefault(uid, {"nickname": nickname, "closeness": 10,
                                                "streak": 0, "turns_today": 0, "last_seen": 0})
        p["nickname"] = nickname or p["nickname"]
        p["closeness"] = min(100, p["closeness"] + 3 + diamonds // 100)
        p["last_seen"] = time.time()
        self.save()

    def note_reply(self, n_sentences: int = 1) -> None:
        s = self.data["self"]
        s["energy"] = max(0.0, s["energy"] - 0.008 - 0.004 * max(0, n_sentences - 1))
        self.save()

    def add_thread(self, peer: str, text: str, due_in_s: int = 1800) -> str:
        """记一个"没聊完的话头"——活人感最强的主动开口由头。"""
        tid = f"t{int(time.time() * 1000)}"
        self.data["threads"].append({"id": tid, "peer": peer, "text": text[:120],
                                     "due_at": time.time() + due_in_s, "asked": False, "done": False})
        self.data["threads"] = self.data["threads"][-80:]
        self.save()
        return tid

    def note_goodnight(self, uid: str) -> None:
        p = self.data["peers"].setdefault(uid, {"nickname": "观众", "closeness": 10})
        p["goodnight_at"] = time.time()
        self.save()

    def _note_dates(self, uid: str, nickname: str, text: str) -> None:
        m = re.search(r"(\d{1,2})月(\d{1,2})日", text or "")
        if not m:
            return
        ymd = f"{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        if any(d["ymd"] == ymd and d["peer"] == uid for d in self.data["dates"]):
            return
        self.data["dates"].append({"peer": uid, "nickname": nickname, "date_text": m.group(0), "ymd": ymd})
        self.data["dates"] = self.data["dates"][-60:]

    # ------------------------------------------------------------ 主动开口打分
    def candidate(self, uid: str = "", nickname: str = "观众", idle_minutes: float = 0.0) -> dict | None:
        """给防冷场一个"由头"。返回 {"type", "score", "reason"} 或 None（别硬找话题）。"""
        now = time.time()
        s = self.data["self"]
        # 晚安屏蔽：19 点后道别 → 次日 9 点前不打扰
        p = self.data["peers"].get(uid) or {}
        g = p.get("goodnight_at")
        hour = time.localtime().tm_hour
        if g and now - g < 3600 * 14 and hour < 9:
            return None
        # 1 未完话头
        for t in self.data["threads"]:
            if not t["done"] and not t["asked"] and now >= t["due_at"]:
                t["asked"] = True
                self.save()
                return {"type": "thread", "score": 0.95, "reason": f"上次{t['peer']}说{t['text']}还没聊完"}
        # 2 重要日期
        today = time.strftime("%m-%d")
        for d in self.data["dates"]:
            if d["ymd"] == today and now - (d.get("greeted_at") or 0) > 86400 * 3:
                d["greeted_at"] = now
                self.save()
                return {"type": "date", "score": 0.82, "reason": f"今天是个特别的日子（{d['date_text']}）"}
        # 3 早间
        if 7.5 <= hour <= 10.5:
            return {"type": "morning", "score": 0.72, "reason": "早上开播打招呼"}
        # 4 深夜
        if hour >= 21 or hour < 2:
            return {"type": "night", "score": 0.6, "reason": "深夜场，聊聊失眠话题"}
        # 5 晚间
        if 18.5 <= hour < 21:
            return {"type": "evening", "score": 0.55, "reason": "晚饭后的黄金时段"}
        # 6 心情低
        if s["mood"] <= 0.35:
            return {"type": "low", "score": 0.52, "reason": "心情有点低，说说丧气话求安慰"}
        # 7 分享
        if idle_minutes >= 360 and hour >= 11:
            return {"type": "share", "score": 0.42, "reason": "分享刚刷到的东西"}
        return None                      # 没有由头，就别硬找话题

    def prompt_block(self, uid: str = "") -> str:
        """注入 prompt 的状态块：只给标签不给数值。"""
        s = self.data["self"]
        mood_w = "心情很好" if s["mood"] > 0.65 else ("心情一般" if s["mood"] > 0.35 else "心情低落")
        energy_w = "精神很好" if s["energy"] > 0.6 else "有点累了"
        rel = "刚认识不久"
        p = self.data["peers"].get(uid)
        if p:
            rel = "很亲近的人" if p.get("closeness", 0) > 60 else ("聊得来的熟人" if p.get("closeness", 0) > 25 else "还不熟，别装得很亲近")
        return f"当前状态（只用于语气，不要报数值）：{mood_w}、{energy_w}；对这位观众：{rel}。"

    def status(self) -> dict:
        s = self.data["self"]
        return {"mood": round(s["mood"], 2), "energy": round(s["energy"], 2),
                "peers": len(self.data["peers"]), "threads": len([t for t in self.data["threads"] if not t["done"]])}


__all__ = ["Companion"]
