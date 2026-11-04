# -*- coding: utf-8 -*-
"""配置中心单测：深合并、点路径、set 持久化到 local.yaml。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Config


def make_config(tmp_path: Path, base: str, local: str = "") -> Config:
    (tmp_path / "config.yaml").write_text(base, encoding="utf-8")
    if local:
        (tmp_path / "local.yaml").write_text(local, encoding="utf-8")
    return Config(config_dir=tmp_path)


def test_deep_merge_and_dotted_get(tmp_path):
    cfg = make_config(tmp_path, "a:\n  b:\n    c: 1\n  d: keep\n",
                      "a:\n  b:\n    c: 2\n")
    assert cfg.get("a.b.c") == 2
    assert cfg.get("a.d") == "keep"
    assert cfg.get("no.such.key", "fallback") == "fallback"


def test_set_persists_to_local(tmp_path):
    cfg = make_config(tmp_path, "modules:\n  music:\n    enabled: false\n")
    assert cfg.get("modules.music.enabled") is False
    cfg.set("modules.music.enabled", True)
    # 重新从磁盘加载，确认落盘
    cfg2 = Config(config_dir=tmp_path)
    assert cfg2.get("modules.music.enabled") is True


def test_reload_detects_change(tmp_path):
    cfg = make_config(tmp_path, "x: 1\n")
    assert cfg.get("x") == 1
    import time as _t
    _t.sleep(0.02)
    (tmp_path / "config.yaml").write_text("x: 2\n", encoding="utf-8")
    assert cfg.reload() is True
    assert cfg.get("x") == 2
