# -*- coding: utf-8 -*-
"""CDP 弹幕源：驱动本机 Edge/Chrome 匿名实例打开直播间，直通转发已鉴权 webcast WS 帧。

来源：adapted from kureha4/dafeiyuzhibodouyinbanlv liveplugin/wss_auto.py + wss_source.py
(MIT License)。保留原版权声明。本项目改动：
- 合并为单文件（浏览器直通 passthrough 模式；不做手动参数直连，避免 cookie 落盘）
- 归一化结果直接喂 EventCenter.ingest（下游零感知）
- 补上游缺失的断线自动重连（supervisor 循环 + 退避）

合规提示（来自上游，务必阅读）：
- 连接的是抖音非公开私有协议（webcast ws），存在账号处罚风险；仅建议在自己的
  本机调试/直播场景使用。本模块只读浏览器已鉴权连接的收帧（只读旁观，不交互）。
"""
from __future__ import annotations

import asyncio
import base64
import glob
import gzip
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zlib

try:
    import websocket  # websocket-client
except ImportError as _e:  # pragma: no cover
    websocket = None
    _WS_IMPORT_ERROR = _e

from ingress.wss_pb import decode_message_fields, decode_push_frame, decode_response

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

_METHOD_MAP = {
    "WebcastMemberMessage": "member",
    "WebcastChatMessage": "comment",
    "WebcastGiftMessage": "gift",
    "WebcastLikeMessage": "like",
    "WebcastSocialMessage": "follow",
    "WebcastRoomUserSeqMessage": "room_stats",
}


def find_browser() -> str | None:
    for p in _BROWSER_CANDIDATES:
        import os
        if os.path.exists(p):
            return p
    return shutil.which("msedge") or shutil.which("chrome")


def normalize_room_url(s: str) -> str:
    """地址栏链接 / 分享短链 / 分享口令 / 纯数字房间号 → 浏览器可打开的房间页。"""
    s = (s or "").strip()
    if s.startswith("http"):
        return s
    m = re.search(r"https?://[A-Za-z0-9./_%?=&-]+", s)
    if m:
        return m.group(0)
    if re.fullmatch(r"\d{4,13}", s):
        return f"https://live.douyin.com/{s}"
    if re.fullmatch(r"\d{14,25}", s):
        return f"https://webcast.amemv.com/webcast/reflow/{s}"
    raise ValueError("填直播间链接或直播间号即可")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ps_sweep(like_pattern: str) -> None:
    """用 PowerShell 按命令行特征强杀 Edge/Chrome 进程（兜底清扫）。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\" | "
          f"Where-Object {{ $_.CommandLine -like '*{like_pattern}*' }} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def kill_browser_tree(pid: int | None = None, udir: str = "") -> None:
    """整树强杀本次拉起的浏览器。

    Windows 上 Popen.terminate() 往往只杀掉 Edge 的启动器进程，兼容层重新拉起的
    真实浏览器会变成孤儿存活——既弹窗又漏内存。必须 taskkill 树杀，
    再按 --user-data-dir 特征兜底清扫一次。
    """
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if udir:
        _ps_sweep(udir.replace("'", "''"))
        time.sleep(0.3)


def kill_orphan_browsers() -> None:
    """清扫历史泄漏：命令行带本项目 xiaoyu_cdp_ 临时目录的浏览器全部强杀，并删残留目录。"""
    _ps_sweep("xiaoyu_cdp_")
    for d in glob.glob(os.path.join(tempfile.gettempdir(), "xiaoyu_cdp_*")):
        shutil.rmtree(d, ignore_errors=True)


def _devtools_attach(port: int, timeout: float):
    """等调试端口就绪，返回首页 target 的 CDP websocket。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
                tabs = [t for t in json.load(r) if t.get("type") == "page"]
            if tabs:
                return websocket.create_connection(
                    tabs[0]["webSocketDebuggerUrl"], timeout=10)
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("浏览器调试端口启动失败（可能被杀软拦截）")


