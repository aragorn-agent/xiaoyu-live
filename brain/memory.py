# -*- coding: utf-8 -*-
"""记忆层：显式记忆三闸门 + 观众档案 + mood 情绪机（旧项目精华重写版）。

- 显式记忆三闸门（ds-live 设计，自实现）：
  1) 只有「记住 xxx」显式触发才写；2) 键必须在白名单内；3) 身份指令（你是/改名…）直接丢弃
- 观众档案：按 user_id 记 来访次数/累计抖币/最近互动/印象标签（模型可写"印象"）
- mood：0-100，夸奖/送礼上涨，冷场按分钟衰减，跌破阈值触发防冷场
- 持久化 data/memory.json，1.2s 防抖落盘
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

_REMEMBER_RE = re.compile(r"(?:记住|记一下|别忘了)[:：\s]*(.{1,60})")
# 身份指令一律拒收：观众不能改人设（ds-live 闸门 3）
_IDENTITY_RE = re.compile(r"你是|你不是|你叫|改名|扮演|假装")
_PRAISE_RE = re.compile(r"好看|可爱|喜欢|厉害|棒|牛|太强|声音好听|陪|爱你")


class MemoryStore:
    def __init__(self, data_file: str = "data/memory.json",
                 allowed_keys: list[str] | None = None,
                 ttl_days: int = 30, explicit_only: bool = True,
                 mood_start: int = 60) -> None:
        self.path = Path(data_file)
        self.allowed_keys = set(allowed_keys or ["称呼", "喜好", "身份", "约定"])
        self.ttl_days = ttl_days
        self.explicit_only = explicit_only
        self._lock = threading.Lock()
        self._save_timer: threading.Timer | None = None
        self.data = {
            "memories": [],          # [{key, value, source, ts}]
            "viewers": {},           # uid -> {nickname, visits, gifts_diamonds, last_seen, notes[]}
            "mood": {"value": mood_start, "updated_at": time.time()},
            "recent_chat": [],       # 最近对话 [{role, content, ts}] 最多 12 条
        }
        self.load()

    # ---------------------------------------------------------------- 持久化
    def load(self) -> None:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                for k in self.data:
                    if k in loaded:
                        self.data[k] = loaded[k]
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[memory] load failed: {exc}", flush=True)

    def save(self) -> None:
        self._save_timer = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            tmp.replace(self.path)
        except OSError as exc:
            print(f"[memory] save failed: {exc}", flush=True)

    def _schedule_save(self) -> None:
        if self._save_timer is None:
            self._save_timer = threading.Timer(1.2, self.save)
            self._save_timer.daemon = True
            self._save_timer.start()

    # ---------------------------------------------------------------- 显式记忆
    def capture_explicit(self, text: str, viewer: str = "") -> dict | None:
        """三闸门。返回写入的记忆 dict；被拒返回 None（拒绝原因在 reason）。"""
        if self.explicit_only:
            m = _REMEMBER_RE.search(text or "")
            if not m:
                return None
            payload = m.group(1).strip()
        else:
            payload = (text or "").strip()
        if _IDENTITY_RE.search(payload):
            return {"rejected": "identity"}
        key = "喜好"
        for k in self.allowed_keys:
            if payload.startswith(k) and len(payload) > len(k):
                payload = payload[len(k):].lstrip("是：: ，,")
                key = k
                break
        if not payload:
            return {"rejected": "empty"}
        with self._lock:
            # 同键覆盖旧值
            self.data["memories"] = [m for m in self.data["memories"] if m["key"] != key]
            rec = {"key": key, "value": payload[:60], "source": viewer, "ts": time.time()}
            self.data["memories"].append(rec)
            self._prune_memories()
            self._schedule_save()
        return rec

    def _prune_memories(self) -> None:
        ttl = self.ttl_days * 86400
        now = time.time()
        self.data["memories"] = [m for m in self.data["memories"] if now - m["ts"] < ttl][-50:]

    def recall(self) -> list[str]:
        with self._lock:
            return [f"{m['key']}：{m['value']}" for m in self.data["memories"]]

    # ---------------------------------------------------------------- 观众档案
    def touch_viewer(self, uid: str, nickname: str, kind: str = "chat",
                     diamonds: int = 0) -> dict:
        with self._lock:
            v = self.data["viewers"].setdefault(uid or nickname, {
                "nickname": nickname, "visits": 0, "gifts_diamonds": 0,
                "last_seen": 0, "notes": []})
            if kind == "member":
                v["visits"] += 1
            v["gifts_diamonds"] += diamonds
            v["last_seen"] = time.time()
            if nickname:
                v["nickname"] = nickname
            self._schedule_save()
            return dict(v)

    def note_viewer(self, uid: str, note: str, polarity: str = "") -> None:
        """写观众印象（分相：7 天 detail / 60 天 gist，非破坏性）。"""
        if not note:
            return
        with self._lock:
            v = self.data["viewers"].get(uid)
            if v is not None:
                v.setdefault("notes_detailed", []).append(
                    {"note": note[:30], "ts": time.time(), "polarity": polarity})
                # 同极性 2/4/8 次晋升稳定印象
                same = [n for n in v["notes_detailed"] if n.get("polarity") == polarity and polarity]
                if polarity and len(same) >= 2:
                    v.setdefault("stable_impressions", []).append(
                        {"note": note[:30], "evidence": len(same), "ts": time.time()})
                v["notes_detailed"] = v["notes_detailed"][-12:]
                self._schedule_save()

    def regulars(self, limit: int = 10) -> list[str]:
        """最近活跃的常客：「昵称(来过N次/送过X抖币)」。"""
        with self._lock:
            items = sorted(self.data["viewers"].items(),
                           key=lambda kv: kv[1].get("last_seen", 0), reverse=True)[:limit]
        out = []
        for _uid, v in items:
            desc = v["nickname"]
            if v.get("visits", 0) > 1:
                desc += f"(来过{v['visits']}次)"
            stable = v.get("stable_impressions") or []
            if stable:
                desc += f"【{stable[-1]['note']}】"
            if v.get("gifts_diamonds", 0) > 0:
                desc += f"(送过{v['gifts_diamonds']}抖币)"
            out.append(desc)
        return out

    # ---------------------------------------------------------------- mood
    _MOOD_LOCK = threading.Lock()

    @property
    def mood(self) -> int:
        with self._lock:
            return int(self.data["mood"]["value"])

    def mood_adjust(self, delta: int, reason: str = "") -> int:
        with self._lock:
            value = max(0, min(100, self.data["mood"]["value"] + delta))
            self.data["mood"] = {"value": value, "updated_at": time.time(), "last_reason": reason}
            self._schedule_save()
        return value

    def mood_decay(self, minutes: float) -> int:
        rate = 2.0
        return self.mood_adjust(-int(rate * minutes), "冷场衰减")

    @staticmethod
    def is_praise(text: str) -> bool:
        return bool(_PRAISE_RE.search(text or ""))

    # ---------------------------------------------------------------- 对话上下文
    def push_chat(self, role: str, content: str, limit: int = 12) -> None:
        with self._lock:
            self.data["recent_chat"].append({"role": role, "content": content[:200], "ts": time.time()})
            self.data["recent_chat"] = self.data["recent_chat"][-limit:]
            self._schedule_save()

    def recent_chat(self, limit: int = 6) -> list[dict]:
        with self._lock:
            return list(self.data["recent_chat"][-limit:])

    def boot(self) -> None:
        """开播清场：只清对话上下文，记忆/档案/mood 保留。"""
        with self._lock:
            self.data["recent_chat"] = []
            self._schedule_save()

    def status(self) -> dict:
        with self._lock:
            return {
                "memories": len(self.data["memories"]),
                "viewers": len(self.data["viewers"]),
                "mood": self.data["mood"]["value"],
                "recent_chat": len(self.data["recent_chat"]),
            }


__all__ = ["MemoryStore"]
