# -*- coding: utf-8 -*-
"""回复编排器：弹幕/进房/礼物事件 → 挑选与节流 → LLM → 过滤 → 发布 BRAIN_REPLY。

融合要点：
- 滑动窗口随机选人（whale-girl studio 设计，自实现）：窗口不满或超时等待，
  过期头剔除，按用户间隔过滤后 random.choice —— 比 FIFO 公平且不冷场
- 零 token 固定话术（Dsh_douyan 设计）：进房/关注走模板，LLM 只管聊天
- 诚实降级：LLM 失败/未配置 → 模板回复并带 is_template 标记
- 输入输出双向过滤 + 台词 lint（FAIL 自动截短重试一次）
"""
from __future__ import annotations

import asyncio
import random
import re
import time

from core.events import Event, Topics
from brain.filter import Filter
from brain.lint import clip, erase_hedge, lint, normalize, summarize
from brain.llm import LLMGateway, LLMNotConfigured
from brain.llm_trace import Trace
from brain.memory import MemoryStore
from brain.pacing import Pacing
from brain.persona import PersonaCard, build_system_prompt
from brain.companion import Companion

_TEMPLATE_CHAT = [
    "嗯嗯，鱼在听～",
    "这个鱼记下了！",
    "哦——？继续说。",
    "哼，算你会聊天。",
]
_TEMPLATE_WELCOME = ["来了？坐。", "欢迎来到深海～", "又见面了。"]
_TEMPLATE_FOLLOW = ["关、关注了？谢谢……才没有很开心。", "雷霆感谢！"]