# ---------------------------------------------------------------------------
# 解帧管线：二进制 PushFrame → gzip → Response → 归一化 payload 列表
# ---------------------------------------------------------------------------
def decode_frames(raw: bytes, seq_counter: list = None) -> list[dict]:
    """一帧二进制 → 0~N 条官方 payload 同构 dict。"""
    pf = decode_push_frame(raw)
    payload = pf.get("payload", b"")
    if payload and (pf.get("payload_encoding") == "gzip" or payload[:2] == b"\x1f\x8b"):
        try:
            payload = gzip.decompress(payload)
        except (OSError, zlib.error):
            pass
    if not payload:
        return []
    resp = decode_response(payload)
    out = []
    ts = int(time.time() * 1000)
    for m in resp.get("messages", []):
        conv = _convert(m.get("method", ""), m.get("payload", b""), ts, seq_counter)
        if conv:
            out.append(conv)
    return out


def _convert(method: str, payload: bytes, ts: int, seq_counter: list | None) -> dict | None:
    kind = _METHOD_MAP.get(method)
    if not kind:
        return None
    f = decode_message_fields(payload)
    if seq_counter is not None:
        seq_counter[0] += 1
    seq = seq_counter[0] if seq_counter else 0
    uid = f.get("user", {}).get("sec_uid", "") or str(f.get("uid", ""))
    nick = f.get("user", {}).get("nickname", "") or "观众"
    thumb = f.get("user", {}).get("avatar_thumb")
    urls = thumb.get("url_list", []) if isinstance(thumb, dict) else []
    avatar = urls[0] if urls else ""
    base = {"msg_id": f"cdp-{seq}-{ts}", "timestamp": ts,
            "sec_open_id": uid, "nickname": nick, "avatar_url": avatar}
    if kind == "member":
        base.update(msg_type=6, msg_type_str="live_member",
                    member_count=f.get("member_count", 0))
    elif kind == "comment":
        base.update(msg_type=2, msg_type_str="live_comment", content=f.get("content", ""))
    elif kind == "gift":
        gid = f.get("gift", {}).get("name", "") if isinstance(f.get("gift"), dict) else ""
        base.update(msg_type=3, msg_type_str="live_gift", gift_name_plain=gid,
                    gift_num=f.get("total_count", 1) or 1)
    elif kind == "like":
        base.update(msg_type=1, msg_type_str="live_like", like_num=f.get("count", 1) or 1)
    elif kind == "follow":
        base.update(msg_type=5, msg_type_str="live_follow", user_follow_action=1)
    elif kind == "room_stats":
        base.update(msg_type=7, msg_type_str="live_room_stats",
                    member_count=f.get("online_user_count", 0))
    return base


