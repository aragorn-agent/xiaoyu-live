# -*- coding: utf-8 -*-
"""抠像管线 v2：rembg AI matte + 双轨输出，3 状态（idle / laugh / talk）。

与 v1 (make_loop.py) 的区别 —— v1 颜色崩坏的根因：
  1. rgb24 rawvideo 中间格式 → yuv↔rgb 默认 bt601 矩阵 → R+19/B-30 色偏
  2. RGB 距离色键吃掉紫发尖端/半透明鱼尾 → alpha 错 → shader 逆合成放大误差
  3. 溢色抑制直接改写角色边缘 RGB

v2 方案：
  - rembg isnet-general-use AI 抠像（不依赖底色，紫发/鱼尾全保真）
  - alpha guard：颜色越接近底色 alpha 压制越强（消灭品红色晕，角色本体不受影响）
  - 全链路显式 bt709：解码 in_color_matrix=bt709，编码 out_color_matrix=bt709 + 打标签
  - RGB 轨 yuv420p 直编（无 rgb 中间格式，实测色偏 <1% 亮度、无色相偏移）
  - 遮罩轨 h264 无损（-qp 0），限位展开约定与 stage.html shader 一致 (16..235)
  - 首尾 24 帧交叉淡化（RGB 与 alpha 同步混合，数学上等价于前景混合）
"""
import json
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

import numpy as np
from rembg import new_session, remove
from scipy.ndimage import binary_closing, binary_fill_holes

import faulthandler
faulthandler.dump_traceback_later(180, repeat=True)   # 每 3 分钟转储调用栈，卡死可定位

PROJECT = Path(__file__).resolve().parent.parent
TMP = Path(r"E:\qc_tmp\assets_v2")
TMP.mkdir(parents=True, exist_ok=True)
OUTDIR = PROJECT / "characters" / "avatar"

STATES = [
    ("idle", r"C:\Users\阿拉贡\Downloads\Live2D拆件：鲸鱼尾巴.mp4"),
    ("laugh", r"C:\Users\阿拉贡\Downloads\Live2D拆件：鲸鱼尾巴22.mp4"),
    ("talk", r"C:\Users\阿拉贡\Downloads\Live2D拆件：鲸鱼尾巴33.mp4"),
]
K_SEAM = 24          # 首尾交叉淡化帧数（1 秒）
GUARD_LO, GUARD_HI = 25.0, 60.0   # alpha guard 的底色距离阈值
W = H = 720
MODEL = "isnet-general-use"


def probe_nframes(src):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "default=noprint_wrappers=1:nokey=1", src],
        capture_output=True, text=True).stdout.strip()
    return int(out)


def decode_pipe(src):
    """流式解码全部帧（bt709 解读），返回 (proc, read_frame)。"""
    p = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", src,
         "-vf", "scale=in_color_matrix=bt709", "-pix_fmt", "rgb24",
         "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, bufsize=W * H * 3 * 8)

    def read_frame():
        buf = p.stdout.read(W * H * 3)
        if len(buf) < W * H * 3:
            return None
        return np.frombuffer(buf, np.uint8).reshape(H, W, 3)

    return p, read_frame


def encoder(args_extra, out_path, errlog):
    return subprocess.Popen(
        ["ffmpeg", "-y", "-v", "warning", "-f", "rawvideo",
         "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", "24", "-i", "-"]
        + args_extra + [str(out_path)], stdin=subprocess.PIPE,
        stderr=open(errlog, "w", encoding="utf-8"))


RGB_ENC = ["-vf", "scale=out_color_matrix=bt709", "-c:v", "libx264", "-crf", "10",
           "-preset", "slow", "-pix_fmt", "yuv420p", "-colorspace", "bt709",
           "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
           "-movflags", "+faststart"]
MASK_ENC = ["-vf", "scale=out_color_matrix=bt709", "-c:v", "libx264", "-qp", "0",
            "-preset", "medium", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart"]

# 遮罩轨量程约定（实测 ffmpeg gray→yuv420p 不做限位压缩，直通）：
# 写入字节 = 16 + alpha*219（严格落在 16..235），shader 端 (v/255-0.0627)/0.8745 严格还原
def a_to_u8(alpha):
    return np.clip(np.round(np.clip(alpha, 0, 1) * 219.0 + 16.0), 16, 235).astype(np.uint8)


def encode_gray_pipe(alpha01_frames, out_path):
    """把 alpha 帧写进无损灰度轨。alpha01_frames: 迭代器 yielding float32 (H,W) 0..1"""
    p = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "gray",
         "-s", f"{W}x{H}", "-r", "24", "-i", "-"] + MASK_ENC + [str(out_path)],
        stdin=subprocess.PIPE)
    for a in alpha01_frames:
        p.stdin.write(a_to_u8(a).tobytes())
    p.stdin.close()
    return p.wait()


