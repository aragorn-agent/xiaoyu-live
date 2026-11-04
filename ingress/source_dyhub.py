# -*- coding: utf-8 -*-
"""DyHub 弹幕源（MIT 直搬）：node 子进程 + 本机 ws，替代无头 Edge CDP（省 ~300MB 内存）。

接法照抄 clseven/whale-girl-live-music 直播预览/douyin.py（MIT，设计参考）：
探测 18764 → spawn node dist/index.js → WS 订阅 chat,gift,member,like,follow,roomStatus
→ generation 计数 + 指数退避重连 → 归一化进 EventCenter。

风险与回退：lightweight 引擎签名可能随抖音改版失效 / 家庭宽带 IP 被风控 415 ——
配置 dyhub.collector="browser" 回退浏览器引擎（仍优于旧 DOM 抓法）；礼物事件需登录
Cookie（POST /api/cookie 注入一次，数天有效；过期表现为弹幕正常但礼物消失）。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import urllib.request

from core.config import ROOT
from core.live_events import MsgType

_TYPE_MAP = {"chat": 2, "gift": 3, "member": 6, "like": 1, "follow": 5, "roomStatus": 7}


class DyhubIngress:
    name = "ingress_dyhub"
    default_enabled = False

    def __init__(self, center, config) -> None:
        self.center = center
        self.config = config
        self.room = ""
        self.state = "stopped"
        self.last_event_at = 0.0
        self.stats = {"events": 0, "reconnects": 0}
        self._proc = None
        self._task = None
        self._generation = 0
        self._running = False

    def available(self) -> bool:
        dist = ROOT / "vendor" / "dyhub" / "dist" / "index.js"
        return dist.exists()

    async def start(self, ctx) -> None:
        self._running = True
        self.room = str(self.config.get("ingress_dyhub.room", "")) or \
            str(self.config.get("ingress_cdp.room", ""))
        self._task = asyncio.create_task(self._supervise(), name="dyhub_ingress")

    async def stop(self) -> None:
        self._running = False
        self.state = "stopped"
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._kill_node()

    def _kill_node(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def _spawn_node(self) -> bool:
        """复用已运行实例，否则拉起 node 子进程（whale-girl douyin.py 骨架）。"""
        port = int(self.config.get("ingress_dyhub.port", 18764))
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/rooms", timeout=1)
            return True                       # 已有实例
        except Exception:
            pass
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith(("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"))}
        env["DYHUB_PORT"] = str(port)
        env["DYHUB_HOST"] = "127.0.0.1"
        env["DYHUB_COLLECTOR"] = str(self.config.get("dyhub.collector", "lightweight"))
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        dist = ROOT / "vendor" / "dyhub" / "dist" / "index.js"
        self._proc = subprocess.Popen(["node", str(dist)], env=env, cwd=str(dist.parent.parent),
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
        for _ in range(80):                   # 最多 20s 等就绪
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/rooms", timeout=1)
                return True
            except Exception:
                time.sleep(0.25)
        return False

    def _emit(self, items: list[dict]) -> None:
        self.last_event_at = time.time()
        for item in items:
            self.center.ingest(item, source="dyhub")
            self.stats["events"] += 1

    def _convert(self, ev: dict) -> dict | None:
        """DyHub DanmakuEvent → 官方 payload 同构 dict。"""
        etype = ev.get("type")
        user = ev.get("user") or {}
        ts = int(float(ev.get("ts") or time.time()) * 1000)
        base = {"msg_id": f"dy-{ev.get('id', '')}", "timestamp": ts,
                "sec_open_id": user.get("id", "") or ev.get("roomId", ""),
                "nickname": user.get("nickname", "") or "观众",
                "avatar_url": (user.get("avatar") or "")}
        if etype == "chat":
            base.update(msg_type=int(MsgType.COMMENT), msg_type_str="live_comment",
                        content=ev.get("content") or ev.get("text") or "")
        elif etype == "gift":
            g = ev.get("gift") or {}
            base.update(msg_type=int(MsgType.GIFT), msg_type_str="live_gift",
                        gift_name_plain=g.get("giftName") or g.get("name", ""),
                        gift_num=int(g.get("repeatCount") or g.get("comboCount") or 1))
        elif etype == "member":
            base.update(msg_type=int(MsgType.MEMBER), msg_type_str="live_member")
        elif etype == "like":
            base.update(msg_type=int(MsgType.LIKE), msg_type_str="live_like",
                        like_num=int(ev.get("count") or 1))
        elif etype == "follow":
            base.update(msg_type=int(MsgType.FOLLOW), msg_type_str="live_follow",
                        user_follow_action=1)
        elif etype == "roomStatus":
            base.update(msg_type=int(MsgType.ROOM_STATS), msg_type_str="live_room_stats",
                        member_count=int((ev.get("total") or 0)))
        else:
            return None
        return base

    async def _supervise(self) -> None:
        import websockets
        backoff = 3.0
        try:
            while self._running:
                if not self.room:
                    self.state = "error"
                    await asyncio.sleep(5)
                    continue
                port = int(self.config.get("ingress_dyhub.port", 18764))
                if not await asyncio.get_running_loop().run_in_executor(None, self._spawn_node):
                    self.state = "error"
                    await asyncio.sleep(backoff)
                    backoff = min(30.0, backoff * 1.5)
                    continue
                self._generation += 1
                gen = self._generation
                url = (f"ws://127.0.0.1:{port}/ws?roomId={self.room}"
                       f"&types=chat,gift,member,like,follow,roomStatus&heartbeat=15")
                self.state = "connecting"
                try:
                    async with websockets.connect(url, max_size=None) as ws:
                        self.state = "live"
                        backoff = 3.0
                        async for raw in ws:
                            if not self._running or gen != self._generation:
                                break
                            try:
                                msg = json.loads(raw)
                            except ValueError:
                                continue
                            if str(msg.get("type", "")).startswith("__"):
                                continue      # 控制帧
                            ev = self._convert(msg.get("data") or msg)
                            if ev:
                                self._emit([ev])
                except Exception as exc:
                    self.state = "error"
                    print(f"[dyhub] 连接断开: {type(exc).__name__}: {str(exc)[:80]}", flush=True)
                if not self._running or gen != self._generation:
                    break
                self.stats["reconnects"] += 1
                self.state = "retrying"
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 1.5)
        except asyncio.CancelledError:
            raise
        finally:
            self._kill_node()

    def status(self) -> dict:
        return {"room": self.room, "state": self.state, "available": self.available(),
                "last_event_s": round(time.time() - self.last_event_at, 1) if self.last_event_at else None,
                **self.stats}


__all__ = ["DyhubIngress"]
