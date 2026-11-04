# -*- coding: utf-8 -*-
"""LLM 网关：OpenAI 兼容接口 + 三层级联 + 指数退避。

级联设计来自 Dsh_douyin（primary → fallback → backup，重试上抛由网关统一做），
实现为本项目自研。纪律：
- 凭证缺失抛 LLMNotConfigured（不静默降级，调用方走模板兜底——诚实降级）
- 可重试状态码集合 + Retry-After 解析 + 2^n 退避加抖动
- 每次调用产出 trace 记录（brain.llm_trace）
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMNotConfigured(Exception):
    pass


@dataclass
class Endpoint:
    name: str
    base_url: str
    key: str
    model: str
    local: bool = False

    @property
    def configured(self) -> bool:
        # 本地路由（fast_reply 双代理）无需 key
        return bool(self.base_url and self.model) and (bool(self.key) or self.local)


@dataclass
class LLMResult:
    text: str
    endpoint: str
    model: str
    ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    fallback: bool = False


class LLMGateway:
    def __init__(self, primary_cfg: dict, fallback_cfg: dict | None = None,
                 backup_cfg: dict | None = None, temperature: float = 0.8,
                 max_tokens: int = 400, timeout: float = 30.0,
                 local_routes: list | None = None) -> None:
        self.endpoints: list[Endpoint] = []
        # 本地快车道在前（fast_reply 双代理：零 key、1.6~3s，但超时短）
        for i, cfg in enumerate(local_routes or []):
            if not cfg:
                continue
            self.endpoints.append(Endpoint(
                name=f"local{i+1}",
                base_url=str(cfg.get("url", "")).rstrip("/"),
                key=_env_of(cfg.get("key_env")) or "",
                model=cfg.get("model", ""), local=True))
        for i, cfg in enumerate([primary_cfg, fallback_cfg, backup_cfg]):
            if not cfg:
                continue
            base = cfg.get("base_url") or _env_of(cfg.get("base_url_env"))
            key = _env_of(cfg.get("key_env"))
            model = cfg.get("model", "")
            self.endpoints.append(Endpoint(
                name=["primary", "fallback", "backup"][i],
                base_url=(base or "").rstrip("/"), key=key or "", model=model))
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.local_timeout = 6.0     # 本地代理挂了要快速落兜底
        self.stats = {"first_ms": []}

    def configured_endpoints(self) -> list[Endpoint]:
        return [e for e in self.endpoints if e.configured]

    async def chat_stream(self, system: str, messages: list[dict],
                          max_tokens: int | None = None):
        """异步流式：按端点顺序尝试，yield 文本增量。

        规则：一旦某个端点已经吐出内容，中途失败不再换端点（避免重复文本），
        直接抛错给调用方走模板兜底；未吐内容前的失败静默换下一个端点。
        """
        import aiohttp
        eps = self.configured_endpoints()
        if not eps:
            raise LLMNotConfigured("没有可用 LLM 端点（检查环境变量）")
        errors: list[str] = []
        yielded = False
        for ep in eps:
            try:
                async for delta in self._stream_endpoint(ep, system, messages,
                                                         max_tokens or self.max_tokens):
                    yielded = True
                    yield delta
                if not yielded:
                    errors.append(f"{ep.name}: 空流")   # 中转站偶发 200 但零内容 → 换下一端点
                    continue
                return
            except LLMNotConfigured:
                raise
            except Exception as exc:
                if yielded:
                    raise RuntimeError(f"{ep.name}: 流中断: {exc}") from exc
                errors.append(f"{ep.name}: {type(exc).__name__}: {str(exc)[:100]}")
        raise RuntimeError("所有 LLM 端点失败：" + "；".join(errors))

    async def _stream_endpoint(self, ep: Endpoint, system: str, messages: list[dict],
                               max_tokens: int):
        """SSE 流式请求（aiohttp）。返回文本增量生成器。"""
        import aiohttp
        payload = {
            "model": ep.model,
            "messages": [{"role": "system", "content": system}] + messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        headers = {"Content-Type": "application/json"}
        if ep.key:
            headers["Authorization"] = f"Bearer {ep.key}"
        timeout = aiohttp.ClientTimeout(total=(self.local_timeout if ep.local else self.timeout),
                                        connect=3)
        t0 = time.time()
        first_logged = False
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(f"{ep.base_url}/chat/completions",
                                 json=payload, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    if resp.status in RETRY_STATUS:
                        raise RetryableError(f"HTTP {resp.status}")
                    raise RuntimeError(f"HTTP {resp.status}: {body[:150]}")
                async for raw in resp.content:
                    line = raw.strip()
                    if not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    delta = (obj.get("choices") or [{}])[0].get("delta", {}).get("content", "")
                    if delta:
                        if not first_logged:
                            self.stats["first_ms"].append(int((time.time() - t0) * 1000))
                            first_logged = True
                        yield delta

    async def warm_loop(self, interval: float = 210.0):
        """本地代理保活：定期 ping 防冷启动首包恶化（fast_reply 同款手法）。"""
        import asyncio
        while True:
            await asyncio.sleep(interval)
            for ep in self.endpoints:
                if not ep.local or not ep.configured:
                    continue
                try:
                    import aiohttp
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as sess:
                        await sess.post(f"{ep.base_url}/chat/completions", json={
                            "model": ep.model,
                            "messages": [{"role": "user", "content": "1"}],
                            "max_tokens": 4,
                        }, headers={"Content-Type": "application/json"})
                except Exception:
                    pass

    def chat(self, system: str, messages: list[dict], max_tokens: int | None = None) -> LLMResult:
        """按序尝试各端点；端点内对可重试错误做指数退避（1.2s 起，上限 12s，尊重 Retry-After）。"""
        eps = self.configured_endpoints()
        if not eps:
            raise LLMNotConfigured("没有可用 LLM 端点（检查环境变量）")
        errors: list[str] = []
        for idx, ep in enumerate(eps):
            try:
                return self._chat_one(ep, system, messages, max_tokens or self.max_tokens)
            except LLMNotConfigured:
                raise
            except Exception as exc:
                errors.append(f"{ep.name}: {type(exc).__name__}: {str(exc)[:120]}")
        raise RuntimeError("所有 LLM 端点失败：" + "；".join(errors))

    def _chat_one(self, ep: Endpoint, system: str, messages: list[dict],
                  max_tokens: int) -> LLMResult:
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                return self._request(ep, system, messages, max_tokens, fallback=(ep.name != "primary"))
            except RetryableError as exc:
                last_err = exc
                retry_after = exc.retry_after or min(12.0, 1.2 * (2 ** attempt) + random.uniform(0, 0.4))
                time.sleep(retry_after)
        raise last_err or RuntimeError(f"{ep.name}: unknown error")

    def _request(self, ep: Endpoint, system: str, messages: list[dict],
                 max_tokens: int, fallback: bool) -> LLMResult:
        body = json.dumps({
            "model": ep.model,
            "messages": [{"role": "system", "content": system}] + messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{ep.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {ep.key}"}, method="POST")
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            retry_after = None
            try:
                retry_after = float(exc.headers.get("Retry-After", 0)) or None
            except (TypeError, ValueError):
                pass
            if exc.code in RETRY_STATUS:
                raise RetryableError(f"HTTP {exc.code}", retry_after) from exc
            raise RuntimeError(f"HTTP {exc.code}: {exc.read()[:200]}") from exc
        except urllib.error.URLError as exc:
            raise RetryableError(f"network: {exc.reason}") from exc

        usage = data.get("usage") or {}
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return LLMResult(
            text=(text or "").strip(), endpoint=ep.name, model=ep.model,
            ms=int((time.time() - t0) * 1000),
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            fallback=fallback)


class RetryableError(Exception):
    def __init__(self, msg: str, retry_after: float | None = None) -> None:
        super().__init__(msg)
        self.retry_after = retry_after


def _env_of(name: str | None) -> str:
    import os
    return os.environ.get(name, "") if name else ""


__all__ = ["LLMGateway", "LLMResult", "LLMNotConfigured", "Endpoint"]
