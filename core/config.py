# -*- coding: utf-8 -*-
"""配置中心：config/config.yaml 为主配置，config/local.yaml 为本机覆盖（git 忽略）。

设计要点：
- 深合并：local.yaml 覆盖 config.yaml，其余键不动
- 点路径访问 cfg.get("voice.primary", "edge")
- 热加载：mtime 轮询（默认 2s），变更后回调订阅者（模块各自实现 apply）
- 密钥零落盘：配置里只允许出现环境变量名，实际值统一走 env()
"""
from __future__ import annotations

import copy
import os
import threading
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
CACHE_DIR = DATA_DIR / "cache"


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    def __init__(self, config_dir: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._data: dict = {}
        self._mtime: dict[str, float] = {}
        self._watchers: list[tuple[str, object]] = []
        self._dir = Path(config_dir) if config_dir else CONFIG_DIR
        self.reload()

    # ---------------------------------------------------------------- 加载
    def reload(self) -> bool:
        """重新读盘。返回是否有变化。"""
        merged: dict = {}
        changed = False
        for name in ("config.yaml", "local.yaml"):
            path = self._dir / name
            if not path.exists():
                continue
            mtime = path.stat().st_mtime
            if self._mtime.get(name) == mtime:
                merged = _deep_merge(merged, self._file_cache.get(name, {}))
                continue
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            self._mtime[name] = mtime
            self._file_cache[name] = data
            changed = True
            merged = _deep_merge(merged, data)
        with self._lock:
            old = self._data
            self._data = merged
        return changed or old != merged

    _file_cache: dict[str, dict] = {}

    # ---------------------------------------------------------------- 访问
    def get(self, path: str, default=None):
        """点路径访问：cfg.get("modules.music.enabled", False)。"""
        node: object = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, path: str) -> dict:
        v = self.get(path, {})
        return dict(v) if isinstance(v, dict) else {}

    def set(self, path: str, value, persist: bool = True) -> None:
        """运行时改配置（中控台用）。persist=True 时写进 local.yaml。"""
        with self._lock:
            node = self._data
            parts = path.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        if persist:
            self._persist(path, value)

    def _persist(self, path: str, value) -> None:
        local_path = self._dir / "local.yaml"
        local: dict = {}
        if local_path.exists():
            with open(local_path, "r", encoding="utf-8") as f:
                local = yaml.safe_load(f) or {}
        node = local
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        tmp = local_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(local, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, local_path)
        self._mtime.pop("local.yaml", None)
        self.reload()

    # ---------------------------------------------------------------- 密钥
    @staticmethod
    def env(name: str, default: str = "") -> str:
        """密钥统一入口：只认环境变量，配置文件里不落任何密钥。"""
        return os.environ.get(name, default)

    # ---------------------------------------------------------------- 热加载
    def watch(self, prefix: str, callback) -> None:
        """注册 watcher：reload 后若 prefix 下的值变了就回调 callback()。"""
        self._watchers.append((prefix, callback))

    async def watch_loop(self, interval: float = 2.0):
        """asyncio 热加载轮询任务（app 启动时挂）。"""
        import asyncio

        while True:
            await asyncio.sleep(interval)
            try:
                self._poll()
            except Exception as exc:  # 热加载失败绝不弄崩直播
                print(f"[config] reload error: {type(exc).__name__}: {exc}", flush=True)

    def _poll(self) -> None:
        for name in ("config.yaml", "local.yaml"):
            path = self._dir / name
            if path.exists():
                mtime = path.stat().st_mtime
                if self._mtime.get(name) != mtime:
                    break
        else:
            return
        before = copy.deepcopy(self._data)
        self.reload()
        for prefix, callback in self._watchers:
            if self.get(prefix) != _dig(before, prefix):
                try:
                    callback()
                except Exception as exc:
                    print(f"[config] watcher {prefix} error: {exc}", flush=True)


def _dig(data: dict, path: str):
    node = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


__all__ = ["Config", "ROOT", "DATA_DIR", "LOG_DIR", "CACHE_DIR"]
