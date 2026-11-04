# -*- coding: utf-8 -*-
"""官方直播伴侣 IPC 数据源（选配）：伴侣插件身份拿官方互动事件 + 全量礼物池。

来源：adapted from kureha4/dafeiyuzhibodouyinbanlv liveplugin/ipc.py (MIT License)。
保留原版权声明；本项目改动：gifts 依赖指向 core.gifts；事件直喂 EventCenter。

两种模式（config.modules.partner_ipc.mode）：
- mock  ：MockPipeClient 内存仿真，无伴侣即可全链路自测（tools/mock_sim 即走这里之外的通道）
- native：被伴侣以插件身份拉起（plugin.exe --pipeName=xxx），读真实命名管道

⚠ 进场(member)事件官方不开放——需要进场请用 ingress_cdp 或 ingress_http 源。
"""
from __future__ import annotations

import json
import struct
import sys
import threading
import time
import uuid
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 官方 IPC 枚举（与 SDK 头文件命名对齐）
# ---------------------------------------------------------------------------
class IPC_VIDEO_PIXEL_FORMAT:
    NONE = 0
    I420 = 1
    RGBA = 2
    BGRA = 3
    NV12 = 4


class IPC_EVENT_TYPE:
    EVENT_CONNECTED = 0
    EVENT_DISCONNECTED = 1
    EVENT_MESSAGE = 2


@dataclass
class VideoSpec:
    cx: int
    cy: int
    frame_rate: tuple = (1, 30)
    format: int = IPC_VIDEO_PIXEL_FORMAT.RGBA
    angle: int = 0
    bFlipH: bool = False
    bFlipV: bool = False
    cs: int = 0
    ct: int = 0
    range_: int = 0


@dataclass
class AudioSpec:
    format: int = 1
    layout: int = 3
    sample_rate: int = 48000
    count: int = 480
    planes: int = 1
    blocksize: int = 4


@dataclass
class Packet:
    channel_id: int
    data: bytes
    timestamp: int
    video: VideoSpec | None = None
    audio: AudioSpec | None = None

    @staticmethod
    def make_video(channel_id: int, rgba: bytes, cx: int, cy: int, fps: int = 30) -> "Packet":
        return Packet(channel_id=channel_id, data=rgba, timestamp=time.time_ns(),
                      video=VideoSpec(cx=cx, cy=cy, frame_rate=(1, fps)))

    @staticmethod
    def make_audio(channel_id: int, pcm: bytes, spec: AudioSpec | None = None) -> "Packet":
        return Packet(channel_id=channel_id, data=pcm, timestamp=time.time_ns(),
                      audio=spec or AudioSpec())


_FRAME_MAGIC = b"LPAV"


def pack_frame(pkt: Packet) -> bytes:
    if pkt.video is not None:
        kind = 1
        meta = struct.pack("<BiiiqiiBi??BBB", kind, pkt.channel_id,
                           pkt.video.cx, pkt.video.cy, pkt.timestamp,
                           *pkt.video.frame_rate, pkt.video.format, pkt.video.angle,
                           pkt.video.bFlipH, pkt.video.bFlipV,
                           pkt.video.cs, pkt.video.ct, pkt.video.range_)
    elif pkt.audio is not None:
        kind = 2
        meta = struct.pack("<BiiiIII", kind, pkt.channel_id, pkt.timestamp,
                           pkt.audio.format, pkt.audio.layout,
                           pkt.audio.sample_rate, len(pkt.data))
    else:
        raise ValueError("Packet must carry video or audio")
    return _FRAME_MAGIC + struct.pack("<I", len(meta)) + meta + struct.pack("<I", len(pkt.data)) + pkt.data


def unpack_frame(buf: bytes) -> Packet:
    if not buf.startswith(_FRAME_MAGIC):
        raise ValueError("bad frame magic")
    off = 4
    (meta_len,) = struct.unpack_from("<I", buf, off); off += 4
    (kind,) = struct.unpack_from("<B", buf, off)
    if kind == 1:
        (_k, ch, cx, cy, ts, nr, dn, fmt, angle, fh, fv, cs, ct, rng) = \
            struct.unpack_from("<BiiiqiiBi??BBB", buf, off)
        spec = VideoSpec(cx=cx, cy=cy, frame_rate=(nr, dn), format=fmt, angle=angle,
                         bFlipH=fh, bFlipV=fv, cs=cs, ct=ct, range_=rng)
        off += meta_len
        (dlen,) = struct.unpack_from("<I", buf, off); off += 4
        return Packet(channel_id=ch, data=buf[off:off + dlen], timestamp=ts, video=spec)
    elif kind == 2:
        (_k, ch, ts, fmt, layout, rate, dlen) = struct.unpack_from("<BiiiIII", buf, off)
        spec = AudioSpec(format=fmt, layout=layout, sample_rate=rate)
        off += meta_len
        (size,) = struct.unpack_from("<I", buf, off); off += 4
        return Packet(channel_id=ch, data=buf[off:off + size], timestamp=ts, audio=spec)
    raise ValueError(f"unknown frame kind {kind}")


