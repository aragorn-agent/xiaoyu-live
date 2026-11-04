# -*- coding: utf-8 -*-
"""点歌台：本地曲库 + 队列规则 + BGM 压音事件。

- 曲库：characters/music/ 下 mp3/flac/wav/m4a/ogg（文件名「歌手 - 歌名.mp3」最佳）
- 触发：观众弹幕「点歌 xxx」由 brain 编排器拦截调用 intercept()（不进 LLM，零 token）
- 队列规则（whale-girl 直播规则设计，自实现）：总上限 / 每人上限 / 复播冷却
- 播放：发布 SHOW_MUSIC 事件，舞台页 <audio> 播 BGM，说话时压音（前端 ducking）
- 网易云在线点歌默认关闭（版权），留 provider 接口
"""
from __future__ import annotations

import asyncio
import difflib
import re
import time
from pathlib import Path

from core.config import ROOT
from core.events import Topics

_ORDER_RE = re.compile(r"^\s*(?:点歌|来一首|来首|播放|play)\s*(.{1,60})$")
_SKIP_RE = re.compile(r"^\s*(?:切歌|下一首|跳过)\s*$")
AUDIO_EXTS = {".mp3", ".flac", ".wav", ".m4a", ".ogg"}


class NeteaseProvider:
    """网易云在线点歌（选配，默认关：版权风险自担）。

    设计来自 Dsh_douyin（外链思路，自实现）：搜索公开 API → 取歌 url →
    probeAudio 1KB 试拉校验（content-type + 大小防试听片段）。
    仅供个人调试；公开直播间商用请用自有曲库。
    """

    SEARCH = "https://music.163.com/api/search/get"
    SONG_URL = "https://music.163.com/api/song/enhance/player/url"

    def __init__(self, probe=lambda u: True) -> None:
        self.probe = probe

    def search(self, keyword: str, limit: int = 3) -> list[dict]:
        import urllib.parse
        import urllib.request
        data = urllib.parse.urlencode({"s": keyword, "type": 1, "limit": limit}).encode()
        req = urllib.request.Request(self.SEARCH, data=data, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                songs = (json.loads(r.read()).get("result", {}).get("songs") or [])
        except Exception:
            return []
        out = []
        for s in songs:
            out.append({"id": f"netease:{s.get('id')}", "title": s.get("name", ""),
                        "artist": "、".join(a.get("name", "") for a in s.get("artists", [])),
                        "song_id": s.get("id")})
        return out

    def song_url(self, song_id) -> str:
        import urllib.parse
        import urllib.request
        data = urllib.parse.urlencode({"ids": f"[{song_id}]", "br": 128000}).encode()
        req = urllib.request.Request(self.SONG_URL, data=data, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                items = json.loads(r.read()).get("data") or []
            return (items[0].get("url") or "") if items else ""
        except Exception:
            return ""


class MusicService:
    name = "music"
    default_enabled = True

    def __init__(self, bus, config) -> None:
        self.bus = bus
        self.config = config
        self.lib: list[dict] = []          # [{id, title, artist, path}]
        self.queue: list[dict] = []        # [{id, title, artist, path, user, at}]
        self.user_orders: dict[str, list[float]] = {}
        self.last_played: dict[str, float] = {}
        self.current: dict | None = None
        self.stats = {"orders": 0, "rejected": 0, "played": 0}

    async def start(self, ctx) -> None:
        self.scan()
        self.config.watch("music.", lambda: None)
        if bool(self.config.get("music.bgm.enabled", True)):
            self._bgm_task = asyncio.create_task(self._bgm_kick(), name="bgm_kick")

    async def stop(self) -> None:
        pass

    async def _bgm_kick(self) -> None:
        """开播 1.2s 后自动起播曲库；后续由舞台 onended → /api/music/ended 续播。"""
        await asyncio.sleep(1.2)
        if self.lib and not self.current:
            self._play_bgm()

    def _play_bgm(self) -> dict | None:
        """BGM 续播：随机挑一首非当前曲（点歌队列永远优先）。"""
        import random
        if not self.lib:
            return None
        pool = [s for s in self.lib if s["id"] != (self.current or {}).get("id")] or self.lib
        song = dict(random.choice(pool))
        self.current = song
        self.last_played[song["id"]] = time.time()
        self.stats["played"] += 1
        self._publish_queue()
        self.bus.publish(Topics.SHOW_MUSIC, {"action": "play", "song": song,
                                             "path": f"/music/{song['id']}"}, source="bgm")
        return song

    # ---------------------------------------------------------------- 曲库
    def scan(self) -> int:
        lib_dir = ROOT / str(self.config.get("music.lib_dir", "characters/music"))
        self.lib = []
        if lib_dir.exists():
            for p in sorted(lib_dir.rglob("*")):
                if p.suffix.lower() in AUDIO_EXTS:
                    name = p.stem
                    if " - " in name:
                        artist, title = name.split(" - ", 1)
                    else:
                        artist, title = "", name
                    self.lib.append({"id": str(p.relative_to(lib_dir)).replace("\\", "/"),
                                     "title": title.strip(), "artist": artist.strip(), "path": str(p)})
        return len(self.lib)

    def search(self, keyword: str, limit: int = 5) -> list[dict]:
        kw = (keyword or "").strip().lower()
        if not kw:
            return []
        scored = []
        for item in self.lib:
            hay = f"{item['artist']} {item['title']}".lower()
            ratio = difflib.SequenceMatcher(None, kw, hay).ratio()
            if kw in hay:
                ratio += 0.35
            scored.append((ratio, item))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [item for r, item in scored[:limit] if r > 0.25]

    # ---------------------------------------------------------------- 拦截入口
    def intercept(self, text: str, user_id: str = "", nickname: str = "观众") -> str | None:
        """返回已接受/拒绝的回复文本（由 brain 直接播报）；不匹配返回 None。"""
        if _SKIP_RE.match(text or ""):
            skip = self.skip()
            return skip or "队列里没有歌啦。"
        m = _ORDER_RE.match(text or "")
        if not m:
            return None
        self.stats["orders"] += 1
        return self.order(m.group(1), user_id=user_id, nickname=nickname)

    def order(self, keyword: str, user_id: str = "", nickname: str = "观众") -> str:
        max_pending = int(self.config.get("music.max_pending", 12))
        max_per_user = int(self.config.get("music.max_pending_per_user", 2))
        cooldown = int(self.config.get("music.replay_cooldown", 600))
        now = time.time()

        hits = self.search(keyword, 1)
        source = "local"
        if not hits and bool(self.config.get("music.netease.enabled", False)):
            provider = NeteaseProvider(probe=self._probe_url)
            nhits = provider.search(keyword, 1)
            for s in nhits:
                url = provider.song_url(s["song_id"])
                if url and self._probe_url(url):
                    hits = [{**s, "path": url}]
                    source = "netease"
                    break
        if not hits:
            self.stats["rejected"] += 1
            return f"曲库里没找到「{keyword}」，老板记得往 characters/music 放歌！"
        song = hits[0]

        if len(self.queue) >= max_pending:
            self.stats["rejected"] += 1
            return "点歌队伍满啦，先听正在播的这首～"
        mine = self.user_orders.get(user_id or nickname, [])
        mine = [t for t in mine if now - t < 3600]
        if len(mine) >= max_per_user:
            self.stats["rejected"] += 1
            return f"{nickname} 你排的歌还没轮到呢，贪心的小鱼要生气了！"
        if now - self.last_played.get(song["id"], 0) < cooldown:
            self.stats["rejected"] += 1
            return "这首歌刚刚才放过，换一首嘛～"

        self.queue.append({**song, "user": nickname, "at": now, "source": source})
        mine.append(now)
        self.user_orders[user_id or nickname] = mine
        self._publish_queue()
        return f"收到！{song['title']}（{source}）排上了，前面还有 {len(self.queue)-1} 首。"

    @staticmethod
    def _probe_url(url: str) -> bool:
        """1KB Range 试拉校验音源可用（防试听片段/死链，Dsh_douyin 设计自实现）。"""
        try:
            req = urllib.request.Request(url, headers={"Range": "bytes=0-1023",
                                                       "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                ctype = r.headers.get("Content-Type", "")
                total = int(r.headers.get("Content-Length", "0") or 0)
                if "audio" in ctype or "octet-stream" in ctype:
                    return total >= 300 * 1024 or total == 0  # Range响应可为分片大小
        except Exception:
            pass
        return False

    def skip(self) -> str:
        if not self.queue:
            return ""
        self.bus.publish(Topics.SHOW_MUSIC, {"action": "skip"}, source="music")
        return "切歌！下一首——"

    def next_song(self) -> dict | None:
        """舞台页播完一首后调用 /api/music/ended。队列空 → BGM 常驻续播。"""
        if not self.queue:
            if bool(self.config.get("music.bgm.enabled", True)) and self.lib:
                return self._play_bgm()
            self.current = None
            self._publish_queue()
            return None
        self.current = self.queue.pop(0)
        self.last_played[self.current["id"]] = time.time()
        self.stats["played"] += 1
        self._publish_queue()
        self.bus.publish(Topics.SHOW_MUSIC, {"action": "play", "song": self.current,
                                             "path": f"/music/{self.current['id']}"}, source="music")
        return self.current

    def _publish_queue(self) -> None:
        self.bus.publish(Topics.SHOW_MUSIC, {
            "action": "queue", "current": self.current,
            "queue": [{"title": q["title"], "artist": q["artist"], "user": q["user"]} for q in self.queue[:8]],
        }, source="music")

    def status(self) -> dict:
        return {"lib_size": len(self.lib), "queue": len(self.queue), **self.stats}


__all__ = ["MusicService"]
