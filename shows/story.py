# -*- coding: utf-8 -*-
"""故事电台：编译式排片（Dsh_douyin planOf 设计，自实现）+ LLM 续讲。

排片模型：开场 N 分钟 → 5 轮「讲述 6 分钟 + 互动 4 分钟」→ 收束。
- 编译：配置 → 按秒事件表（story / summary / choices / collect / decide / closer）
- 字数硬约束写进 prompt（口播分钟数 × 4.2 字/秒反推汉字数）
- 本段 summary 回喂下一段；观众建议 top3 注入；history 末 3 条作"不能推翻"约束
- LLM 不可用 → 直接念大纲原文（兜底，直播不停）
冷场时由 CooldownGuard 的 SHOW_COOLDOWN 事件接管启动（可配）。
"""
from __future__ import annotations

import asyncio
import time

from core.events import Event, Topics

_MS_PER_CHAR = 1000.0 / 4.2   # 与 voice.estimate_segments 同口径


def plan(config: dict) -> list[dict]:
    """编译排片配置 → [{at_s, op, label, ...}]（按秒排序）。"""
    opener_s = int(config.get("opener_minutes", 8)) * 60
    rounds = int(config.get("rounds", 5))
    story_s = int(config.get("story_minutes", 6)) * 60
    interact_s = int(config.get("interact_minutes", 4)) * 60
    outline = list(config.get("outline") or ["深海邮局的夜班，小鱼捡到一封没有署名的信。"])

    table: list[dict] = []
    t = 0
    table.append({"at": t, "op": "opener", "minutes": opener_s / 60})
    t += opener_s
    for i in range(rounds):
        table.append({"at": t, "op": "story", "round": i + 1, "minutes": story_s / 60,
                      "hint": outline[min(i, len(outline) - 1)]})
        t += story_s
        table.append({"at": t, "op": "interact", "round": i + 1, "minutes": interact_s / 60})
        t += interact_s
    table.append({"at": t, "op": "closer", "minutes": 2})
    return table


def words_for(minutes: float) -> int:
    """口播分钟数 → 目标汉字数（上下浮动 10%）。"""
    return max(60, int(minutes * 60 * 4.2 * 0.92))


class StoryRadio:
    name = "story"
    default_enabled = False    # 选配：开了之后冷场可自动接管

    def __init__(self, bus, config, brain=None) -> None:
        self.bus = bus
        self.config = config
        self.brain = brain
        self.table: list[dict] = []
        self.running = False
        self.phase = "idle"        # idle / opener / story / interact / closer / done
        self.round = 0
        self.summaries: list[str] = []
        self.suggestions: list[str] = []
        self._task: asyncio.Task | None = None
        self._unsub = None
        self.stats = {"segments": 0}

    @property
    def llm(self):
        return self.brain.llm if self.brain else None

    @property
    def memory(self):
        return self.brain.memory if self.brain else None

    async def start(self, ctx) -> None:
        self.table = plan(self.config.section("story"))
        self._unsub = self.bus.subscribe(Topics.SHOW_COOLDOWN, self._on_cooldown)
        self.config.watch("story.", self._reload)

    async def stop(self) -> None:
        if self._unsub:
            self._unsub()
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _reload(self) -> None:
        self.table = plan(self.config.section("story"))

    def _on_cooldown(self, ev: Event) -> None:
        if self.config.get("story.takeover_cooldown", True) and not self.running:
            self.start_show()

    # ---------------------------------------------------------------- 控制
    def start_show(self) -> bool:
        if self.running:
            return False
        self.running = True
        self.phase = "idle"
        self.round = 0
        self.summaries = []
        self._task = asyncio.get_running_loop().create_task(self._run_show(), name="story")
        return True

    def add_suggestion(self, text: str) -> None:
        """观众「期待后续 xxx」——brain 可调用。"""
        if text and len(text) <= 60 and text not in self.suggestions:
            self.suggestions.append(text)
            self.suggestions = self.suggestions[-20:]

    # ---------------------------------------------------------------- 主循环
    async def _run_show(self) -> None:
        try:
            for item in self.table:
                if not self.running:
                    return
                op = item["op"]
                minutes = item.get("minutes", 5)
                self.phase = op
                if op == "opener":
                    await self._speak(self._prompt("开场", f"用不超过{words_for(minutes)}字开场，营造悬念，最后说「接下来故事走向哪里，由你们决定」。"))
                elif op == "story":
                    self.round = item["round"]
                    hint = item.get("hint", "")
                    summary_ctx = self.summaries[-1] if self.summaries else "（这是第一段）"
                    locked = "；".join(self.summaries[-3:])
                    await self._speak(self._prompt(
                        f"第{self.round}轮讲述", 
                        f"前情总结：{summary_ctx}\n已发生（不能推翻）：{locked or '无'}\n"
                        f"观众期待：{'；'.join(self.suggestions[-3:]) or '无'}\n"
                        f"本段剧情提示：{hint}\n"
                        f"请写约{words_for(minutes)}字的连续讲述，结尾留一个选择。"))
                    await self._speak(f"本段小结：{self.summaries[-1][:60]}" if self.summaries else "（本段小结待生成）")
                elif op == "interact":
                    await self._speak(self._prompt(
                        f"第{item['round']}轮互动",
                        "念一段不超过60字的本段回顾，邀请观众发「期待后续+建议」点拨剧情，并回应弹幕氛围。"))
                elif op == "closer":
                    await self._speak(self._prompt("收束", "用不超过120字把今晚的故事收住，感谢陪伴，预告明天。"))
                await self._nap(minutes * 60)
            self.phase = "done"
            self.running = False
        except asyncio.CancelledError:
            raise

    def _prompt(self, label: str, instruction: str) -> str:
        return f"【{label}】{instruction}"

    async def _speak(self, text: str) -> None:
        """LLM 生成 → 分句发布；LLM 不可用直接念指令里的固定文案。"""
        reply = None
        if self.llm is not None:
            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None, lambda: self.llm.chat(
                        "你是在抖音直播间讲晚安故事的「小鱼」，傲娇的雷霆小肥鱼。只输出故事正文，不要解释。",
                        [{"role": "user", "content": text}]))
                reply = result.text
            except Exception as exc:
                print(f"[story] llm failed: {exc}", flush=True)
        if not reply:
            reply = text.replace("【开场】", "").replace("【收束】", "")[:120]  # 兜底：念指令
        if self.memory is not None:
            self.memory.push_chat("assistant", reply[:100])
        # 长文按 ~80 字切片发布，语音流水线逐段合成
        for i in range(0, len(reply), 80):
            self.stats["segments"] += 1
            self.bus.publish(Topics.BRAIN_REPLY, {
                "text": reply[i:i + 80], "kind": "story", "emotion": "neutral",
                "viewer": "", "is_template": False,
                "client_id": f"story-{int(time.time()*1000)}-{i}",
            }, source="story")
        if self.phase == "story" and reply:
            self.summaries.append(reply[-80:])

    async def _nap(self, seconds: float) -> None:
        await asyncio.sleep(min(seconds, 600))   # 分段睡，便于 stop 响应

    def status(self) -> dict:
        return {"running": self.running, "phase": self.phase, "round": self.round,
                "suggestions": len(self.suggestions), **self.stats}


__all__ = ["StoryRadio", "plan", "words_for"]
