# -*- coding: utf-8 -*-
"""LLM trace 日志：logs/llm_trace.jsonl，排障与算账两用。

设计对标 dafeiyu-ai-live llm_trace.py（自实现精简版）：
- 密钥遮蔽（sk- / Bearer / key=）/ 分字段截断 / 写入失败绝不影响直播主链路
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{6,}|Bearer\s+\S+|[A-Za-z_]*KEY=[^\s&]+)", re.I)
_WRITE_LOCK = threading.Lock()


def _mask(s: str) -> str:
    return _SECRET_RE.sub("[已隐藏]", s or "")


def _clip(s: str, n: int) -> str:
    s = _mask(s or "")
    return s[:n] + ("…" if len(s) > n else "")


class Trace:
    def __init__(self, path: str = "logs/llm_trace.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = 0

    def append(self, *, source: str, ok: bool, ms: int, endpoint: str = "",
               model: str = "", prompt_tokens: int = 0, completion_tokens: int = 0,
               system: str = "", messages: list | None = None,
               reply: str = "", error: str = "", **extra) -> None:
        self._seq += 1
        rec = {
            "id": f"t{self._seq}", "at": round(time.time(), 3), "source": source,
            "ok": ok, "ms": ms, "endpoint": endpoint, "model": model,
            "tokens": {"prompt": prompt_tokens, "completion": completion_tokens},
            "system": _clip(system, 400),
            "messages": [
                {"role": m.get("role"), "content": _clip(str(m.get("content", "")), 600)}
                for m in (messages or [])[:12]
            ],
            "reply": _clip(reply, 1000),
            "error": _clip(error, 400),
            **{k: _clip(str(v), 100) for k, v in extra.items()},
        }
        try:
            with _WRITE_LOCK:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 日志失败绝不影响直播

    def tail(self, limit: int = 30) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
            return [json.loads(l) for l in lines if l.strip()]
        except (OSError, json.JSONDecodeError):
            return []


__all__ = ["Trace"]