class Orchestrator:
    name = "brain"
    default_enabled = True

    def __init__(self, bus, config, trace: Trace | None = None) -> None:
        self.bus = bus
        self.config = config
        self.trace = trace or Trace()
        self.card: PersonaCard | None = None
        self.memory: MemoryStore | None = None
        self.companion: Companion | None = None
        self.llm: LLMGateway | None = None
        self.filter: Filter | None = None
        self.pacing = Pacing()
        # 弹幕滑窗（窗口随机选人）
        self.window: list[Event] = []
        self.window_wait = 4.0
        self.window_started = 0.0
        self.user_last_reply: dict[str, float] = {}
        self._last_welcome: dict[str, float] = {}
        self.global_last = 0.0
        self.rate_times: list[float] = []
        # 拦截器：点歌等零 token 指令先于 LLM（返回回复文本即拦截成功）
        self.interceptors: list = []
        self._task: asyncio.Task | None = None
        self._running = False
        self._unsub = None
        self.stats = {"chats_seen": 0, "replies": 0, "templates": 0, "filtered": 0, "llm_errors": 0}

    # ---------------------------------------------------------------- 生命周期
    async def start(self, ctx) -> None:
        self._load_config()
        self._running = True
        self._unsub = self.bus.subscribe_many(
            [Topics.LIVE_CHAT, Topics.LIVE_MEMBER, Topics.LIVE_FOLLOW, Topics.LIVE_GIFT],
            self._on_live)
        self._task = asyncio.create_task(self._window_loop(), name="brain_window")
        self._warm_task = asyncio.create_task(self.llm.warm_loop(), name="llm_warm")

    async def stop(self) -> None:
        self._running = False
        if self._unsub:
            self._unsub()
        if self._warm_task:
            self._warm_task.cancel()
            try:
                await self._warm_task
            except asyncio.CancelledError:
                pass
            self._warm_task = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass    # 任务可能早已带着异常死亡（如上游 bug），热停止不能被它拖垮
            self._task = None

    def _load_config(self) -> None:
        cfg = self.config
        persona_dir = cfg.get("brain.persona_dir", "characters")
        persona_id = cfg.get("brain.persona_id", "xiaoyu")
        self.card = PersonaCard.load(base=ROOT_OR(persona_dir), persona_id=persona_id)
        self.memory = MemoryStore(
            data_file=cfg.get("brain.memory.data_file", "data/memory.json"),
            allowed_keys=cfg.get("brain.memory.allowed_keys"),
            ttl_days=int(cfg.get("brain.memory.ttl_days", 30)),
            explicit_only=bool(cfg.get("brain.memory.explicit_only", True)),
            mood_start=int(cfg.get("brain.mood.start", 60)))
        self.companion = Companion(path="data/life/companion.json")
        self.llm = LLMGateway(
            cfg.section("brain.primary"), cfg.section("brain.fallback"),
            cfg.section("brain.backup") or None,
            temperature=float(cfg.get("brain.temperature", 0.8)),
            max_tokens=int(cfg.get("brain.max_tokens", 400)),
            timeout=float(cfg.get("brain.timeout_s", 30)),
            local_routes=cfg.get("brain.local_routes") or None)
        self.filter = Filter(
            wordlist=cfg.get("filter.wordlist", "config/wordlist.txt"),
            on_hit=str(cfg.get("filter.on_hit", "deflect")).strip())
        self.window_wait = float(cfg.get("ingress.chat_window_wait", 4.0))
        self.config.watch("brain.", self._reload_watcher)

    def _reload_watcher(self) -> None:  # 热加载：重建网关，卡片懒重读
        try:
            persona_id = self.config.get("brain.persona_id", "xiaoyu")
            self.card = PersonaCard.load(base=ROOT_OR(self.config.get("brain.persona_dir", "characters")),
                                         persona_id=persona_id)
            self.llm = LLMGateway(
                self.config.section("brain.primary"), self.config.section("brain.fallback"),
                self.config.section("brain.backup") or None,
                temperature=float(self.config.get("brain.temperature", 0.8)),
                max_tokens=int(self.config.get("brain.max_tokens", 400)),
                timeout=float(self.config.get("brain.timeout_s", 30)),
                local_routes=self.config.get("brain.local_routes") or None)
        except Exception as exc:
            print(f"[brain] reload error: {exc}", flush=True)

    # ---------------------------------------------------------------- 事件入口
    def _on_live(self, ev: Event) -> None:
        if not self._running:
            return
        d = ev.data
        kind = d.get("type")
        if kind == "comment":
            self.stats["chats_seen"] += 1
            action, text = self.filter.check_input(d.get("content", ""))
            if action == "drop":
                return
            self.stats["filtered"] += action != "pass"
            if action == "deflect":
                self._publish(text, kind="chat", emotion="shy", viewer=d.get("nickname", ""))
                return
            self.memory.push_chat("user", f"{d.get('nickname','观众')}：{text}")
            if d.get("msg_id"):
                self._try_explicit_memory(d)
            self.memory.touch_viewer(d.get("user_id", ""), d.get("nickname", ""), kind="chat")
            if self.companion:
                self.companion.note_user(d.get("user_id", ""), d.get("nickname", ""), text)
                # 观众留下"未完话头"
                if re.search(r"下次|改天|回头|还没|继续|待会", text):
                    self.companion.add_thread(d.get("nickname", "观众"), text)
                if re.search(r"晚安", text):
                    self.companion.note_goodnight(d.get("user_id", ""))
            if self.memory.is_praise(text):
                old = self.memory.mood
                new = self.memory.mood_adjust(int(self.config.get("brain.mood.praise", 4)), "被夸")
                self.bus.publish(Topics.BRAIN_MOOD, {"mood": new, "delta": new - old, "reason": "praise"}, source="brain")
            self._push_window(ev)
        elif kind == "member":
            uid = d.get("user_id", "")
            self.memory.touch_viewer(uid, d.get("nickname", ""), kind="member")
            if self.pacing.gap_remaining() > 0:
                return
            now = time.time()
            if now - self._last_welcome.get(uid, 0) < 600:   # 同一观众 10 分钟只欢迎一次
                return
            self._last_welcome[uid] = now
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._welcome_llm(d.get("nickname", "观众"), uid))
            except RuntimeError:
                self._publish(random.choice(_TEMPLATE_WELCOME), kind="welcome",
                              emotion="happy", viewer=d.get("nickname", ""))
        elif kind == "follow" and d.get("follow_action", 1) == 1:
            self._publish(random.choice(_TEMPLATE_FOLLOW), kind="follow",
                          emotion="happy", viewer=d.get("nickname", ""))
        elif kind == "gift":
            # 礼物感谢由 gift_tiers 模块接手，这里只记档案和心情
            self.memory.touch_viewer(d.get("user_id", ""), d.get("nickname", ""),
                                     kind="gift", diamonds=d.get("total_value", 0))
            if self.companion:
                self.companion.note_gift(d.get("user_id", ""), d.get("nickname", ""),
                                         d.get("total_value", 0))
            old = self.memory.mood
            new = self.memory.mood_adjust(int(self.config.get("brain.mood.gift_small", 3)), "收到礼物")
            self.bus.publish(Topics.BRAIN_MOOD, {"mood": new, "delta": new - old, "reason": "gift"}, source="brain")

    def _try_explicit_memory(self, d: dict) -> None:
        rec = self.memory.capture_explicit(d.get("content", ""), viewer=d.get("nickname", ""))
        if rec and "rejected" not in rec:
            self.bus.publish(Topics.BRAIN_MEMORY, {"viewer": d.get("nickname", ""), **rec}, source="brain")

    # ---------------------------------------------------------------- 弹幕滑窗
    def _push_window(self, ev: Event) -> None:
        now = time.time()
        if not self.window:
            self.window_started = now
        self.window.append(ev)
        max_pending = int(self.config.get("ingress.chat_max_pending", 7))
        if len(self.window) >= max_pending:
            self.window_started = now - self.window_wait  # 满窗立即触发

    async def _window_loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(0.4)
                if not self.window:
                    continue
                waited = time.time() - self.window_started
                if waited < self.window_wait:
                    continue
                ev = self._pick_one()
                if ev is None:
                    continue
                try:
                    self.window.remove(ev)   # 只移除已回复的：其余弹幕留给下一拍，不丢评论
                except ValueError:
                    pass
                await self._reply_chat(ev)
        except asyncio.CancelledError:
            raise

    def _pick_one(self) -> Event | None:
        now = time.time()
        user_gap = float(self.config.get("ingress.user_gap", 8.0))
        pool = [e for e in self.window
                if now - self.user_last_reply.get(e.data.get("user_id", e.data.get("nickname", "")), 0) >= user_gap]
        if not pool:
            pool = self.window  # 全被 gap 压住时仍然选一个，避免彻底冷场
        # 被@的弹幕优先（观众点名小鱼 = 最强的互动意图）
        mentioned = [e for e in pool if "小鱼" in e.data.get("content", "")]
        if mentioned:
            pool = mentioned
        return random.choice(pool)

    # ---------------------------------------------------------------- 回复主链
    _SENT_END = "。！？!?…；;\n"

    async def _reply_chat(self, ev: Event) -> None:
        now = time.time()
        rate_limit = int(self.config.get("ingress.reply_rate_limit", 8))
        self.rate_times = [t for t in self.rate_times if now - t < 60]
        if len(self.rate_times) >= rate_limit or now - self.global_last < float(self.config.get("ingress.global_gap", 2.5)):
            return
        self.rate_times.append(now)
        self.global_last = now

        d = ev.data
        user_text = d.get("content", "")
        nickname = d.get("nickname", "观众")

        # 人设切换（零 token）
        m = re.match(r"^人设(默认|傲娇|毒舌|高冷|病娇)$", user_text)
        if m:
            ok = self.switch_persona(m.group(1))
            self._publish(f"{m.group(1)}模式……切换完毕。哼，人设随你换，本鱼还是本鱼。" if ok
                          else f"{m.group(1)}？没听过这个人设。",
                          kind="command", emotion="tsundere", viewer=nickname, is_template=True)
            return

        # 零 token 指令拦截（点歌/切歌等）——不进 LLM
        for fn in self.interceptors:
            try:
                intercepted = fn(user_text, user_id=d.get("user_id", ""), nickname=nickname)
            except TypeError:
                intercepted = fn(user_text)
            if intercepted:
                self._publish(intercepted, kind="command", emotion="happy",
                              viewer=nickname, is_template=True)
                return

        messages = self.memory.recent_chat(6)
        system = self._system_prompt(d.get("user_id", ""))

        # 流式：LLM 增量 → 切句 → 每句立刻推语音段（体感 1.5~3s 开口）
        client_id = f"chat-{int(time.time() * 1000)}-{random.randint(100, 999)}"
        buf = ""
        seq = 0
        full_parts: list[str] = []
        replied_any = False
        llm_error = ""
        t0 = time.time()
        # 硬截止：上游中转站偶发"流不收尾"，必须保证窗口循环永不卡死
        turn_deadline = float(self.config.get("brain.turn_deadline_s", 30))
        gen = self.llm.chat_stream(system, self._chat_messages(nickname, user_text))
        try:
            async with asyncio.timeout(turn_deadline):
                async for delta in gen:
                    buf += delta
                    while self._sentence_ready(buf):
                        sentence, buf = self._flush_sentence(buf)
                        if sentence:
                            replied_any = True
                            full_parts.append(sentence)
                            self._emit_segment(client_id, seq, sentence, nickname)
                            seq += 1
        except TimeoutError:
            llm_error = f"turn_deadline({turn_deadline}s)"
        except LLMNotConfigured:
            llm_error = "not_configured"
        except Exception as exc:
            llm_error = f"{type(exc).__name__}: {str(exc)[:100]}"
        finally:
            try:
                await asyncio.wait_for(gen.aclose(), 3)   # 关流释放连接，防生成器悬挂
            except Exception:
                pass
        if llm_error and not replied_any:
            self.trace.append(source="chat", ok=False, ms=int((time.time() - t0) * 1000),
                              error=llm_error, system=system,
                              messages=messages, viewer=nickname)

        if buf.strip():
            tail = buf.strip()
            if replied_any or not llm_error:
                replied_any = True
                full_parts.append(tail)
                self._emit_segment(client_id, seq, tail, nickname)
                seq += 1
            else:
                buf = ""     # 一句都没说出口 → 模板兜底

        reply = "".join(full_parts)
        is_template = not replied_any
        if is_template:
            reply = random.choice(_TEMPLATE_CHAT)
            self.stats["templates"] += 1
            self._publish(reply, kind="chat", emotion=self._emotion_hint(reply),
                          viewer=nickname, is_template=True)
        else:
            reply = self._finish_reply(reply) or reply
            self.stats["replies"] += 1
            self.pacing.note_sent(reply, quips=self.card.quips if self.card else ())
            self.memory.push_chat("assistant", reply)
            self.bus.publish(Topics.BRAIN_REPLY, {
                "text": reply, "kind": "chat", "emotion": self._emotion_hint(reply),
                "viewer": nickname, "is_template": False, "voice": False,
                "client_id": client_id, "mood": self.memory.mood,
            }, source="brain")
            self.trace.append(source="chat", ok=True, ms=int((time.time() - t0) * 1000),
                              endpoint="stream", reply=reply, viewer=nickname,
                              error=llm_error)
            self._remember_impression(None, d)

    def _sentence_ready(self, buf: str) -> bool:
        if not buf:
            return False
        if any(ch in buf for ch in self._SENT_END):
            return True
        return len(buf) >= 30       # 无标点长句强制断，保首音延迟

    def _flush_sentence(self, buf: str) -> tuple[str, str]:
        idx = max(buf.rfind(ch) for ch in self._SENT_END if buf.rfind(ch) >= 0)
        if idx < 0:
            sentence, buf = buf, ""
        else:
            sentence, buf = buf[:idx + 1].strip(), buf[idx + 1:]
        return sentence, buf

    def _emit_segment(self, client_id: str, seq: int, text: str, viewer: str) -> None:
        """单句语音段：过滤→发布→记账（语音合成由 voice 模块异步完成）。"""
        action, text = self.filter.check_output(text)
        if action == "drop":
            return
        text = normalize(text)
        if not text:
            return
        text = erase_hedge(text)          # 口癖橡皮擦
        card = self.card
        issues = lint(text, card.style, quips=card.quips,
                      banned_prefixes=card.banned_prefixes,
                      banned_labels=card.banned_labels,
                      repeat_words=card.repeat_words)
        if not summarize(issues)["ok"]:
            text = clip(text, card.style.soft_len)
        self.bus.publish(Topics.VOICE_SEGMENT, {
            "client_id": client_id, "seq": seq, "text": text,
            "emotion": self._emotion_hint(text), "viewer": viewer,
        }, source="brain")
        self.stats["replies"] += 1
        if self.companion:
            self.companion.note_reply()

    def _finish_reply(self, text: str) -> str:
        """完整回复收口：输出侧过滤 + 口癖清洗（跨句违禁兜底）；被清空则返回空串。"""
        action, text = self.filter.check_output(text)
        if action == "drop":
            return ""
        return normalize(erase_hedge(text))

    def _remember_impression(self, result, d: dict) -> None:
        """让模型顺手写观众印象（≤30字），失败静默。M2 用启发式，M6 可换成模型自写。"""
        if self.memory.regulars and d.get("user_id"):
            self.memory.note_viewer(d.get("user_id"), "")

    def _publish(self, text: str, *, kind: str, emotion: str, viewer: str,
                 is_template: bool = False) -> None:
        self.stats["replies"] += 1
        self.pacing.note_sent(text, quips=self.card.quips if self.card else ())
        self.memory.push_chat("assistant", text)
        self.bus.publish(Topics.BRAIN_REPLY, {
            "text": text, "kind": kind, "emotion": emotion, "viewer": viewer,
            "is_template": is_template, "mood": self.memory.mood,
            "client_id": f"{int(time.time()*1000)}-{random.randint(100,999)}",
        }, source="brain")

    def switch_persona(self, name: str) -> bool:
        """弹幕/中控台切换人设（默认/傲娇/毒舌/高冷/病娇）。"""
        if name not in ("默认", "傲娇", "毒舌", "高冷", "病娇"):
            return False
        import json as _json
        from brain.persona import PersonaCard as _PC
        raw = _json.load(open(ROOT_OR(f"web/static/personas/{name}.json"), encoding="utf-8"))
        self.card = _PC.from_dict(raw, source=f"web/static/personas/{name}.json")
        self.config.set("brain.persona_variant", name, persist=True)
        return True

    async def _welcome_llm(self, nickname: str, uid: str) -> None:
        """进场主动打招呼：LLM 带名字一句话欢迎；任何失败回落模板（不阻塞）。"""
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self.llm.chat,
                    self._system_prompt(uid) + "\n\n# 此刻任务\n"
                    f"一位叫「{nickname}」的观众刚刚进入直播间。跟他打个招呼：一句话、不超过20字、"
                    "必须带上他的名字，符合你的人设，傲娇但热情，结尾不要问问题。",
                    [{"role": "user", "content": f"{nickname} 进入了直播间"}],
                    90),
                timeout=25)
            text = (result.text or "").strip()
            if not text:
                raise RuntimeError("empty")
            text = self._finish_reply(text) or text
        except Exception as exc:
            print(f"[welcome] LLM 欢迎失败回落模板: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
            text = random.choice(_TEMPLATE_WELCOME)
        self._publish(text, kind="welcome", emotion="happy", viewer=nickname,
                      is_template=text in _TEMPLATE_WELCOME)

    def _system_prompt(self, uid: str = "") -> str:
        base = build_system_prompt(
            self.card, mood=self.memory.mood,
            memories=self.memory.recall(), viewers=self.memory.regulars(8))
        life = self.memory and getattr(self, "life_store", None)
        if life is not None:
            lt = life.prompt_text()
            if lt:
                base += "\n\n" + lt
        if self.companion:
            base += "\n\n" + self.companion.prompt_block(uid)
        return base

    def _chat_messages(self, nickname: str, text: str) -> list[dict]:
        msgs = [{"role": m["role"], "content": m["content"]} for m in self.memory.recent_chat(6)]
        msgs.append({"role": "user", "content": f"{nickname}说：{text}"})
        return msgs

    def _remember_impression(self, result, d: dict) -> None:
        """让模型顺手写观众印象（≤30字），失败静默。M2 用启发式，M6 可换成模型自写。"""
        if self.memory.regulars and d.get("user_id"):
            self.memory.note_viewer(d.get("user_id"), "")

    @staticmethod
    def _emotion_hint(text: str) -> str:
        if any(w in text for w in ("哼", "才不是", "笨蛋")):
            return "tsundere"
        if any(w in text for w in ("谢谢", "开心", "哈")):
            return "happy"
        return "neutral"

    def status(self) -> dict:
        s = dict(self.stats)
        if self.memory:
            s["memory"] = self.memory.status()
        s["llm_endpoints"] = [f"{e.name}:{e.model}" for e in (self.llm.configured_endpoints() if self.llm else [])]
        return s


def ROOT_OR(rel: str):
    import pathlib
    from core.config import ROOT
    p = pathlib.Path(rel)
    return p if p.is_absolute() else ROOT / p


__all__ = ["Orchestrator"]
