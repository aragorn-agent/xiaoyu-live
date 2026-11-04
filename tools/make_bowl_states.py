# -*- coding: utf-8 -*-
"""点赞加饭 · 碗五态合成器 v2。

改动（相对 v1）：
- 米堆先做绿幕溢色校正（拉红压蓝 → 暖白米饭），再轻度提亮
- 米面用抛物线堆顶曲线（中间拱起），100% 满仓自然高出沿口，不再出现三角尖
- 纹理锚定碗底、宽度略超外沿，由 mask 裁形

用法：python tools/make_bowl_states.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
BOWL_SRC = ROOT / "web" / "static" / "bowl_glass.png"
RICE_SRC = ROOT / "web" / "static" / "rice_mound.png"
OUT_DIR = ROOT / "web" / "static" / "bowl"

W, H = 520, 760
CX = 260.0
RIM_Y = 380.0
RIM_RX = 195.0
RIM_RY = 52.0
WALL = 10.0
OUTER_RX = RIM_RX + WALL

LEVELS = [("00", 0.0), ("30", 0.30), ("60", 0.60), ("90", 0.90), ("100", 1.0)]
INNER_BOT = 542.0    # 由 load_bowl() 的真实映射覆盖


def fix_spill(mound: Image.Image) -> Image.Image:
    """绿幕溢色校正：r += (b-r)*0.55，b *= 0.965，g 压到 (r+b)/2+6，再提亮。"""
    r, g, b, a = mound.split()
    diff = ImageChops.subtract(b, r)                      # b-r（clamp 0，青色溢出量）
    diff = diff.point(lambda v: int(v * 0.55))
    r2 = ImageChops.add(r, diff)
    b2 = b.point(lambda v: int(v * 0.965))
    half_r = r2.point(lambda v: v // 2)
    half_b = b2.point(lambda v: v // 2)
    mid = ImageChops.add(ImageChops.add(half_r, half_b), Image.new("L", mound.size, 6))
    g2 = ImageChops.darker(g, mid)
    out = Image.merge("RGBA", (r2, g2, b2, a))
    out = ImageEnhance.Brightness(out).enhance(1.05)
    out = ImageEnhance.Color(out).enhance(0.88)           # 再去一点彩度，米饭偏暖白
    return out


def load_bowl() -> tuple[Image.Image, dict]:
    im = Image.open(BOWL_SRC).convert("RGBA")
    bbox = im.getchannel("A").getbbox()
    bw = bbox[2] - bbox[0]
    scale = (OUTER_RX * 2) / bw
    new_size = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
    im = im.resize(new_size, Image.LANCZOS)
    bb2 = im.getchannel("A").getbbox()
    paste_x = round(CX - OUTER_RX - bb2[0])
    paste_y = round(RIM_Y - RIM_RY - bb2[1])
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    canvas.alpha_composite(im, (paste_x, paste_y))
    return canvas, {"bowl_bottom_y": bb2[3] + paste_y}


def interior_halfw(y: float) -> float:
    if y <= RIM_Y:
        t = abs(y - RIM_Y) / RIM_RY
        return (RIM_RX - WALL) * math.sqrt(max(0.0, 1.0 - t * t))
    t = min(1.0, max(0.0, (y - RIM_Y) / (INNER_BOT - RIM_Y)))
    return (RIM_RX - WALL) * (1.0 - t) * 0.92 + 30.0 * t


def surface_y(level: float) -> float:
    return INNER_BOT - 14 - level * ((INNER_BOT - 14) - (RIM_Y - RIM_RY * 0.55))


def rice_mask(level: float) -> Image.Image:
    """米饭区域：抛物线堆顶（中部拱起 heap），两侧沿碗内壁收到底。"""
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    ys = surface_y(level)
    heap = 12.0 + level * 42.0
    hw_surf = max(20.0, interior_halfw(max(ys, RIM_Y)))
    poly = []
    steps = 72
    for i in range(steps + 1):                            # 堆顶曲线：左 → 右
        u = -1.0 + 2.0 * i / steps
        x = CX + u * hw_surf
        y = ys - heap * (1.0 - u * u)
        poly.append((x, y))
    for i in range(steps + 1):                            # 右壁 → 左壁
        y = max(ys, RIM_Y) + (INNER_BOT - max(ys, RIM_Y)) * i / steps
        poly.append((CX + interior_halfw(y), y))
    for i in range(steps + 1):                            # 底部回到左侧
        y = INNER_BOT - (INNER_BOT - max(ys, RIM_Y)) * i / steps
        poly.append((CX - interior_halfw(y), y))
    d.polygon(poly, fill=255)
    return m.filter(ImageFilter.GaussianBlur(1.4))


def paste_rice(base: Image.Image, level: float) -> None:
    mound = Image.open(RICE_SRC).convert("RGBA")
    mound = fix_spill(mound)
    target_w = round(OUTER_RX * 2 * 1.08)
    mound = mound.resize((target_w, round(target_w * mound.height / mound.width * 1.15)),
                         Image.LANCZOS)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    layer.alpha_composite(mound, (round(CX - mound.width / 2), round(INNER_BOT - mound.height + 8)))
    mask = rice_mask(level)
    base.paste(layer, (0, 0), Image.composite(layer.split()[3], Image.new("L", (W, H), 0), mask))


def main() -> None:
    global INNER_BOT
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    bowl, meta = load_bowl()
    INNER_BOT = meta["bowl_bottom_y"] - 12.0
    print(f"碗映射: bowl_bottom_y={meta['bowl_bottom_y']:.0f} INNER_BOT={INNER_BOT:.0f}")
    surfaces = {}
    for tag, level in LEVELS:
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        if level > 0:
            paste_rice(canvas, level)
        canvas.alpha_composite(bowl)
        out = OUT_DIR / f"bowl_{tag}.png"
        canvas.save(out, optimize=True)
        surfaces[tag] = round(surface_y(level)) if level > 0 else round(meta["bowl_bottom_y"] - 40)
        print(f"  {out.name}  surfaceY={surfaces[tag]}  {out.stat().st_size // 1024}KB")
    (OUT_DIR / "meta.json").write_text(
        json.dumps({"surface_y": surfaces, "bowl_bottom_y": round(meta["bowl_bottom_y"])},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print("meta.json 已写出")


if __name__ == "__main__":
    main()