# ---------------------------------------------------------------------------
# CdpWssSource：浏览器直通。接口对齐上游（stats/close/_on_open/_on_error/_on_close）。
# ---------------------------------------------------------------------------
class CdpWssSource:
    def __init__(self, on_payload, log=None, windowed: bool = False) -> None:
        self.on_payload = on_payload
        self._windowed = windowed   # True 时才允许"失败切可见窗口"兜底；默认始终无头
        self._log = log or print
        self._stop = threading.Event()
        self._finished = threading.Event()   # _run 真正结束时置位（supervisor 靠它重连）
        self._proc: subprocess.Popen | None = None
        self._ws_page = None
        self._udir = ""
        self._ws_ids: set[str] = set()
        self.room_id = ""
        self._opened = False
        self._seq = [0]
        self.stats = {"frames": 0, "errors": 0}

    def _on_open(self) -> None: ...
    def _on_error(self, err) -> None: ...
    def _on_close(self) -> None: ...

    def connect(self, room_input: str, capture_timeout: float = 45.0) -> None:
        browser = find_browser()
        if not browser:
            raise RuntimeError("找不到 Edge/Chrome（Windows 一般自带 Edge）")
        room_url = normalize_room_url(room_input)
        self._room_url = room_url
        self._capture_timeout = capture_timeout
        self._thread = threading.Thread(target=self._run, args=(browser,),
                                        daemon=True, name="cdp-wss")
        self._thread.start()

    def _run(self, browser: str) -> None:
        try:
            modes = (True, False) if self._windowed else (True,)   # 默认只无头，绝不弹窗
            for headless in modes:
                if self._stop.is_set():
                    return
                self._udir = tempfile.mkdtemp(prefix="xiaoyu_cdp_")
                port = _free_port()
                args = [browser, "--disable-gpu", "--mute-audio", "--window-position=-32000,-32000", "--window-size=1024,768",
                        "--disable-blink-features=AutomationControlled",
                        f"--user-agent={_UA}", f"--remote-debugging-port={port}",
                        f"--user-data-dir={self._udir}",
                        "--window-size=1280,800", "about:blank"]
                if headless:
                    args.insert(1, "--headless=new")
                self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL)
                try:
                    self._log(f"[cdp] {'无头' if headless else '窗口'}模式打开 {self._room_url}")
                    self._ws_page = _devtools_attach(port, self._capture_timeout)
                    self._pump(self._capture_timeout)
                    return
                except Exception as e:
                    if self._stop.is_set():
                        return
                    self._log(f"[cdp] {'无头' if headless else '窗口'}模式失败: {e}"
                              + ("，换窗口模式重试…" if headless else ""))
                    self._teardown()
            self._on_error("浏览器无法建立直播连接（没开播/验证码）")
            self._on_close()
        finally:
            self._finished.set()

    def _pump(self, capture_timeout: float) -> None:
        ws = self._ws_page
        ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
        ws.send(json.dumps({"id": 2, "method": "Page.enable"}))
        ws.send(json.dumps({"id": 3, "method": "Page.navigate",
                            "params": {"url": self._room_url}}))
        ws.settimeout(1.0)
        t0 = time.time()
        while not self._stop.is_set():
            try:
                msg = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                if not self._ws_ids and time.time() - t0 > capture_timeout:
                    raise RuntimeError(
                        f"{capture_timeout:.0f} 秒内页面没发起 WS 连接（直播间没开播或被风控）")
                continue
            except Exception as e:
                if not self._stop.is_set():
                    self._on_error(f"浏览器连接中断: {e!r}")
                break
            method = msg.get("method")
            if method == "Network.webSocketCreated":
                p = msg.get("params") or {}
                u = p.get("url", "")
                if "webcast/im/push" in u:
                    self._ws_ids.add(p.get("requestId", ""))
                    if not self.room_id:
                        q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
                        self.room_id = (q.get("room_id_str")
                                        or q.get("room_id") or [""])[0]
                    if not self._opened:
                        self._opened = True
                        self._on_open()
            elif method == "Network.webSocketFrameReceived":
                p = msg.get("params") or {}
                if p.get("requestId") not in self._ws_ids:
                    continue
                frame = p.get("response") or {}
                data = frame.get("payloadData", "")
                if frame.get("opcode") == 2:       # binary
                    try:
                        raw = base64.b64decode(data)
                    except Exception:
                        raw = data.encode("latin-1", "ignore")
                    self.stats["frames"] += 1
                    try:
                        items = decode_frames(raw, self._seq)
                    except Exception as e:
                        self.stats["errors"] += 1
                        self._log(f"[cdp] 解帧错误: {e!r}")
                        continue
                    if items:
                        try:
                            self.on_payload(items)
                        except Exception as e:
                            self._log(f"[cdp] payload handler error: {e!r}")
        self._teardown()
        if not self._stop.is_set():
            self._on_close()

    def _teardown(self) -> None:
        if self._ws_page:
            try:
                self._ws_page.close()
            except Exception:
                pass
            self._ws_page = None
        pid = self._proc.pid if self._proc else None
        self._proc = None
        kill_browser_tree(pid, self._udir)
        if self._udir:
            shutil.rmtree(self._udir, ignore_errors=True)
            self._udir = ""

    def close(self) -> None:
        self._stop.set()
        self._teardown()


