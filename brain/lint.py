# -*- coding: utf-8 -*-
"""台词 lint：每条要念出口的回复都过一遍。

来源：adapted from lxllxl233/dafeiyu-ai-live src/dafeiyu/persona/lint.py (MIT License)。
按「小鱼」口径裁剪：短句直播，去掉游戏解说专属规则（血条/伤害数字），保留
FAIL（空台词/超长/自称前缀/标签化/复读/双招牌）与 WARN（语气词/破折号/标点堆叠/叹气）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from brain.persona import Style

_SPEAKER_RE = re.compile(r"(?:小鱼|AI|DeepSeek|模型|主播)\s*[:：]\s*", re.I)
_SPACE_RE = re.compile(r"[ \t\u3000]+")
_SENT_RE = re.compile(r"[。！？!?；;…]+")
_FILLER_RE = re.compile(r"[啊吧嘛哈诶]")
_STACK_RE = re.compile(r"[？?]{2,}|[。]{2,}")
_SIGH_RE = re.compile(r"唉|哎|呜|唔")


@dataclass(frozen=True, slots=True)
class LintIssue:
    level: str  # FAIL | WARN
    message: str
    rule: str = ""

    def to_dict(self) -> dict:
        return {"level": self.level, "message": self.message, "rule": self.rule}


def strip_speaker(text: str) -> str:
    """去掉「小鱼：」「AI:」这类自称前缀。"""
    return _SPEAKER_RE.sub("", text or "").strip()


def normalize(text: str) -> str:
    """只做格式归一，不改内容：去自称前缀、去多余空格、去首尾引号。"""
    t = strip_speaker(text)
    t = _SPACE_RE.sub(" ", t).strip()
    return t.strip("“”\"'「」")


def clip(text: str, limit: int) -> str:
    """超长按字符截断，尽量落在标点后；补省略号。"""
    t = (text or "").strip()
    if limit <= 0 or len(t) <= limit:
        return t
    head = t[: max(1, limit - 1)]
    cut = max(head.rfind(ch) for ch in "，。！？；、,.;!?")
    if cut >= limit // 2:
        head = head[: cut + 1]
    return head.rstrip("，,、；;：: ") + "…"


def split_sentences(text: str, max_chars: int = 78) -> list[str]:
    """按句末标点贪心切段，每段 ≤ max_chars（语音流水线用，ds-live/Dsh_douyin 口径）。"""
    t = normalize(text)
    if not t:
        return []
    raw = [p for p in _SENT_RE.split(t)]
    out: list[str] = []
    buf = ""
    for part in raw:
        seg = part if part and part[-1] in "。！？!?…" else (part + "。" if part else "")
        if not seg:
            continue
        if len(buf) + len(seg) <= max_chars:
            buf += seg
        else:
            if buf:
                out.append(buf)
            while len(seg) > max_chars:      # 单句超长硬切
                out.append(seg[:max_chars])
                seg = seg[max_chars:]
            buf = seg
    if buf:
        out.append(buf)
    return out


def lint(text: str, style: Style | None = None,
         quips: tuple[str, ...] = (),
         banned_prefixes: tuple[str, ...] = (),
         banned_labels: tuple[str, ...] = (),
         repeat_words: tuple[str, ...] = ()) -> list[LintIssue]:
    style = style or Style()
    issues: list[LintIssue] = []
    t = (text or "").strip()
    if not t:
        return [LintIssue("FAIL", "空台词", "empty")]

    n = len(t)
    if n > style.soft_len:
        issues.append(LintIssue("FAIL", f"{n} 字 > {style.soft_len}：砍到一个信息点", "length"))
    elif n > style.max_len:
        issues.append(LintIssue("WARN", f"{n} 字 > {style.max_len}：建议再砍", "length"))

    for prefix in banned_prefixes:
        if t.startswith(prefix) or prefix in t[:6]:
            issues.append(LintIssue("FAIL", f"不要自称/前缀「{prefix}」", "prefix"))

    for label in banned_labels:
        if label in t:
            issues.append(LintIssue("FAIL", f"不要标签化「{label}」", "label"))

    if "——" in t:
        issues.append(LintIssue("WARN", "不要用破折号硬拼两句", "dash"))

    for word in repeat_words:
        if t.count(word) >= 2:
            issues.append(LintIssue("FAIL", f"「{word}」一条里出现 ≥2 次 = 复读", "repeat"))

    if _STACK_RE.search(t):
        issues.append(LintIssue("WARN", "标点堆叠（？？/。。。）", "punct"))

    fillers = len(_FILLER_RE.findall(t))
    if fillers > style.max_fillers:
        issues.append(LintIssue("WARN", f"语气词 {fillers} 个，删几个", "filler"))

    parts = [p for p in _SENT_RE.split(t) if p.strip()]
    if len(parts) > 3:
        issues.append(LintIssue("WARN", f"{len(parts)} 个短句：一句话最多三拍", "sentences"))

    hits = [q for q in quips if q and q in t]
    if len(hits) >= 2:
        issues.append(LintIssue("FAIL", f"一句里 {len(hits)} 句招牌：最多一句", "quip"))

    sighs = len(_SIGH_RE.findall(t))
    if sighs > 1:
        issues.append(LintIssue("WARN", f"叹气 {sighs} 次：一屏一个就够", "sigh"))

    return issues


def summarize(issues: list[LintIssue]) -> dict:
    fails = [i for i in issues if i.level == "FAIL"]
    return {
        "ok": not fails,
        "fails": len(fails),
        "warns": len(issues) - len(fails),
        "issues": [i.to_dict() for i in issues],
    }


__all__ = ["LintIssue", "lint", "split_sentences", "clip", "normalize", "strip_speaker", "summarize"]


# ── 口癖橡皮擦（astrbot hedge_eraser 设计）：出站整形 ──
_ERASER_RULES = [
    (re.compile(r"(哼，?){2,}"), "哼，"),          # "哼"连发合并
    (re.compile(r"^(哼|才不是|笨蛋)[，。]"), ""),  # 句首口癖冷却由 pacing 管，这里去叠
    (re.compile(r"[？?]{2,}"), "？"),              # 标点堆叠
    (re.compile(r"(哈){3,}"), "哈哈"),
]


def erase_hedge(text: str, quip_cooldown_map: dict | None = None) -> str:
    """口癖擦除：连续口癖合并、标点规整。"""
    t = text or ""
    for rx, repl in _ERASER_RULES:
        t = rx.sub(repl, t)
    return t.strip()


def ends_with_question(text: str) -> bool:
    return bool(text) and text[-1] in "？?"
