# -*- coding: utf-8 -*-
"""人设层：人设卡加载 + 系统提示词拼装。

来源：adapted from lxllxl233/dafeiyu-ai-live src/dafeiyu/persona/card.py (MIT License)。
本项目简化为两层：characters/<id>/card.json（身份+台词规则） + style.md（语气样本）。

目录结构：
  characters/xiaoyu/card.json   结构化人设（可被中控台编辑）
  characters/xiaoyu/style.md    纯文本语气样例，直接拼进 system prompt
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from core.config import ROOT


@dataclass(frozen=True, slots=True)
class Style:
    """台词规则。数值与 brain/lint.py 的口径保持一致。"""

    max_len: int = 60          # 硬上限（超过 FAIL）
    soft_len: int = 42         # 软上限（超过 WARN）——一句话直播节奏
    max_fillers: int = 3
    min_gap: float = 1.2
    quip_cooldown: float = 600.0
    max_quips_per_10min: int = 2

    @classmethod
    def from_dict(cls, raw: dict | None) -> "Style":
        raw = raw or {}
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass(frozen=True, slots=True)
class PersonaCard:
    id: str = "xiaoyu"
    name: str = "小鱼"
    title: str = "傲娇的雷霆小肥鱼"
    tagline: str = ""
    voice: str = "edge"
    traits: tuple[str, ...] = ()
    banned_prefixes: tuple[str, ...] = ()
    banned_labels: tuple[str, ...] = ()
    repeat_words: tuple[str, ...] = ()
    quips: tuple[str, ...] = ()
    style: Style = field(default_factory=Style)
    source: str = ""

    @classmethod
    def from_dict(cls, raw: dict, source: str = "") -> "PersonaCard":
        raw = raw or {}
        return cls(
            id=str(raw.get("id") or "xiaoyu"),
            name=str(raw.get("name") or "小鱼"),
            title=str(raw.get("title") or ""),
            tagline=str(raw.get("tagline") or ""),
            voice=str(raw.get("voice") or "edge"),
            traits=tuple(str(x) for x in raw.get("traits") or ()),
            banned_prefixes=tuple(str(x) for x in raw.get("banned_prefixes") or ()),
            banned_labels=tuple(str(x) for x in raw.get("banned_labels") or ()),
            repeat_words=tuple(str(x) for x in raw.get("repeat_words") or ()),
            quips=tuple(str(x) for x in raw.get("quips") or ()),
            style=Style.from_dict(raw.get("style")),
            source=source,
        )

    @classmethod
    def load(cls, base: Path | None = None, persona_id: str = "xiaoyu") -> "PersonaCard":
        base = base or ROOT / "characters"
        path = base / persona_id / "card.json"
        if not path.exists():
            raise FileNotFoundError(f"人设卡不存在：{path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.setdefault("id", persona_id)
        return cls.from_dict(raw, source=str(path))

    def load_style_md(self) -> str:
        path = ROOT / "characters" / self.id / "style.md"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        return ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "title": self.title,
            "tagline": self.tagline, "voice": self.voice,
            "traits": list(self.traits), "quips": list(self.quips),
            "style": {k: getattr(self.style, k) for k in self.style.__dataclass_fields__},
            "source": self.source,
        }


def build_system_prompt(card: PersonaCard, *, mood: int = 60,
                        memories: list[str] | None = None,
                        viewers: list[str] | None = None,
                        scene: str = "") -> str:
    """拼 system prompt：人设卡 + 语气样例 + 当前状态（情绪/记忆/在场观众）。"""
    parts = [
        f"你是「{card.name}」，{card.title}。" + (card.tagline or ""),
    ]
    if card.traits:
        parts.append("性格要点：" + "；".join(card.traits))
    style_md = card.load_style_md()
    if style_md:
        parts.append(style_md)
    hard_rules = (
        f"硬规则：每次回复最多 {card.style.max_len} 字、一句话、不换行；"
        "绝不自称 AI/模型/助手，被问就说自己是小鱼；"
        "不承诺收益、不引导私下交易、不接政治/色情/赌博/医疗/投资类话题，岔开就好别解释规则；"
        "情绪要连贯，别上一句炸毛下一句撒娇。"
    )
    parts.append(hard_rules)
    if memories:
        parts.append("你记得的事（可能过时，别当新闻讲）：\n- " + "\n- ".join(memories[:8]))
    if viewers:
        parts.append("最近在直播间的人：" + "、".join(viewers[:10]))
    parts.append(f"当前心情值 {mood}/100（越高越兴奋，低了会蔫）。" + (f"场景：{scene}" if scene else ""))
    return "\n\n".join(parts)


__all__ = ["Style", "PersonaCard", "build_system_prompt"]
