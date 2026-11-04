# -*- coding: utf-8 -*-
"""绿幕键控管线 v5（为 v2 素材定制：纯绿背景 + 全身不透明角色）。

绿幕是键控的数学最优情形：
  - 角色配色纪律（无绿）→ 色相距离即权威判据
  - 底色均匀 → 逐像素底色板（多项式拟合）吸收轻微 vignette
  - 预乘前景存储 + shader 零底色还原（同 v4.8 架构）
  - despill：边缘像素的绿色反弹（白围裙吸绿光）钳制

用法：python make_assets_green.py <源视频路径> [状态名]（缺省 idle）
输出：characters/avatar/fish2_<state>_rgb.mp4 + _a.mp4 + manifest
"""
import json
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

import numpy as np
from scipy.ndimage import convolve, binary_erosion, binary_fill_holes

import faulthandler
faulthandler.dump_traceback_later(180, repeat=True)

PROJECT = Path(__file__).resolve().parent.parent
TMP = Path(r"E:\qc_tmp\assets_green")
TMP.mkdir(parents=True, exist_ok=True)
OUTDIR = PROJECT / "characters" / "avatar"
W = H = 720
K_SEAM = 24
T_IN, T_OUT = 30.0, 110.0     # 绿色色距键控阈值（角色内容离绿 ≥150+，实测后可收紧）


def a_to_u8(alpha):
    return np.clip(np.round(np.clip(alpha, 0, 1) * 219.0 + 16.0), 16, 235).astype(np.uint8)


def smoothstep(x, lo, hi):
    t = np.clip((x - lo) / (hi - lo), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def probe_nframes(src):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "default=noprint_wrappers=1:nokey=1", src],
        capture_output=True, text=True).stdout.strip()
    try:
        return int(out)
    except ValueError:
        return -1


def decode_pipe(src):
    p = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", src, "-vf", "scale=in_color_matrix=bt709",
         "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, bufsize=W * H * 3 * 8)

    def read_frame():
        buf = p.stdout.read(W * H * 3)
        if len(buf) < W * H * 3:
            return None
        return np.frombuffer(buf, np.uint8).reshape(H, W, 3)

    return p, read_frame


RGB_ENC = ["-vf", "scale=out_color_matrix=bt709", "-c:v", "libx264", "-crf", "10",
           "-preset", "slow", "-pix_fmt", "yuv420p", "-colorspace", "bt709",
           "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
           "-movflags", "+faststart"]
MASK_ENC = ["-vf", "scale=out_color_matrix=bt709", "-c:v", "libx264", "-qp", "0",
            "-preset", "medium", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]


def fit_bg_plane(frames_rgb):
    """边框环二次多项式拟合（绿幕轻微不均匀的兜底）。"""
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    ring = np.zeros((H, W), bool)
    m = 22
    ring[:m, :] = True; ring[:, :m] = True; ring[:, -m:] = True
    ring[-120:, :] = True          # 绿幕无角色贴边假设，底环也采样
    ys, xs = yy[ring], xx[ring]
    A = np.stack([np.ones_like(ys), xs, ys, xs * xs, xs * ys, ys * ys], 1)
    med = np.median(np.stack([f[ring] for f in frames_rgb]), axis=0)
    coef = np.zeros((6, 3), np.float32)
    for _ in range(2):
        coef, *_ = np.linalg.lstsq(A, med, rcond=None)
        resid = np.linalg.norm(med - A @ coef, axis=1)
        keep = resid < max(3.0, np.percentile(resid, 97))
        A, med, ys, xs = A[keep], med[keep], ys[keep], xs[keep]
    plane = (coef[0][None, None] + coef[1][None, None] * xx[..., None] + coef[2][None, None] * yy[..., None]
             + coef[3][None, None] * (xx * xx)[..., None] + coef[4][None, None] * (xx * yy)[..., None]
             + coef[5][None, None] * (yy * yy)[..., None])
    return np.clip(plane, 0, 255).astype(np.float32)


def green_key(alpha_hint, rgb_f, bg_map):
    """绿幕键控 + despill。alpha_hint: isnet matte（可选辅助，None 则纯键控）。"""
    import cv2 as _cv
    dist = np.sqrt(((rgb_f - bg_map) ** 2).sum(-1))
    key = smoothstep(dist, T_IN, T_OUT)
    # 绿色色度判据：G 显著高于 R/B 且色相落在绿区 → 即使亮度接近也压 0
    mx = np.maximum(rgb_f[..., 0], rgb_f[..., 2])
    greenness = (rgb_f[..., 1] - mx).astype(np.float32)
    gkey = smoothstep(greenness, 8.0, 40.0)
    alpha = key * (1.0 - gkey)
    # 3×3 高斯抗锯齿
    k = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], np.float32) / 16.0
    alpha = convolve(alpha, k, mode="nearest")
    # despill：实体区内绿色反弹钳制（白围裙吸绿光 → G 压到 max(R,B)）
    solid = alpha > 0.85
    spill = (rgb_f[..., 1] - mx) > 6
    fix = solid & spill
    rgb_out = rgb_f.copy()
    rgb_out[..., 1][fix] = mx[fix]
    # 内部实体保证（isnet 辅助时）
    if alpha_hint is not None:
        core = binary_fill_holes(alpha_hint > 0.75) & (alpha > 0.5)
        core = binary_erosion(core, np.ones((3, 3)))
        alpha[core] = 1.0
        rgb_out[core] = rgb_f[core]
    return np.clip(alpha, 0, 1), rgb_out


