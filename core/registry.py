# -*- coding: utf-8 -*-
"""模块注册器：所有可选功能（选配）都注册成 Module，由 config.modules.*.enabled 决定启停。

契约（参考 dafeiyu-ai-live 的插件能力声明思想，自实现）：
- Module.name 唯一；enabled 读 config["modules"][name]["enabled"]（默认取模块声明的默认值）
- async start(ctx) / async stop()；注册器只管生命周期与状态上报，不做别的
- 中控台读 status() 展示每个模块的状态
"""
from __future__ import annotations

import asyncio
import inspect
import time
from typing import Protocol


class Module(Protocol):
    name: str
    default_enabled: bool

    async def start(self, ctx) -> None: ...
    async def stop(self) -> None: ...


class ModuleHandle:
    def __init__(self, module, enabled: bool) -> None:
        self.module = module
        self.enabled = enabled
        self.state = "stopped"      # stopped / starting / running / error
        self.error = ""
        self.started_at = 0.0


class ModuleRegistry:
    def __init__(self, config) -> None:
        self._config = config
        self._modules: dict[str, ModuleHandle] = {}

    def register(self, module) -> bool:
        """注册并按配置决定是否启用。返回是否启用。"""
        if not hasattr(module, "name"):
            raise ValueError("module must have .name")
        flag = self._config.get(f"modules.{module.name}.enabled", None)
        if flag is None:
            flag = bool(getattr(module, "default_enabled", False))
        handle = ModuleHandle(module, bool(flag))
        self._modules[module.name] = handle
        return handle.enabled

    def set_enabled(self, name: str, enabled: bool) -> None:
        if name in self._modules:
            self._modules[name].enabled = bool(enabled)

    def get(self, name: str):
        return self._modules.get(name)

    async def start_all(self, ctx) -> None:
        for name, handle in list(self._modules.items()):
            if not handle.enabled:
                continue
            await self._start_one(name, ctx)

    async def _start_one(self, name: str, ctx) -> None:
        handle = self._modules.get(name)
        if handle is None:
            return
        handle.state = "starting"
        try:
            result = handle.module.start(ctx)
            if inspect.isawaitable(result):
                await result
            handle.state = "running"
            handle.started_at = time.time()
            handle.error = ""
        except Exception as exc:
            handle.state = "error"
            handle.error = f"{type(exc).__name__}: {exc}"
            print(f"[registry] module {name} start failed: {handle.error}", flush=True)

    async def stop_all(self) -> None:
        for name, handle in list(self._modules.items()):
            if handle.state not in ("running", "error", "starting"):
                continue
            try:
                result = handle.module.stop()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                print(f"[registry] module {name} stop failed: {exc}", flush=True)
            handle.state = "stopped"

    async def restart(self, name: str, ctx) -> None:
        handle = self._modules.get(name)
        if handle is None:
            return
        try:
            result = handle.module.stop()
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass
        await self._start_one(name, ctx)

    def status(self) -> dict:
        return {
            name: {
                "enabled": h.enabled,
                "state": h.state,
                "error": h.error,
                "uptime_s": round(time.time() - h.started_at, 1) if h.started_at else 0,
            }
            for name, h in self._modules.items()
        }


__all__ = ["ModuleRegistry", "ModuleHandle"]
