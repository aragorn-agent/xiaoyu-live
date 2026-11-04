# -*- coding: utf-8 -*-
"""豆包生成图 → 舞台碗五态 管线。

输入：本目录临时文件夹的豆包生成图（1536×1536 绿幕）
  c1=空碗 n1=30% g1=60% g2=90% g3=100% n2=米粒堆
输出（web/static/bowl/）：
  bowl_00/30/60/90/100.png —— 键控+对齐到舞台碗几何（520×760），五态碗体不跳位
  grain_0/1/2.png          —— 米粒粒子贴图（撒落动画用）
  meta.json                —— 各态米面高度 surfaceY
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter

TMP = Path(r"C:\Users\a\AppData\Local\Temp")
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "static" / "bowl"

W, H = 520, 760
CX = 260.0
RIM_Y = 380.0
RIM_RX = 195.0
RIM_RY = 52.0
WALL = 10.0
OUTER_RX = RIM_RX + WALL

SOURCES = {
    "00": TMP / "bowl_c1.png",
    "30": TMP / "n1.png",
    "60": TMP / "g1.png",
    "90": TMP / "g2.png",
    "100": TMP / "g3.png",
}


def key_green(im: Image.Image) -> Image.Image:
    """绿幕抠像 v4：HSV 饱和度硬门控（实测：米/玻璃 S<70，绿桌/绿背景 S 100-180）
    + 中值去噪 + 连通域过滤 + 溢色抑制。"""
    rgba = im.convert("RGBA")
    hsv = rgba.convert("HSV")
    _h, s, _v = hsv.split()
    # 饱和度门控：S<=55 全不透明，S>=105 全透明（绿桌 S≈107、背景绿 S 120-180 全灭）
    alpha = s.point(lambda v: 255 if v <= 55 else (0 if v >= 105 else int(255 - (v - 55) * 5.1)))
    alpha = alpha.filter(ImageFilter.MedianFilter(5))
    alpha = _keep_main_components(alpha)
    r, g, b, _ = rgba.split()
    half_r = r.point(lambda v: v // 2)
    half_b = b.point(lambda v: v // 2)
    rb_mid = ImageChops.add(half_r, half_b)
    mid_soft = ImageChops.add(rb_mid, Image.new("L", im.size, 12))
    g2 = ImageChops.darker(g, mid_soft)                     # 溢色抑制
    alpha = alpha.filter(ImageFilter.GaussianBlur(1.0))
    return Image.merge("RGBA", (r, g2, b, alpha))


def _keep_main_components(alpha: Image.Image) -> Image.Image:
    """保留与碗体相连的大块连通域，抹掉背景渐变残留的散斑。"""
    small = alpha.resize((alpha.width // 8, alpha.height // 8), Image.BILINEAR)
    w, h = small.size
    px = small.load()
    binmap = [1 if px[x, y] >= 110 else 0 for y in range(h) for x in range(w)]
    labels = [0] * (w * h)
    sizes = {}
    cur = 0
    for start in range(w * h):
        if binmap[start] and labels[start] == 0:
            cur += 1
            stack = [start]
            labels[start] = cur
            size = 0
            while stack:
                idx = stack.pop()
                size += 1
                x, y = idx % w, idx // w
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < w and 0 <= ny < h:
                        nidx = ny * w + nx
                        if binmap[nidx] and labels[nidx] == 0:
                            labels[nidx] = cur
                            stack.append(nidx)
            sizes[cur] = size
    if not sizes:
        return alpha
    main_id = max(sizes, key=sizes.get)
    main_size = sizes[main_id]
    keep = {i for i, sz in sizes.items() if sz >= main_size * 0.25}
    mask = Image.new("L", (w, h), 0)
    mpx = mask.load()
    for y in range(h):
        for x in range(w):
            if labels[y * w + x] in keep:
                mpx[x, y] = 255
    mask_full = mask.resize(alpha.size, Image.BILINEAR).filter(ImageFilter.GaussianBlur(1.5))
    return ImageChops.multiply(alpha, mask_full)


def bowl_region(keyed: Image.Image) -> Image.Image:
    """裁出碗体：碗沿最宽行定位 rim；向下容忍碗脚外扩（桌面触边/大幅出界即断），
    向上容忍 100% 态高出沿口的米堆穹顶（横向不出沿口范围即继续）。"""
    a = keyed.getchannel("A")
    factor = 4
    small = a.resize((keyed.width // factor, keyed.height // factor), Image.BILINEAR)
    sw, sh = small.size
    p = small.load()
    spans = []
    for y in range(sh):
        xs = [x for x in range(0, sw, 2) if p[x, y] > 120]
        spans.append((min(xs), max(xs)) if xs else None)
    rim_y = max(range(sh), key=lambda y: (spans[y][1] - spans[y][0]) if spans[y] else 0)
    rim_l, rim_r = spans[rim_y]
    rim_w = rim_r - rim_l
    lo_lim = rim_l - 0.12 * rim_w     # 碗脚可略微外扩
    hi_lim = rim_r + 0.12 * rim_w
    top_y = rim_y
    for y in range(rim_y - 1, -1, -1):            # 向上：穹顶在沿口横向范围内则继续
        if not spans[y]:
            break
        l, r = spans[y]
        if l < rim_l - 0.06 * rim_w or r > rim_r + 0.06 * rim_w:
            break
        top_y = y
    bottom_y = rim_y
    for y in range(rim_y + 1, sh):                # 向下：碗脚外扩可接受，桌面触边即断
        if not spans[y]:
            break
        l, r = spans[y]
        if l <= 2 or r >= sw - 3 or l < lo_lim or r > hi_lim:
            break
        bottom_y = y
    x0 = max(0, (rim_l - 4) * factor)
    x1 = min(keyed.width, (rim_r + 4) * factor)
    y0 = max(0, (top_y - 2) * factor)
    y1 = min(keyed.height, (bottom_y + 2) * factor)
    return keyed.crop((x0, y0, x1, y1)), rim_w * factor


def fit_stage(keyed: Image.Image) -> tuple[Image.Image, dict]:
    """碗体裁剪后：沿口宽度对齐 2*OUTER_RX，沿口顶=RIM_Y-RIM_RY，水平居中。"""
    bowl, rim_w = bowl_region(keyed)
    scale = (OUTER_RX * 2) / rim_w
    im2 = bowl.resize((max(1, round(bowl.width * scale)), max(1, round(bowl.height * scale))), Image.LANCZOS)
    bb = im2.getchannel("A").getbbox()
    px = round(CX - (bb[0] + bb[2]) / 2.0)
    py = round(RIM_Y - RIM_RY - bb[1])
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    canvas.alpha_composite(im2, (px, py))
    return canvas, {"bowl_bottom_y": bb[3] + py}


def detect_surface(canvas: Image.Image, bowl_bottom: float) -> float:
    """自动检测米面高度：逐行求"最长连续白像素段"，段长≥130px 记为米行，
    取最长连续米行段的顶端（玻璃竖高光条横向碎短，不会误判）。"""
    px = canvas.load()
    x0 = int(CX - OUTER_RX) + 6
    x1 = int(CX + OUTER_RX) - 6
    top = max(40, int(RIM_Y - RIM_RY - 130))     # 100% 穹顶可高出沿口
    bot = int(bowl_bottom - 10)

    def rice_row(y: int) -> bool:
        run = best = 0
        for x in range(x0, x1):
            r, g, b, a = px[x, y]
            if a > 210 and r > 185 and g > 185 and b > 170 and r - b >= 5:
                run += 1                     # 暖白（奶油色）才算米：玻璃反光是冷白
                best = max(best, run)
            else:
                run = 0
        return best >= 130

    best_top = bot - 20
    best_len = 0
    run = []
    for y in range(bot, top, -2):
        if rice_row(y):
            run.append(y)
        else:
            if len(run) > best_len:
                best_len = len(run)
                best_top = run[-1]
            run = []
    if len(run) > best_len:
        best_top = run[-1]
    return float(best_top)


def cut_grains(im: Image.Image) -> list[Image.Image]:
    """从米粒堆图裁 3 小撮米粒做粒子贴图（只取上半部，避开桌面）。"""
    keyed = key_green(im)
    bbox = keyed.getchannel("A").getbbox()
    x0, y0, x1, y1 = bbox
    w, h = x1 - x0, y1 - y0
    y1 = y0 + int(h * 0.72)                       # 只取堆的上部
    patches = [
        (x0 + int(w * 0.06), y0 + int(h * 0.18), x0 + int(w * 0.30), y0 + int(h * 0.46)),   # 左上缘
        (x0 + int(w * 0.38), y0 + int(h * 0.02), x0 + int(w * 0.62), y0 + int(h * 0.30)),   # 顶部
        (x1 - int(w * 0.30), y0 + int(h * 0.20), x1 - int(w * 0.06), y0 + int(h * 0.48)),   # 右上缘
    ]
    outs = []
    for i, box in enumerate(patches):
        p = keyed.crop(box)
        if p.width < 20 or p.height < 14:
            continue
        p = p.resize((64, max(14, round(64 * p.height / p.width))), Image.LANCZOS)
        outs.append(p)
    return outs


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    surfaces = {}
    bowl_bottom_ref = None
    for tag, src in SOURCES.items():
        keyed = key_green(Image.open(src))
        canvas, meta = fit_stage(keyed)
        bowl_bottom_ref = meta["bowl_bottom_y"] if bowl_bottom_ref is None else bowl_bottom_ref
        surface = detect_surface(canvas, meta["bowl_bottom_y"]) if tag != "00" else meta["bowl_bottom_y"] - 40
        surfaces[tag] = round(surface)
        out = OUT / f"bowl_{tag}.png"
        canvas.save(out, optimize=True)
        print(f"bowl_{tag}.png  bottom={meta['bowl_bottom_y']:.0f} surfaceY={surfaces[tag]} "
              f"{out.stat().st_size // 1024}KB")
    # 米粒粒子
    for i, p in enumerate(cut_grains(Image.open(TMP / "n2.png"))):
        p.save(OUT / f"grain_{i}.png", optimize=True)
        print(f"grain_{i}.png {p.size}")
    (OUT / "meta.json").write_text(json.dumps({"surface_y": surfaces}, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    print("done")


if __name__ == "__main__":
    main()