def smoothstep(x, lo, hi):
    t = np.clip((x - lo) / (hi - lo), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def clean_alpha(alpha, rgb_f, bg):
    """AI matte 清洗 v3：
    - 色距 guard 只作用于「软边/低置信」像素（按 alpha 软硬度加权）。
      v2 的教训：guard 无差别作用于全帧，珊瑚底视频里暗部肤色离底色仅 dist≈34，
      落在压制区间 → 腿/手/脸的实体肉被抠成半透明。
      现在：实体像素（alpha→1）完全豁免；纯底色软晕（obs≈bg, alpha 中低）仍被清除。
    - 实体轮廓内部填洞：运动帧 isnet 偶发的脸/手/腿内部空洞强制实体，
      填洞基于闭运算轮廓，位于身形之外的底色晕不受影响。"""
    dist = np.sqrt(((rgb_f - bg) ** 2).sum(-1))
    guard = smoothstep(dist, GUARD_LO, GUARD_HI)            # 1 = 颜色远离底色
    soft = 1.0 - smoothstep(alpha, 0.55, 0.85)              # 1 = 软边, 0 = 实体
    alpha = alpha * (1.0 - soft * (1.0 - guard))
    solid = alpha > 0.75
    if solid.sum() > 500:
        filled = binary_fill_holes(binary_closing(solid, structure=np.ones((5, 5))))
        alpha = np.maximum(alpha, filled.astype(np.float32))
    return alpha


def main():
    only = sys.argv[1:] or None
    session = new_session(MODEL)
    manifest = {}

    # ── 遮罩轨量程自检：alpha 0/0.25/0.5/0.75/1 → 写 16+219a → 解码 → shader 公式还原 ──
    test_png = TMP / "range_probe.mp4"
    probes = [0.0, 0.25, 0.5, 0.75, 1.0]
    encode_gray_pipe([np.full((H, W), p, np.float32) for p in probes], test_png)
    d = subprocess.run(["ffmpeg", "-v", "error", "-i", str(test_png), "-f", "rawvideo",
                        "-pix_fmt", "gray", "-"], capture_output=True).stdout
    vals = np.frombuffer(d, np.uint8).reshape(len(probes), H, W)[:, 0, 0]
    expand = (vals.astype(np.float32) - 16.0) / 219.0
    print(f"[range probe] alpha={probes} encoded={vals.tolist()} expanded={expand.round(4).tolist()}", flush=True)
    err = np.abs(expand - np.array(probes)).max()
    assert err < 0.02, f"遮罩轨量程往返误差过大: {err:.3f}"

    for state, src in STATES:
        if only and state not in only:
            continue
        t0 = time.time()
        n = probe_nframes(src)
        tmp_rgb, tmp_a = TMP / f"fish_{state}_rgb.mp4", TMP / f"fish_{state}_a.mp4"

        def corners_bg_of(first_frame):
            cs = 30
            c = np.concatenate([first_frame[:cs, :cs].reshape(-1, 3), first_frame[:cs, -cs:].reshape(-1, 3),
                                first_frame[-cs:, :cs].reshape(-1, 3), first_frame[-cs:, -cs:].reshape(-1, 3)])
            return np.median(c, axis=0).astype(np.float32)

        def grab(path, idx):
            d = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path),
                                "-vf", f"select=eq(n\\,{idx})", "-frames:v", "1",
                                "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                               capture_output=True).stdout
            return np.frombuffer(d, np.uint8).reshape(H, W, 3).astype(np.float32)

        # 断点续跑：临时目录里已有完整成品 → 跳过抠像直接走验收+交付
        if tmp_rgb.exists() and tmp_a.exists() \
                and probe_nframes(str(tmp_rgb)) == n and probe_nframes(str(tmp_a)) == n:
            print(f"[{state}] 发现完整临时成品，跳过抠像", flush=True)
            bg = corners_bg_of(grab(tmp_rgb, 0))
        else:
            print(f"[{state}] {n} 帧，开始处理…", flush=True)
            p, read_frame = decode_pipe(src)
            head = read_frame()
            bg = corners_bg_of(head)   # 底色：首帧四角中位数（bt709 解读空间）
            print(f"[{state}] bg RGB = {bg.round(1).tolist()}", flush=True)

            head_a = (remove(head, session=session)[..., 3].astype(np.float32) / 255.0)
            head_a = clean_alpha(head_a, head.astype(np.float32), bg)

            rgb_enc = encoder(RGB_ENC, tmp_rgb, TMP / f"enc_{state}_rgb.err")
            a_enc = subprocess.Popen(
                ["ffmpeg", "-y", "-v", "warning", "-f", "rawvideo", "-pix_fmt", "gray",
                 "-s", f"{W}x{H}", "-r", "24", "-i", "-"] + MASK_ENC + [str(tmp_a)],
                stdin=subprocess.PIPE, stderr=open(TMP / f"enc_{state}_a.err", "w", encoding="utf-8"))

            def write_pair(rgb, alpha):
                rgb_enc.stdin.write(np.clip(rgb, 0, 255).astype(np.uint8).tobytes())
                a_enc.stdin.write(a_to_u8(alpha).tobytes())

            write_pair(head, head_a)   # 第 0 帧 = head
            i = 1
            while True:
                fr = read_frame()
                if fr is None:
                    break
                alpha = (remove(fr, session=session)[..., 3].astype(np.float32) / 255.0)
                alpha = clean_alpha(alpha, fr.astype(np.float32), bg)
                if i >= n - K_SEAM:   # 尾部向 head 交叉淡化（观测空间混合，RGB/alpha 等价一致）
                    t = (i - (n - K_SEAM) + 1) / (K_SEAM + 1)
                    fr = fr.astype(np.float32) * (1 - t) + head.astype(np.float32) * t
                    alpha = alpha * (1 - t) + head_a * t
                write_pair(fr, alpha)
                i += 1
                if i % 60 == 0:
                    rate = (time.time() - t0) / i
                    print(f"[{state}] {i}/{n}  预计剩余 {rate * (n - i) / 60:.1f} min", flush=True)
            p.wait()
            rgb_enc.stdin.close(); rgb_enc.wait()
            a_enc.stdin.close(); a_enc.wait()
            print(f"[{state}] 编码完成 {time.time() - t0:.0f}s，自检…", flush=True)

        # ── 自检：解码 rgb+mask 第 90 帧复验 ──
        def grab(path, idx):
            d = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path),
                                "-vf", f"select=eq(n\\,{idx})", "-frames:v", "1",
                                "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                               capture_output=True).stdout
            return np.frombuffer(d, np.uint8).reshape(H, W, 3).astype(np.float32)

        rgb90 = grab(tmp_rgb, 90)
        g90 = grab(tmp_a, 90)[..., 0]
        a90 = np.clip((g90 - 16.0) / 219.0, 0, 1)
        corner_a = max(a90[:8, :8].max(), a90[:8, -8:].max(), a90[-8:, :8].max(), a90[-8:, -8:].max())
        body_a = a90[H // 2 - 10:H // 2 + 10, W // 2 - 10:W // 2 + 10].mean()
        print(f"[{state}] 角落 alpha={corner_a:.3f}（应≈0）  中心 alpha={body_a:.2f}（应≈1）", flush=True)

        # QC 合成图（棋盘/白/暗底）
        a3 = a90[..., None]
        fin = np.clip((rgb90 - bg * (1 - a3)) / np.maximum(a3, 0.02), 0, 255)
        yy, xx = np.mgrid[0:H, 0:W]
        chk2 = ((((yy // 40) + (xx // 40)) % 2) * 85 + 170).astype(np.float32)
        chk = np.repeat(chk2[..., None], 3, axis=-1)          # (H,W,3)
        import cv2
        for nm, bgd in (("checker", chk), ("white", np.full_like(chk, 255.0)),
                        ("dark", np.full_like(chk, 26.0))):
            comp = (fin * a3 + bgd * (1 - a3)).astype(np.uint8)
            ok = cv2.imwrite(str(TMP / f"qc_{state}_{nm}.png"), comp[..., ::-1])
            if not ok:
                print("QC imwrite FAILED", state, nm, flush=True)

        for suffix in ("rgb", "a"):
            shutil.move(str(TMP / f"fish_{state}_{suffix}.mp4"), str(OUTDIR / f"fish_{state}_{suffix}.mp4"))
        manifest[state] = {"rgb": f"/avatar/fish_{state}_rgb.mp4",
                           "alpha": f"/avatar/fish_{state}_a.mp4",
                           "bg": [round(float(c) / 255.0, 4) for c in bg]}
        print(f"[{state}] [OK] 完成，用时 {time.time() - t0:.0f}s", flush=True)

    # 三个状态全部交付后写 manifest（供 stage/后续系统读取底色等元数据）
    if all((OUTDIR / f"fish_{s}_rgb.mp4").exists() and (OUTDIR / f"fish_{s}_a.mp4").exists()
           for s, _ in STATES):
        (OUTDIR / "fish_manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        print("manifest →", OUTDIR / "fish_manifest.json", flush=True)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        print("PIPELINE FAILED", flush=True)
        sys.exit(1)
