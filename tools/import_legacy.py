# -*- coding: utf-8 -*-
"""旧项目资产导入（可选，只拷素材文件，绝不迁移代码）。

用法：python tools/import_legacy.py [--from "E:\\d盘临时文件夹\\虚拟直播"] [--dry]

拷贝清单（存在才拷，全部带 --dry 预览）：
  runtime/*.wav 声线（CosyVoice2 克隆参考音） → characters/voices/
  data/memory.json → data/memory_legacy.json（新项目首次启动后可人工合并）
  docs 里的人设描述 → characters/xiaoyu/legacy_persona.md
  assets/music 曲库 → characters/music/
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

DEFAULT_FROM = Path(r"E:\d盘临时文件夹\虚拟直播")
HERE = Path(__file__).resolve().parent.parent


def copy_if_exists(src: Path, dst: Path, dry: bool, label: str) -> bool:
    if not src.exists():
        print(f"  [跳过] {label}: {src} 不存在")
        return False
    if dry:
        print(f"  [预览] {label}: {src} → {dst}")
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)
    print(f"  [完成] {label}: {dst}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="旧项目素材导入（可选）")
    parser.add_argument("--from", dest="src", default=str(DEFAULT_FROM))
    parser.add_argument("--dry", action="store_true", help="只预览不拷贝")
    args = parser.parse_args()
    src = Path(args.src)

    print(f"导入源：{src}")
    print("1) 声线素材（wav 参考音）")
    runtime = src / "runtime"
    if runtime.exists():
        n = 0
        for wav in sorted(runtime.glob("*.wav")):
            n += copy_if_exists(wav, HERE / "characters" / "voices" / wav.name, args.dry, f"声线 {wav.name}")
        if n == 0:
            print("  （runtime 下没有 .wav）")
    print("2) 观众记忆数据（合并用，不覆盖新项目）")
    copy_if_exists(src / "runtime" / "memory.json",
                   HERE / "data" / "memory_legacy.json", args.dry, "memory.json")
    print("3) 旧人设文档（参考用）")
    copy_if_exists(src / "docs" / "交接提示词.md",
                   HERE / "characters" / "xiaoyu" / "legacy_persona.md", args.dry, "交接提示词")
    print("4) BGM / 点歌曲库")
    for music_dir in (src / "runtime" / "assets" / "music", src / "assets" / "music"):
        if music_dir.exists():
            copy_if_exists(music_dir, HERE / "characters" / "music", args.dry, f"曲库 {music_dir.name}")
            break
    print("完成。注意：memory_legacy.json 只作参考，正式记忆以新项目 data/memory.json 为准。")


if __name__ == "__main__":
    main()