# ---------------------------------------------------------------------------
# MockPipeClient：内存仿真伴侣（无伴侣自测用）
# ---------------------------------------------------------------------------
class MockPipeClient:
    def __init__(self) -> None:
        self._pending: dict = {}
        self._on_event = None
        self._on_frame = None
        self._rx: list = []
        self._lock = threading.Lock()
        self._disconnected = threading.Event()
        self.frames_sent = 0
        self.max_channels = 6
        self._next_channel = 1

    def is_connected(self) -> bool:
        return not self._disconnected.is_set()

    def connect(self, pipe_name: str = "mock", max_channels: int = 6) -> bool:
        self.max_channels = max_channels
        return True

    def shutdown(self) -> None:
        self._mark_disconnected()

    def _mark_disconnected(self) -> None:
        self._disconnected.set()

    def write_packet(self, pkt: Packet, timeout_ms: int = 1000) -> bool:
        if not self.is_connected():
            return False
        self.frames_sent += 1
        if self._on_frame:
            self._on_frame(pkt)
        return True

    def send_message(self, payload: dict) -> None:
        if payload.get("type") != "request":
            return
        req_id, method, params = payload.get("reqId"), payload.get("method"), payload.get("params", {})
        resp = {"type": "request", "reqId": req_id, "code": 1, "data": {}, "message": ""}
        if method == "createMediaSources":
            ids = []
            for _ in range(int(params.get("count", 1))):
                if self._next_channel >= self.max_channels:
                    resp = {"type": "request", "reqId": req_id, "code": 0,
                            "data": {}, "message": "超出 maxChannels 限制"}
                    break
                ids.append({"name": f"素材{self._next_channel}", "id": self._next_channel})
                self._next_channel += 1
            resp["data"] = {"sourceIds": ids}
        elif method in ("x.subscribeEvent", "x.unsubscribeEvent", "x.publishEvent"):
            resp["data"] = {}
        elif method == "getOpenGiftPools":
            from core.gifts import default_gift_pool
            resp["data"] = {"gift_pool": default_gift_pool()}
        elif method == "getOpenRoomData":
            resp["data"] = {"room_like_count": 1024}
        else:
            resp["data"] = {"sourceIds": []}
        cb = self._pending.pop(req_id, None)
        if cb:
            cb(resp)

    def request(self, method: str, params: dict | None = None, timeout: float = 5.0) -> dict:
        req_id = uuid.uuid4().hex
        evt = threading.Event()
        box: dict = {}

        def _pair(resp: dict) -> None:
            box.update(resp)
            evt.set()

        self._pending[req_id] = _pair
        self.send_message({"type": "request", "reqId": req_id,
                           "method": method, "params": params or {}})
        evt.wait(timeout)
        self._pending.pop(req_id, None)
        return box or {"code": -1, "message": "timeout", "data": {}}

    def set_event_handler(self, on_event) -> None:
        self._on_event = on_event

    def set_frame_sink(self, on_frame) -> None:
        self._on_frame = on_frame

    def inject_event(self, event_name: str, params: dict) -> None:
        if self._on_event:
            self._on_event(event_name, params)