# ---------------------------------------------------------------------------
# 模块封装：重连看护 + 喂 EventCenter
# ---------------------------------------------------------------------------
class CdpIngress:
    name = "ingress_cdp"
    default_enabled = False     # 真直播才开，需要 websocket-client + 本机浏览器

    def __init__(self, center, config) -> None:
        self.center = center
        self.config = config
        self._source: CdpWssSource | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self.room = ""
        self.state = "stopped"     # stopped / connecting / live / retrying / error
        self.last_event_at = 0.0
        self.stats = {"events": 0, "reconnects": 0}

    def available(self) -> bool:
        return websocket is not None and find_browser() is not None

    async def start(self, ctx) -> None:
        if not self.available():
            raise RuntimeError("缺少 websocket-client 或本机没有 Edge/Chrome")
        self._running = True
        self.room = str(self.config.get("ingress_cdp.room", ""))
        threading.Thread(target=kill_orphan_browsers, daemon=True,
                         name="cdp_orphan_sweep").start()   # 清掉上次运行残留的孤儿浏览器
        self._task = asyncio.create_task(self._supervise(), name="cdp_ingress")
        self.config.watch("ingress_cdp.room", self._on_room_change)

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
        if self._source:
            self._source.close()
            self._source = None

    def _on_room_change(self) -> None:
        new_room = str(self.config.get("ingress_cdp.room", ""))
        if new_room and new_room != self.room:
            self.room = new_room
            if self._source:
                self._source.close()   # supervisor 会用新房间重连

    def _emit(self, items: list[dict]) -> None:
        self.last_event_at = time.time()
        for item in items:
            self.center.ingest(item, source="cdp")
            self.stats["events"] += 1

    async def _supervise(self) -> None:
        """断线自动重连（上游没有的能力，这里补上）。"""
        backoff = 5.0
        try:
            while self._running:
                if not self.room:
                    self.state = "error"
                    await asyncio.sleep(5)
                    continue
                loop = asyncio.get_running_loop()
                self.state = "connecting"
                src = CdpWssSource(on_payload=lambda items: loop.call_soon_threadsafe(self._emit, items),
                                   log=lambda m: print(m, flush=True),
                                   windowed=bool(self.config.get("ingress_cdp.windowed", False)))
                src._on_open = lambda: self._set_state("live")
                src._on_close = lambda: self._set_state("retrying")
                src._on_error = lambda e: self._set_state("error", e)
                self._source = src
                try:
                    await loop.run_in_executor(None, src.connect, self.room)
                    # 等待真正结束：连接建立后 _pump 阻塞在收帧循环；
                    # 只有 close()（我们主动停）或 _finished（两模式都失败）才退出重连
                    backoff = 5.0
                    while self._running and not src._stop.is_set() and not src._finished.is_set():
                        await asyncio.sleep(2)
                except Exception as exc:
                    self.state = "error"
                    print(f"[cdp] connect failed: {exc}", flush=True)
                finally:
                    src.close()
                if not self._running:
                    break
                self.stats["reconnects"] += 1
                self.state = "retrying"
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 1.5)
        except asyncio.CancelledError:
            raise

    def _set_state(self, state: str, err: str = "") -> None:
        self.state = state
        if err:
            print(f"[cdp] {err}", flush=True)

    def status(self) -> dict:
        return {"room": self.room, "state": self.state, "available": self.available(),
                "last_event_s": round(time.time() - self.last_event_at, 1) if self.last_event_at else None,
                **self.stats}


__all__ = ["CdpIngress", "CdpWssSource", "decode_frames", "normalize_room_url"]
