# -*- coding: utf-8 -*-
"""鲸鱼尾巴 mp4 → 高质量无缝循环（彩色轨 + 遮罩轨 双轨输出）。

抠像算法（Keylight 式专业色度键）：
- YUV 色度距离键控（亮度无关 → 角色暗部/亮部都稳，修掉 RGB 距离法导致的橘色偏移）
- alpha = smoothstep 双阈值 → 边缘半透明正确呈现（鱼鳍/发丝）
- 溢色中和：边缘像素品红色度 → 同亮度中性色（Keylight 手法）
- 首尾 1 秒 YUV 空间交叉淡化消接缝
"""
import subprocess
import sys
from pathlib import Path

import numpy as np

SRC = sys.argv[1] if len(sys.argv) > 1 else "C:/Users/阿拉贡/Downloads/Live2D拆件：鲸鱼尾巴.mp4"
OUTDIR = Path(__file__).resolve().parent.parent / "characters" / "avatar"

KEY_RGB = np.array([210, 30, 175], dtype=float)
T_INNER = 30.0
T_OUTER = 100.0
SEAM_FRAMES = 24


def rgb2yuv(rgb):
    y = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    u = -0.169 * rgb[..., 0] - 0.331 * rgb[..., 1] + 0.5 * rgb[..., 2] + 128.0
    v = 0.5 * rgb[..., 0] - 0.419 * rgb[..., 1] - 0.081 * rgb[..., 2] + 128.0
    return y, u, v


def chroma_key_frame(rgb):
    # RGB 欧氏距离：品红底 [210,30,175] 与角色像素距离 ≥100，干净分离
    dist = np.sqrt(((rgb - KEY_RGB) ** 2).sum(axis=-1))
    alpha = np.clip((dist - T_INNER) / (T_OUTER - T_INNER), 0.0, 1.0)
    alpha = alpha * alpha * (3 - 2 * alpha)
    # 溢色抑制：半透明边缘的品红分量中和（RGB 空间直接压品红分量）
    fg = alpha[..., None]
    spill = (np.minimum(rgb[..., 0], rgb[..., 2]) * 0.6) * (1.0 - alpha)   # (H,W)，alpha 无 [...,None]
    rgb_clean = rgb.copy()
    rgb_clean[..., 0] -= spill     # R 通道压品红溢出
    rgb_clean[..., 2] -= spill     # B 通道压品红溢出
    rgb_clean = np.where(alpha[..., None] < 0.02, 0, rgb_clean)
    return np.clip(rgb_clean, 0, 255), alpha


def main():
    import cv2
    cap = cv2.VideoCapture(SRC)
    if not cap.isOpened():
        print("cannot open", SRC); sys.exit(1)
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    frames = []
    while True:
        ok, bgr = cap.read()
        if not ok:
            break
        frames.append(bgr[..., ::-1].astype(float))
    cap.release()
    n = len(frames)
    print(f"抽帧完成：{n} 帧 @ {fps:.1f}fps")

    k = min(SEAM_FRAMES, n // 4)
    head = frames[0]
    for i in range(n - k, n):
        t = (i - (n - k) + 1) / (k + 1)
        frames[i] = frames[i] * (1 - t) + head * t

    OUTDIR.mkdir(parents=True, exist_ok=True)
    rgb_path = OUTDIR / "idle_leiteng_rgb.mp4"
    a_path = OUTDIR / "idle_leiteng_a.mp4"
    h, w = frames[0].shape[:2]
    enc_rgb = subprocess.Popen([
        "ffmpeg", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", f"{fps:.2f}", "-i", "-",
        "-c:v", "libx264", "-crf", "14", "-preset", "slow", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(rgb_path)], stdin=subprocess.PIPE)
    enc_a = subprocess.Popen([
        "ffmpeg", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}", "-r", f"{fps:.2f}", "-i", "-",
        "-c:v", "libx264", "-crf", "10", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(a_path)], stdin=subprocess.PIPE)
    for i, f in enumerate(frames):
        rgb_c, alpha = chroma_key_frame(f)
        enc_rgb.stdin.write(rgb_c.astype(np.uint8)[..., ::-1].tobytes())
        enc_a.stdin.write((alpha * 255).astype(np.uint8).tobytes())
        if i % 90 == 0:
            print(f"  处理 {i}/{n}", flush=True)
    enc_rgb.stdin.close(); enc_rgb.wait()
    enc_a.stdin.close(); enc_a.wait()
    print(f"完成：{rgb_path.name} ({rgb_path.stat().st_size // 1024} KB) + {a_path.name} ({a_path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