# ---------------------------------------------------------------------------
# NativePipeClient：真实伴侣命名管道
# ---------------------------------------------------------------------------
class NativePipeClient(MockPipeClient):
    r"""伴侣启动插件时给出 --pipeName=N → \\.\pipe\N。帧协议与官方 IPC_PACKET 对齐。"""

    def __init__(self) -> None:
        super().__init__()
        self._handle = None
        self._wlock = threading.Lock()

    def connect(self, pipe_name: str = "", max_channels: int = 6) -> bool:
        import ctypes
        from ctypes import wintypes
        target = pipe_name if pipe_name.startswith("\\\\") else rf"\\.\pipe\{pipe_name}"
        GENERIC_READ, GENERIC_WRITE, OPEN_EXISTING = 0x80000000, 0x40000000, 3
        CreateFileW = ctypes.windll.kernel32.CreateFileW
        CreateFileW.restype = wintypes.HANDLE
        h = CreateFileW(target, GENERIC_READ | GENERIC_WRITE, 0, None,
                        OPEN_EXISTING, 0, None)
        INVALID = wintypes.HANDLE(-1).value
        if not h or ctypes.c_void_p(h).value == ctypes.c_void_p(INVALID).value:
            return False
        self._handle = h
        threading.Thread(target=self._read_loop, daemon=True, name="partner-rx").start()
        return True

    def shutdown(self) -> None:
        import ctypes
        if self._handle:
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None
        self._mark_disconnected()

    def write_packet(self, pkt: Packet, timeout_ms: int = 1000) -> bool:
        import ctypes
        if not self._handle or not self.is_connected():
            return False
        buf = pack_frame(pkt)
        written = ctypes.c_ulong(0)
        with self._wlock:
            ok = ctypes.windll.kernel32.WriteFile(self._handle, buf, len(buf),
                                                  ctypes.byref(written), None)
        return bool(ok) and written.value == len(buf)

    def send_message(self, payload: dict) -> None:
        import ctypes
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        written = ctypes.c_ulong(0)
        with self._wlock:
            ctypes.windll.kernel32.WriteFile(self._handle, raw, len(raw),
                                             ctypes.byref(written), None)

    def _read_loop(self) -> None:
        import ctypes
        buf = b""
        chunk = ctypes.create_string_buffer(65536)
        got = ctypes.c_ulong(0)
        while self.is_connected() and self._handle:
            ok = ctypes.windll.kernel32.ReadFile(self._handle, chunk, 65536,
                                                 ctypes.byref(got), None)
            if not ok or got.value == 0:
                self._mark_disconnected()
                return
            buf += chunk.raw[:got.value]
            while True:
                if buf.startswith(_FRAME_MAGIC):
                    if len(buf) < 8:
                        break
                    (meta_len,) = struct.unpack_from("<I", buf, 4)
                    dpos = 8 + meta_len
                    if len(buf) < dpos + 4:
                        break
                    (dlen,) = struct.unpack_from("<I", buf, dpos)
                    total = dpos + 4 + dlen
                    if len(buf) < total:
                        break
                    pkt = unpack_frame(buf[:total])
                    buf = buf[total:]
                    if self._on_frame:
                        self._on_frame(pkt)
                else:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line, buf = buf[:nl], buf[nl + 1:]
                    if line.strip():
                        self._dispatch_json(line)

    def _dispatch_json(self, line: bytes) -> None:
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if msg.get("type") == "event" and self._on_event:
            self._on_event(msg.get("eventName", ""), msg.get("params", {}))
        elif msg.get("type") == "request":
            cb = self._pending.pop(msg.get("reqId"), None)
            if cb:
                cb(msg)


# ---------------------------------------------------------------------------
# 模块封装：事件 → EventCenter；礼物池合并进 GiftCatalog
# ---------------------------------------------------------------------------
class PartnerIpcIngress:
    name = "partner_ipc"
    default_enabled = False

    def __init__(self, center, config, gifts) -> None:
        self.center = center
        self.config = config
        self.gifts = gifts
        self.client = None
        self.mode = "mock"
        self.state = "stopped"
        self.stats = {"events": 0}

    async def start(self, ctx) -> None:
        self.mode = str(self.config.get("partner_ipc.mode", "mock"))
        if self.mode == "native":
            pipe = self._pipe_name_from_argv()
            if not pipe:
                raise RuntimeError("native 模式需由直播伴侣拉起（argv 带 --pipeName）")
            self.client = NativePipeClient()
            if not self.client.connect(pipe):
                raise RuntimeError(f"连不上伴侣管道 {pipe}")
        else:
            self.client = MockPipeClient()
            self.client.connect("mock")
        self.client.set_event_handler(self._on_partner_event)
        self.client.subscribe_event("OPEN_LIVE_DATA")
        self.state = "live"
        # 官方礼物池合并（>= 8.0.0）
        try:
            pools = self.client.get_open_gift_pools()
            n = self.gifts.merge_pool((pools.get("data") or {}).get("gift_pool") or [])
            print(f"[partner_ipc] 礼物池合并 {n} 款", flush=True)
        except Exception as exc:
            print(f"[partner_ipc] 礼物池获取失败（旧版伴侣）: {exc}", flush=True)

    @staticmethod
    def _pipe_name_from_argv() -> str:
        for i, a in enumerate(sys.argv):
            if a.startswith("--pipeName="):
                return a.split("=", 1)[1]
            if a == "--pipeName" and i + 1 < len(sys.argv):
                return sys.argv[i + 1]
        return ""

    async def stop(self) -> None:
        self.state = "stopped"
        if self.client:
            self.client.shutdown()
            self.client = None

    def _on_partner_event(self, event_name: str, params: dict) -> None:
        self.stats["events"] += 1
        self.center.handle_partner_event(event_name, params, source="partner_ipc")

    def inject(self, msg_type: int, **fields) -> None:
        """中控台/测试：走官方 payload 结构注入（含连击/去重管线）。"""
        if self.client:
            payload = {"msg_type": msg_type, **fields}
            self.client.inject_event("OPEN_LIVE_DATA", {"payload": [payload]})

    def status(self) -> dict:
        return {"mode": self.mode, "state": self.state, **self.stats}


__all__ = ["PartnerIpcIngress", "MockPipeClient", "NativePipeClient", "Packet", "pack_frame", "unpack_frame"]
