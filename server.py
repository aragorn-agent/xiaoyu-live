# -*- coding: utf-8 -*-
"""小肥鱼直播间 · 主服务（aiohttp 单进程）。

一个端口承载：舞台页(/) + 中控台(/admin) + API(/api/*)。
启动流程：配置 → 总线/事件中心/流水线 → 模块注册与启动 → 配置热加载循环。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from aiohttp import web

from core import events as ev_topics
from core.bus import EventBus
from core.config import ROOT, Config
from core.gifts import GiftCatalog
from core.live_events import EventCenter
from core.registry import ModuleRegistry
from console.bridge import ConsoleBridge
from brain.filter import Filter
from brain.orchestrator import Orchestrator
from brain.life import LifeService
from brain.llm_trace import Trace
from ingress.pipeline import Pipeline
from ingress.source_cdp import CdpIngress
from ingress.source_dyhub import DyhubIngress
from ingress.source_http import HttpIngress
from ingress.source_mock import MockSource
from shows.cooldown import CooldownGuard
from shows.blackboard import Blackboard
from shows.boss import BossFight
from shows.dance import DanceTrigger
from shows.guess import GuessSong
from shows.minigame import MiniGames
from shows.souvenir import Souvenirs
from shows.kureha4_bridge import Kureha4Bridge
from shows.egg import EggTrigger
from shows.gift_tiers import GiftTiers
from shows.growth import Growth
from shows.music import MusicService
from shows.story import StoryRadio
from output.partner_ipc import PartnerIpcIngress
from voice.livetalking import HttpLiveTalkingVoice, LiveTalkingRender
from voice.module import VoiceModule

STATIC_DIR = ROOT / "web" / "static"


def build_app(config: Config) -> web.Application:
    bus = EventBus(history=512)
    gifts = GiftCatalog()
    center = EventCenter(
        gift_catalog=gifts,
        like_window=float(config.get("ingress.like_window", 2.0)),
        combo_window=float(config.get("ingress.combo_window", 3.0)),
    )
    pipeline = Pipeline(bus)
    registry = ModuleRegistry(config)

    # 模块装配（选配开关在 config.modules.*）
    trace = Trace()
    mock = MockSource(center)
    http_ingress = HttpIngress(center)
    cdp_ingress = CdpIngress(center, config)
    dyhub_ingress = DyhubIngress(center, config)
    brain = Orchestrator(bus, config, trace=trace)
    voice = VoiceModule(bus, config)
    music = MusicService(bus, config)
    gift_tiers = GiftTiers(bus, config, brain=brain)
    cooldown = CooldownGuard(bus, config, brain=brain)
    story = StoryRadio(bus, config, brain=brain)
    growth = Growth(bus, config)
    dance = DanceTrigger(bus, config)
    egg = EggTrigger(bus, config)
    k4 = Kureha4Bridge(bus, config, gifts)
    blackboard = Blackboard(bus, config)
    guess_song = GuessSong(bus, config, music, brain=brain)
    boss = BossFight(bus, config, brain=brain)
    souvenir = Souvenirs(bus, config)
    minigame = MiniGames(bus, config, brain=brain)
    life = LifeService(bus, config, brain=brain)
    partner_ipc = PartnerIpcIngress(center, config, gifts)
    lt_render = LiveTalkingRender(config)
    brain.life_store = life.store                # 生活记忆注入 system prompt
    brain.interceptors.append(music.intercept)   # 点歌拦截优先于 LLM
    _filter = Filter(wordlist=config.get("filter.wordlist", "config/wordlist.txt"),
                     on_hit=str(config.get("filter.on_hit", "deflect")).strip())
    for module in (mock, http_ingress, cdp_ingress, dyhub_ingress, brain, voice, music, gift_tiers,
                   cooldown, story, growth, dance, egg, k4, blackboard, guess_song, boss, souvenir, minigame, life, partner_ipc, lt_render):
        registry.register(module)

    console = ConsoleBridge(bus, registry, pipeline, config)
    console.attach_center(center)
    center.on_event(pipeline.on_live_event)

    app = web.Application(client_max_size=1024 * 256)
    app["config"] = config
    app["bus"] = bus
    app["center"] = center
    app["pipeline"] = pipeline
    app["registry"] = registry
    app["console"] = console
    app["gifts"] = gifts
    app["modules"] = {"ingress_mock": mock, "ingress_http": http_ingress,
                      "ingress_cdp": cdp_ingress, "ingress_dyhub": dyhub_ingress,
                      "brain": brain, "voice": voice, "filter": _filter,
                      "music": music, "gift_tiers": gift_tiers, "cooldown": cooldown,
                      "story": story, "growth": growth, "dance": dance, "egg": egg, "k4": k4, "guess_song": guess_song, "boss": boss, "souvenir": souvenir, "minigame": minigame, "blackboard": blackboard, "life": life,
                      "partner_ipc": partner_ipc, "livetalking": lt_render}

    # ---------------- 页面 ----------------
    async def index(_request):
        return web.FileResponse(STATIC_DIR / "stage.html")

    async def admin(_request):
        return web.FileResponse(STATIC_DIR / "admin.html")

    async def models_page(_request):
        return web.FileResponse(STATIC_DIR / "models.html")

    # ---------------- API ----------------
    async def api_state(_request):
        snap = console.state_snapshot()
        snap["brain"] = brain.status()
        snap["voice"] = voice.status()
        snap["filter"] = _filter.status()
        snap["trace_tail"] = trace.tail(10)
        return web.json_response(snap, dumps=lambda d: json.dumps(d, ensure_ascii=False))

    async def api_events(request):
        return await console.sse_events(request)

    async def api_ingest(request):
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        mt = http_ingress.handle_payload(payload)
        return web.json_response({"ok": True, "msg_type": mt})

    async def api_inject(request):
        """中控台/模拟器专用注入：自动补 mock msg_id（绕过指纹合并，所见即所得）。"""
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        if not payload.get("msg_id"):
            payload["msg_id"] = f"inject-{time.time_ns()}"
        mt = http_ingress.handle_payload(payload)
        return web.json_response({"ok": True, "msg_type": mt})

    async def api_boot(_request):
        pipeline.boot()
        center._seen.clear()
        blackboard.boot()
        return web.json_response({"ok": True})

    async def api_module_toggle(request):
        name = request.match_info["name"]
        body = await request.json()
        enabled = bool(body.get("enabled", True))
        result = await console.toggle_module(name, enabled, ctx=app)
        return web.json_response(result)

    async def api_config_get(_request):
        return web.json_response(config._data, dumps=lambda d: json.dumps(d, ensure_ascii=False))

    async def api_config_set(request):
        body = await request.json()
        path = str(body.get("path", ""))
        if not path:
            return web.json_response({"ok": False, "error": "path required"}, status=400)
        config.set(path, body.get("value"))
        return web.json_response({"ok": True, "path": path, "value": config.get(path)})

    async def api_music_state(_request):
        return web.json_response({
            "lib": music.lib_size() if hasattr(music, "lib_size") else len(music.lib),
            "queue": music.queue, "current": music.current,
        }, dumps=lambda d: json.dumps(d, ensure_ascii=False))

    async def api_music_ended(_request):
        song = music.next_song()
        return web.json_response({"ok": True, "next": song is not None})

    async def api_models(_request):
        base = ROOT / "characters" / "live2d"
        out = []
        if base.exists():
            for p in sorted(base.rglob("*.model3.json")):
                rel = p.relative_to(base).as_posix()
                out.append({"id": rel.rsplit("/", 1)[0], "file": rel})
        return web.json_response({"models": out, "current": config.get("render.live2d.model")})

    async def api_growth(_request):
        return web.json_response(growth.status(), dumps=lambda d: json.dumps(d, ensure_ascii=False))

    async def api_life_feed(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        title = str(body.get("title", ""))[:100] or "无标题"
        content = str(body.get("content", ""))[:6000]
        if not content:
            return web.json_response({"ok": False, "error": "content required"}, status=400)
        try:
            import asyncio as _aio
            entry = await asyncio.get_running_loop().run_in_executor(
                None, lambda: life.feed(title, content, str(body.get("url", ""))))
            return web.json_response({"ok": True, "entry": entry}, dumps=lambda d: json.dumps(d, ensure_ascii=False))
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)[:200]})

    async def api_life_compress(_request):
        import asyncio as _aio
        result = await asyncio.get_running_loop().run_in_executor(None, life.compress)
        return web.json_response(result)

    async def api_life_status(_request):
        return web.json_response(life.status())

    async def api_studio(_request):
        """dafeiyu studio 页适配：返回我们内部运行态快照。"""
        snap = console.state_snapshot()
        snap["brain"] = brain.status()
        snap["voice"] = voice.status()
        snap["life"] = life.status()
        snap["pipeline"]["by_type"] = snap["pipeline"]["by_type"]
        return web.json_response(snap, dumps=lambda d: json.dumps(d, ensure_ascii=False))

    async def api_voice_status(_request):
        return web.json_response(voice.status(), dumps=lambda d: json.dumps(d, ensure_ascii=False))

    async def api_llm_traces(request):
        try:
            q = int(request.query.get("page", "0"))
        except ValueError:
            q = 0
        rows = trace.tail(50)
        if request.query.get("source"):
            rows = [r for r in rows if r.get("source") == request.query["source"]]
        return web.json_response({"traces": rows}, dumps=lambda d: json.dumps(d, ensure_ascii=False))

    async def api_llm_trace_one(request):
        tid = request.match_info["tid"]
        rows = [t for t in trace.tail(200) if t.get("id") == tid]
        return web.json_response(rows[0] if rows else {}, dumps=lambda d: json.dumps(d, ensure_ascii=False))

    async def api_persona(request):
        card_path = ROOT / "characters" / "xiaoyu" / "card.json"
        if request.method == "POST":
            try:
                body = await request.json()
            except Exception:
                return web.json_response({"ok": False}, status=400)
            card_path.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
            try:
                brain._reload_watcher()
            except Exception:
                pass
        from brain.persona import PersonaCard
        try:
            card = PersonaCard.load()
            return web.json_response(card.to_dict(), dumps=lambda d: json.dumps(d, ensure_ascii=False))
        except Exception as exc:
            return web.json_response({"error": str(exc)})

    async def api_studio_page(_request):
        return web.FileResponse(STATIC_DIR / "studio" / "index.html")

    async def api_settings_page(_request):
        return web.FileResponse(STATIC_DIR / "settings" / "index.html")

    async def api_overlay_page(_request):
        return web.FileResponse(STATIC_DIR / "overlay" / "index.html")

    async def api_say(request):
        """中控台/验收：让小鱼说一句话（走完整语音链路）。"""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        text = str(body.get("text", ""))[:120]
        if not text:
            return web.json_response({"ok": False, "error": "text required"}, status=400)
        bus.publish(ev_topics.Topics.BRAIN_REPLY, {
            "text": text, "kind": "admin", "emotion": "happy", "viewer": "",
            "is_template": True, "client_id": f"say-{int(time.time()*1000)}",
        }, source="admin")
        return web.json_response({"ok": True})

    async def api_story_start(_request):
        ok = story.start_show()
        return web.json_response({"ok": ok, "status": story.status()})

    async def api_design_img(_request):
        # CORS 开放端点：供豆包页面 fetch 本地设计图（DataTransfer 注入上传用）
        p = ROOT / "characters" / "avatar" / "design_v2.png"
        resp = web.FileResponse(p)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    app.add_routes([
        web.get("/", index),
        web.get("/api/design_img", api_design_img),
        web.get("/admin", admin),
        web.get("/models", models_page),
        web.get("/api/state", api_state),
        web.get("/api/events", api_events),
        web.post("/api/ingest", api_ingest),
        web.post("/api/inject", api_inject),
        web.post("/api/boot", api_boot),
        web.post("/api/modules/{name}/toggle", api_module_toggle),
        web.get("/api/config", api_config_get),
        web.post("/api/config", api_config_set),
        web.get("/api/music/state", api_music_state),
        web.post("/api/music/ended", api_music_ended),
        web.get("/api/models", api_models),
        web.get("/api/growth", api_growth),
        web.get("/api/blackboard", lambda r: web.json_response(blackboard.status())),
        web.get("/api/studio", api_studio),
        web.get("/api/voice", api_voice_status),
        web.get("/api/llm/traces", api_llm_traces),
        web.get("/api/llm/traces/{tid}", api_llm_trace_one),
        web.get("/api/persona", api_persona),
        web.post("/api/persona", api_persona),
        web.get("/studio", api_studio_page),
        web.get("/settings", api_settings_page),
        web.get("/overlay", api_overlay_page),
        web.post("/api/life/feed", api_life_feed),
        web.post("/api/life/compress", api_life_compress),
        web.get("/api/life/status", api_life_status),
        web.post("/api/say", api_say),
        web.post("/api/story/start", api_story_start),
        web.static("/tts", str(ROOT / "data" / "cache" / "tts")),
        web.static("/music", str(ROOT / "characters" / "music")),
        web.static("/model", str(ROOT / "characters" / "live2d")),
        web.static("/assets", str(ROOT / "web" / "static" / "studio" / "assets")),
        web.static("/avatar", str(ROOT / "characters" / "avatar")),
        web.static("/motions", str(ROOT / "web" / "static" / "motions")),
        web.static("/static", STATIC_DIR),
    ])

    # ---------------- 生命周期 ----------------
    async def on_startup(app):
        center.attach_loop(asyncio.get_running_loop())
        bus.publish(ev_topics.Topics.SESSION_STARTED, {}, source="server")
        await registry.start_all(app)
        app["config_watch"] = asyncio.create_task(config.watch_loop(2.0), name="config_watch")

    async def on_cleanup(app):
        bus.publish(ev_topics.Topics.SESSION_STOPPED, {}, source="server")
        app["config_watch"].cancel()
        center.close()
        console.close()
        await registry.stop_all()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="小肥鱼直播间 主服务")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    config = Config()
    for d in ("data", "logs", "data/cache", "data/cache/tts", "characters"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)
    host = args.host or config.get("app.host", "127.0.0.1")
    port = args.port or int(config.get("app.port", 8188))

    print(f"[server] 小肥鱼直播间 http://{host}:{port}  控制台 /admin", flush=True)
    web.run_app(build_app(config), host=host, port=port, print=None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
