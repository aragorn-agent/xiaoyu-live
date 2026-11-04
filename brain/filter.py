# -*- coding: utf-8 -*-
"""双向敏感词过滤：输入（弹幕）与输出（AI 回复）都过一遍，独立于 LLM 提示词。

设计对标 digital-human-livestream 的双向过滤思想（自实现）：
- 词库 config/wordlist.txt：每行一个精确词；正则行用 `正则::pattern` 前缀
- on_hit 模式：deflect=命中→岔开话题（不解释规则）| drop=丢弃 | mask=脱敏放行
- 热加载：词库 mtime 变化自动重载（挂在 config watch 循环里）
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from core.config import ROOT

_DEFLECT_REPLIES = [
    "这个话题鱼不太懂，说点开心的吧！",
    "哎呀换个话题嘛，鱼带你看气泡！",
    "嘘——聊点别的，今天水温刚刚好。",
]


class Filter:
    def __init__(self, wordlist: str = "config/wordlist.txt", on_hit: str = "deflect") -> None:
        self.wordlist_path = ROOT / wordlist
        self.on_hit = on_hit
        self._exact: set[str] = set()
        self._regexes: list[re.Pattern] = []
        self._mtime = 0.0
        self._lock = threading.Lock()
        self.stats = {"in_hits": 0, "out_hits": 0, "deflected": 0, "dropped": 0, "masked": 0}
        self.reload()

    def reload(self) -> bool:
        path = self.wordlist_path
        if not path.exists():
            return False
        mtime = path.stat().st_mtime
        if mtime == self._mtime:
            return False
        exact: set[str] = set()
        regexes: list[re.Pattern] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("正则::"):
                try:
                    regexes.append(re.compile(line[len("正则::"):].strip()))
                except re.error:
                    continue
            else:
                exact.add(line.lower())
        with self._lock:
            self._exact, self._regexes, self._mtime = exact, regexes, mtime
        return True

    # ---------------------------------------------------------------- 检查
    def hit(self, text: str) -> str | None:
        """返回命中的词/模式描述，未命中返回 None。"""
        low = (text or "").lower()
        with self._lock:
            for w in self._exact:
                if w in low:
                    return w
            for rx in self._regexes:
                m = rx.search(text or "")
                if m:
                    return f"regex:{m.group(0)[:8]}"
        return None

    def check_input(self, text: str) -> tuple[str, str]:
        """处理观众输入。返回 (action, text)。
        action: pass | drop | deflect（deflect 时 text 为岔开话术）| mask。"""
        h = self.hit(text)
        if h is None:
            self.reload()   # 热加载顺带做
            return "pass", text
        self.stats["in_hits"] += 1
        mode = self.on_hit
        if mode == "drop":
            self.stats["dropped"] += 1
            return "drop", ""
        if mode == "mask":
            self.stats["masked"] += 1
            return "pass", self.mask(text)
        self.stats["deflected"] += 1
        return "deflect", _DEFLECT_REPLIES[int(time.time()) % len(_DEFLECT_REPLIES)]

    def check_output(self, text: str) -> tuple[str, str]:
        """处理 AI 输出。返回 (action, text)。输出侧不做 deflect（没意义），drop/mask。"""
        h = self.hit(text)
        if h is None:
            return "pass", text
        self.stats["out_hits"] += 1
        mode = self.on_hit
        if mode == "mask":
            self.stats["masked"] += 1
            return "pass", self.mask(text)
        self.stats["dropped"] += 1
        return "drop", ""

    def mask(self, text: str) -> str:
        out = text or ""
        with self._lock:
            for w in self._exact:
                if w in out.lower():
                    out = re.sub(re.escape(w), "*" * max(1, len(w)), out, flags=re.I)
            for rx in self._regexes:
                out = rx.sub("*", out)
        return out

    def status(self) -> dict:
        with self._lock:
            return {"exact": len(self._exact), "regex": len(self._regexes), **self.stats}


__all__ = ["Filter"]