def main():
    src = sys.argv[1]
    state = sys.argv[2] if len(sys.argv) > 2 else "idle"
    session = None
    try:
        from rembg import new_session, remove
        session = new_session("isnet-general-use")
    except Exception:
        print("[warn] rembg 不可用，纯键控模式", flush=True)

    t0 = time.time()
    n = probe_nframes(src)
    print(f"[{state}] {n} 帧", flush=True)
    p, read_frame = decode_pipe(src)
    head = read_frame()
    mid = head.copy()
    tail = head.copy()

    def grab(idx):
        d = subprocess.run(["ffmpeg", "-v", "error", "-i", src,
                            "-vf", f"select=eq(n\\,{idx})", "-frames:v", "1",
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           capture_output=True).stdout
        return np.frombuffer(d, np.uint8).reshape(H, W, 3).astype(np.float32)

    mid = grab(n // 2); tail = grab(n - 1)
    bg_map = fit_bg_plane([head, mid, tail])
    bg_med = np.median(bg_map.reshape(-1, 3), axis=0)
    print(f"[{state}] 绿幕底色 RGB={bg_med.round(1).tolist()}", flush=True)

    head_hint = None
    if session is not None:
        head_hint = (remove(np.ascontiguousarray(head), session=session)[..., 3].astype(np.float32) / 255.0)
    head_a, head_rgb_clean = green_key(head_hint, head.astype(np.float32), bg_map)

    rgb_enc = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "warning", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", "24", "-i", "-"] + RGB_ENC + [str(TMP / f"fish2_{state}_rgb.mp4")],
        stdin=subprocess.PIPE, stderr=open(TMP / f"enc_{state}_rgb.err", "w", encoding="utf-8"))
    a_enc = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "warning", "-f", "rawvideo", "-pix_fmt", "gray",
         "-s", f"{W}x{H}", "-r", "24", "-i", "-"] + MASK_ENC + [str(TMP / f"fish2_{state}_a.mp4")],
        stdin=subprocess.PIPE, stderr=open(TMP / f"enc_{state}_a.err", "w", encoding="utf-8"))

    def write_pair(prem, alpha):
        rgb_enc.stdin.write(np.clip(prem, 0, 255).astype(np.uint8).tobytes())
        a_enc.stdin.write(a_to_u8(alpha).tobytes())

    def premul(rgb_clean, alpha):
        a3 = alpha[..., None]
        return np.clip(rgb_clean * a3, 0, 255) * (a3 > 0.004)

    write_pair(premul(head_rgb_clean, head_a), head_a)
    i = 1
    while True:
        fr = read_frame()
        if fr is None:
            break
        hint = None
        if session is not None:
            hint = (remove(np.ascontiguousarray(fr), session=session)[..., 3].astype(np.float32) / 255.0)
        a, rgb_clean = green_key(hint, fr.astype(np.float32), bg_map)
        if i >= n - K_SEAM:
            # 首尾交叉淡化：观测空间混合后再键控一次，保证混合帧的 alpha/颜色自洽
            t = (i - (n - K_SEAM) + 1) / (K_SEAM + 1)
            fr2 = fr.astype(np.float32) * (1 - t) + head.astype(np.float32) * t
            hint2 = None
            if session is not None:
                hint2 = (remove(np.ascontiguousarray(fr2.astype(np.uint8)), session=session)[..., 3].astype(np.float32) / 255.0)
            a, rgb_clean = green_key(hint2, fr2, bg_map)
        prem = premul(rgb_clean, a)
        write_pair(prem, a)
        i += 1
        if i % 60 == 0:
            rate = (time.time() - t0) / i
            print(f"[{state}] {i}/{n}  剩余 {rate * (n - i) / 60:.1f} min", flush=True)
    p.wait()
    rgb_enc.stdin.close(); rgb_enc.wait()
    a_enc.stdin.close(); a_enc.wait()
    print(f"[{state}] 编码完成 {time.time() - t0:.0f}s", flush=True)

    for suffix in ("rgb", "a"):
        shutil.move(str(TMP / f"fish2_{state}_{suffix}.mp4"), str(OUTDIR / f"fish2_{state}_{suffix}.mp4"))
    manifest_p = OUTDIR / "fish2_manifest.json"
    man = {}
    if manifest_p.exists():
        man = json.loads(manifest_p.read_text(encoding="utf-8"))
    man[state] = {"rgb": f"/avatar/fish2_{state}_rgb.mp4", "alpha": f"/avatar/fish2_{state}_a.mp4",
                  "storage": "premultiplied", "bg": [0.0, 0.0, 0.0]}
    manifest_p.write_text(json.dumps(man, indent=1), encoding="utf-8")
    print(f"[{state}] [OK] 全部完成", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        print("PIPELINE FAILED", flush=True)
        sys.exit(1)
