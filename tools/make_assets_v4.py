# -*- coding: utf-8 -*-
"""抠像管线 v4：混合键控（生产级 keyer）。

为什么 v3（纯 AI matte）到不了完美：
  - isnet 内部 1024² 上采样回 720 → 边缘亚像素信息丢失 → 毛刺
  - 逐帧独立推理 → 边缘置信度逐帧晃动 → 视频里"边缘沸腾"
  - 低对比内部细节（肤色阴影/手指/高光）被误判 → 抠烂
  - 它不利用「底色是纯色」这个最强先验

v4 = AI 形状先验 + 颜色距离精确边缘（专业合成院的 garbage matte + key 组合）：
  - bg_map：边框环采样 + 二次多项式鲁棒拟合（中位数重拟合抗角色干扰）→ 逐像素底色
  - key_alpha = smoothstep(色距, T1, T2)：亚像素平滑边缘，发梢/鱼鳍自然渐隐
  - core  = fill_holes(ai>0.9) 腐蚀1px：内部强制实体（肉绝不进 key 的歧义区）
  - zone  = fill_holes(ai>0.25) 膨胀4px：角色邻域；zone 外 alpha=0（底色雾一刀切净）
  - 3×3 高斯抗锯齿后量化
其余约定（bt709 全链路 / 遮罩限位 16..235 / 首尾 24 帧交叉淡化）与 v2/v3 相同。

RGB 轨存储革命（v4 关键）：不再存「观测帧」，存「预乘前景」P = fin×a = obs − bg_map×(1−a)：
  - 逆合成在管线内用逐像素底色板完成（shader 只有一个常数底色，±10 渐变
    在半透明边缘被 ÷a 放大 → 白底可见粉色残边；管线内无此问题）
  - shader 端底色 uniform 置 0：uncomposite(P, a, 0) = P/a = fin，预乘回写严格还原
  - 数值稳健：P 对低 alpha 无除法放大；交叉淡化在预乘域线性混合，合成学严格一致
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
from scipy.ndimage import binary_dilation, binary_erosion, binary_fill_holes
from scipy.ndimage import label as cc_label

import faulthandler
faulthandler.dump_traceback_later(180, repeat=True)

PROJECT = Path(__file__).resolve().parent.parent
TMP = Path(r"E:\qc_tmp\assets_v4")
TMP.mkdir(parents=True, exist_ok=True)
OUTDIR = PROJECT / "characters" / "avatar"

STATES = [
    ("idle", r"C:\Users\阿拉贡\Downloads\Live2D拆件：鲸鱼尾巴.mp4"),
    ("laugh", r"C:\Users\阿拉贡\Downloads\Live2D拆件：鲸鱼尾巴22.mp4"),
    ("talk", r"C:\Users\阿拉贡\Downloads\Live2D拆件：鲸鱼尾巴33.mp4"),
]
KEY_T = {"idle": (14.0, 62.0), "laugh": (14.0, 62.0), "talk": (11.0, 54.0)}
K_SEAM = 24
W = H = 720
MODEL = "isnet-general-use"


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
        return -1   # 文件损坏/不可读 → 视为不存在，触发重处理


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


def encoder(args_extra, out_path, errlog):
    return subprocess.Popen(
        ["ffmpeg", "-y", "-v", "warning", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", "24", "-i", "-"] + args_extra + [str(out_path)],
        stdin=subprocess.PIPE, stderr=open(errlog, "w", encoding="utf-8"))


def encode_gray_pipe(frames, out_path):
    p = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "warning", "-f", "rawvideo", "-pix_fmt", "gray",
         "-s", f"{W}x{H}", "-r", "24", "-i", "-"] + MASK_ENC + [str(out_path)],
        stdin=subprocess.PIPE, stderr=open(TMP / "probe.err", "w", encoding="utf-8"))
    for a in frames:
        p.stdin.write(a_to_u8(a).tobytes())
    p.stdin.close()
    return p.wait()


def fit_bg_plane(frames_rgb):
    """边框环（排除角色可能出没的底部）采样 → 二次多项式鲁棒拟合 → 逐像素底色图。"""
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    ring = np.zeros((H, W), bool)
    m = 22
    ring[:m, :] = True; ring[-160:, :] = False   # 底部 160px 不采样（角色脚部区）
    ring[:, :m] = True; ring[:, -m:] = True
    ys, xs = yy[ring], xx[ring]
    A = np.stack([np.ones_like(ys), xs, ys, xs * xs, xs * ys, ys * ys], 1)
    med = np.median(np.stack([f[ring] for f in frames_rgb]), axis=0)   # 多帧中位数抗噪
    coef = np.zeros((6, 3), np.float32)
    for _ in range(2):     # 两轮：拟合 → 剔除 >3σ 离群（角色残留）→ 重拟合
        coef, *_ = np.linalg.lstsq(A, med, rcond=None)
        resid = np.linalg.norm(med - A @ coef, axis=1)
        keep = resid < max(3.0, np.percentile(resid, 97))
        A, med, ys, xs = A[keep], med[keep], ys[keep], xs[keep]
    plane = (coef[0][None, None] + coef[1][None, None] * xx[..., None] + coef[2][None, None] * yy[..., None]
             + coef[3][None, None] * (xx * xx)[..., None] + coef[4][None, None] * (xx * yy)[..., None]
             + coef[5][None, None] * (yy * yy)[..., None])
    return np.clip(plane, 0, 255).astype(np.float32)


def hyb_alpha(ai, rgb_f, bg_map, t1, t2):
    """AI 形状先验 + 颜色键精确边缘 + 色相角判别。ai: float 0..1 (H,W)

    v4.8 关键判别 —— 「半透明角色内容」vs「底色增亮光晕」：
      尾根在源渲染里就是半透明蓝（ai 0.1-0.4、色相偏离底色 ~90°）；
      bloom 光晕是底色同色相增亮（ai≈0.02、色相差≈5°）。
      单看 ai 会把尾巴一起杀掉 → 双门控取大：
        gate = max(ai门控, 色相角门控)   光晕两者皆低 → 归零；尾巴色相门控=1 → key 全额
    封闭袋逐块鉴别：光晕袋（同色相高饱和）压 20% 残辉；
      肉体低谷袋（色相偏离/低饱和 + 高色距）补满实；其余保持软过渡。
    """
    import cv2 as _cv
    dist = np.sqrt(((rgb_f - bg_map) ** 2).sum(-1))
    key = smoothstep(dist, t1, t2)
    hsv_f = _cv.cvtColor(np.clip(rgb_f, 0, 255).astype(np.uint8)[..., ::-1], _cv.COLOR_BGR2HSV).astype(np.float32)
    hsv_b = _cv.cvtColor(np.clip(bg_map, 0, 255).astype(np.uint8)[..., ::-1], _cv.COLOR_BGR2HSV).astype(np.float32)
    dhue = np.abs(hsv_f[..., 0] - hsv_b[..., 0])
    dhue = np.minimum(dhue, 180 - dhue)            # 逐像素色相角距离
    bg_med = np.median(bg_map.reshape(-1, 3), axis=0)
    hsv_bg = _cv.cvtColor(np.uint8([[bg_med[::-1]]]), _cv.COLOR_BGR2HSV)[0, 0]
    hue_bg, sat_bg = int(hsv_bg[0]), float(hsv_bg[1]) / 255.0

    solid = ai > 0.75
    glow_masks = []
    if solid.sum() > 500:
        filled = binary_fill_holes(solid)
        pocket = filled & ~solid
        core = solid & (dist > 25.0)
        core = binary_erosion(core, np.ones((3, 3)))
        lab, nl = cc_label(pocket)
        for k in range(1, nl + 1):
            m = lab == k
            sz = int(m.sum())
            if sz < 24 or sz > 40000:
                continue
            mc = rgb_f[m].mean(0)
            hsv = _cv.cvtColor(np.uint8([[mc[::-1]]]), _cv.COLOR_BGR2HSV)[0, 0]
            dh = min(abs(int(hsv[0]) - hue_bg), 180 - abs(int(hsv[0]) - hue_bg))
            st = float(hsv[1]) / 255.0
            if dh < 20 and st > 0.5 and sat_bg > 0.35:
                glow_masks.append(m)               # 光晕袋：残辉
            elif (dh > 30 or st < 0.45) and dist[m].mean() > 55.0 and ai[m].mean() > 0.2:
                core |= binary_erosion(m, np.ones((3, 3))) if sz > 60 else m
    else:
        filled = solid
        core = solid
    loose = ai > 0.5
    hue_zone = (dhue > 35.0) & (dist > 30.0)       # 角色色相区（半透明尾/蓝紫内容）
    zone = (binary_dilation(loose, np.ones((11, 11))) | filled | hue_zone) if loose.sum() > 500 \
        else (filled | hue_zone)
    alpha = np.where(zone, key, 0.0).astype(np.float32)
    gate_ai = smoothstep(ai, 0.15, 0.50)
    gate_hue = smoothstep(dhue, 25.0, 55.0)
    alpha *= np.maximum(gate_ai, gate_hue)
    # 逐像素光晕抑制：底色同色相(±14°)+高饱和 = 透底的品红/亮底雾，
    # 薄鳍/发梢边缘的粉色镶边主要来源；核心区之外统一压到 25%
    glow_px = (dhue < 14.0) & (hsv_f[..., 1] / 255.0 > 0.55)
    alpha[glow_px & ~core] *= 0.25
    for gm in glow_masks:
        alpha[gm] *= 0.2                           # 光晕袋残辉
    alpha[core] = 1.0
    # 3×3 高斯抗锯齿（消 8-bit 毛刺），核心区保持满实
    k = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], np.float32) / 16.0
    from scipy.ndimage import convolve
    alpha = convolve(alpha, k, mode="nearest")
    alpha[core] = 1.0
    return np.clip(alpha, 0, 1)


def main():
    only = sys.argv[1:] or None
    session = new_session(MODEL)
    manifest = {}

    # 遮罩轨量程自检
    probes = [0.0, 0.25, 0.5, 0.75, 1.0]
    encode_gray_pipe([np.full((H, W), p, np.float32) for p in probes], TMP / "range_probe.mp4")
    d = subprocess.run(["ffmpeg", "-v", "error", "-i", str(TMP / "range_probe.mp4"), "-f", "rawvideo",
                        "-pix_fmt", "gray", "-"], capture_output=True).stdout
    vals = np.frombuffer(d, np.uint8).reshape(len(probes), H, W)[:, 0, 0]
    expand = (vals.astype(np.float32) - 16.0) / 219.0
    err = np.abs(expand - np.array(probes)).max()
    print(f"[range probe] encoded={vals.tolist()} max_err={err:.4f}", flush=True)
    assert err < 0.02, "遮罩轨量程往返误差过大"

    for state, src in STATES:
        if only and state not in only:
            continue
        t0 = time.time()
        n = probe_nframes(src)
        tmp_rgb, tmp_a = TMP / f"fish_{state}_rgb.mp4", TMP / f"fish_{state}_a.mp4"

        def grab(path, idx):
            dd = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path),
                                 "-vf", f"select=eq(n\\,{idx})", "-frames:v", "1",
                                 "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                                capture_output=True).stdout
            return np.frombuffer(dd, np.uint8).reshape(H, W, 3).astype(np.float32)

        if tmp_rgb.exists() and tmp_a.exists() \
                and probe_nframes(str(tmp_rgb)) == n and probe_nframes(str(tmp_a)) == n:
            print(f"[{state}] 临时成品已存在，跳过", flush=True)
        else:
            print(f"[{state}] {n} 帧，混合键控处理…", flush=True)
            p, read_frame = decode_pipe(src)
            head = read_frame()
            # 底色板：首帧 + 中帧 + 尾帧 环采样拟合
            mid = grab(src, n // 2)
            tail = grab(src, n - 1)
            bg_map = fit_bg_plane([head, mid, tail])
            bg_med = np.median(bg_map.reshape(-1, 3), axis=0)
            print(f"[{state}] bg_median={bg_med.round(1).tolist()} plane_spread="
                  f"{(bg_map.max((0, 1)) - bg_map.min((0, 1))).round(1).tolist()}", flush=True)
            t1, t2 = KEY_T[state]

            head_ai = remove(head, session=session)[..., 3].astype(np.float32) / 255.0
            head_a = hyb_alpha(head_ai, head.astype(np.float32), bg_map, t1, t2)

            rgb_enc = encoder(RGB_ENC, tmp_rgb, TMP / f"enc_{state}_rgb.err")
            a_enc = subprocess.Popen(
                ["ffmpeg", "-y", "-v", "warning", "-f", "rawvideo", "-pix_fmt", "gray",
                 "-s", f"{W}x{H}", "-r", "24", "-i", "-"] + MASK_ENC + [str(tmp_a)],
                stdin=subprocess.PIPE, stderr=open(TMP / f"enc_{state}_a.err", "w", encoding="utf-8"))

            def write_pair(rgb, alpha):
                # 预乘前景：P = obs − bg_map×(1−a)（= fin×a，无需除法，数值稳健）
                a3 = alpha[..., None]
                prem = np.clip(rgb.astype(np.float32) - bg_map * (1.0 - a3), 0, 255)
                prem = np.where(a3 > 0.004, prem, 0.0)
                rgb_enc.stdin.write(prem.astype(np.uint8).tobytes())
                a_enc.stdin.write(a_to_u8(alpha).tobytes())

            write_pair(head, head_a)
            i = 1
            while True:
                fr = read_frame()
                if fr is None:
                    break
                ai = remove(fr, session=session)[..., 3].astype(np.float32) / 255.0
                alpha = hyb_alpha(ai, fr.astype(np.float32), bg_map, t1, t2)
                if i >= n - K_SEAM:
                    t = (i - (n - K_SEAM) + 1) / (K_SEAM + 1)
                    fr = fr.astype(np.float32) * (1 - t) + head.astype(np.float32) * t
                    alpha = alpha * (1 - t) + head_a * t
                write_pair(fr, alpha)
                i += 1
                if i % 60 == 0:
                    rate = (time.time() - t0) / i
                    print(f"[{state}] {i}/{n}  剩余 {rate * (n - i) / 60:.1f} min", flush=True)
            p.wait()
            rgb_enc.stdin.close(); rgb_enc.wait()
            a_enc.stdin.close(); a_enc.wait()
            print(f"[{state}] 编码完成 {time.time() - t0:.0f}s", flush=True)

        # 自检 + QC（RGB 轨已是预乘前景：合成 = P + 底×(1−a)，无需逆推）
        rgb90 = grab(tmp_rgb, 90)
        g90 = grab(tmp_a, 90)[..., 0]
        a90 = np.clip((g90 - 16.0) / 219.0, 0, 1)
        corner_a = max(a90[:8, :8].max(), a90[:8, -8:].max(), a90[-8:, :8].max(), a90[-8:, -8:].max())
        body_a = a90[H // 2 - 10:H // 2 + 10, W // 2 - 10:W // 2 + 10].mean()
        from scipy.ndimage import binary_fill_holes
        solid = a90 > 0.85
        holes = int((binary_fill_holes(solid) & ~solid).sum()) if solid.sum() > 1000 else 0
        print(f"[{state}] 角落alpha={corner_a:.3f} 中心alpha={body_a:.2f} 内部空洞={holes}", flush=True)

        a3 = a90[..., None]
        yy, xx = np.mgrid[0:H, 0:W]
        chk2 = ((((yy // 40) + (xx // 40)) % 2) * 85 + 170).astype(np.float32)
        chk = np.repeat(chk2[..., None], 3, axis=-1)
        import cv2
        for nm, bgd in (("checker", chk), ("white", np.full_like(chk, 255.0)),
                        ("dark", np.full_like(chk, 26.0))):
            comp = np.clip(rgb90 * a3 + bgd * (1 - a3), 0, 255)
            cv2.imwrite(str(TMP / f"qc_{state}_{nm}.png"), comp.astype(np.uint8)[..., ::-1])

        for suffix in ("rgb", "a"):
            shutil.move(str(tmp_rgb if suffix == "rgb" else tmp_a), str(OUTDIR / f"fish_{state}_{suffix}.mp4"))
        manifest[state] = {"rgb": f"/avatar/fish_{state}_rgb.mp4",
                           "alpha": f"/avatar/fish_{state}_a.mp4",
                           "storage": "premultiplied", "bg": [0.0, 0.0, 0.0]}
        print(f"[{state}] [OK] {time.time() - t0:.0f}s", flush=True)

    if all((OUTDIR / f"fish_{s}_rgb.mp4").exists() for s, _ in STATES):
        (OUTDIR / "fish_manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        print("manifest updated", flush=True)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        print("PIPELINE FAILED", flush=True)
        sys.exit(1)
