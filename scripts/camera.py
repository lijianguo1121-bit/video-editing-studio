#!/usr/bin/env python3
"""运镜（放大 / 缩小）+ 圆圈画中画（主画面缩成小圆圈）。

一条统一的描述：**画面按 k(t) 缩放，原片上的锚点 (ax, ay) 落在屏幕的
(cx(t), cy(t))**。k > 1 是推近，k < 1 是把主画面缩小，缩小到一定程度
再配一张 B-roll 打底，就成了"讲一会儿主画面切成一个小圆圈"。

两侧各自实现同一份关键帧：

* ffmpeg 侧 —— `scale` 的 w/h 用 `eval=frame` 逐帧求值（唯一支持时间变量的
  **改尺寸** 手段；`pad` 的 x/y 不接受 `t`，所以位置交给 `overlay`，它的
  x/y 恰好支持 `t`）。整条链走 yuv420p 且尺寸/偏移全部取偶，色度不重采样。
* 浏览器侧 —— 用同一份 segs 算出 k(t)/圆心，在 B-roll 图上按圆心 + 半径
  挖一个洞，主画面就从洞里透出来，洞外是 B-roll。圆洞半径恒 ≤ 缩略画面的
  内切圆半径，所以永远不会露出 ffmpeg 那层黑底。
"""

from pathlib import Path
import json
import subprocess

import paths

W, H = paths.OUT_W, paths.OUT_H
FPS = paths.FPS

# 锚点兜底位置：竖屏口播人脸基本在画面中上部
ANCHOR_FALLBACK = (round(W * 0.5), round(H * 0.31))

PIP_LEAD = 0.35          # 圆圈出现前后的"收边"时长（B-roll 淡入 / 淡出）
PIP_TR = 0.55            # 主画面缩成圆圈的时长
PIP_R = 150              # 圆圈半径（720x1280 画布）
PIP_REGION = 600         # 圆圈里露出原片的宽度（越小脸越大；600 ≈ 头 + 肩，留足余量）

ZOOM_AMPLITUDE = 0.075   # 推近幅度
ZOOM_TIMES = 1           # 整片做几次「推近 → 拉回」。圆圈本身已经在缩放，默认就一次
ZOOM_MIN_SPAN = 2.5      # 一段区间短于这个秒数就不在里面做推拉了（来不及，反而像抖）
ZOOM_SAME_SPAN = 1.0     # 区间长度差在这个秒数以内算"差不多长"，此时优先靠前的
PIP_BUDGET = 0.45        # auto 模式下，圆圈总时长最多占全片的比例
PIP_OPEN_SKIP = 1.5      # 片头这么多秒内不做圆圈（先让人物出镜）


# ------------------------------------------------------------------ 小工具
def _ease_expr(p, kind):
    """把归一化进度 p 包成带缓动的表达式。"""
    if kind == "smooth":
        return "((%s)*(%s)*(3-2*(%s)))" % (p, p, p)
    return "(%s)" % p


def _progress_expr(seg):
    return "max(0,min((t-%.4f)/%.4f,1))" % (seg["t0"], max(1e-6, seg["t1"] - seg["t0"]))


def _lerp_expr(seg, f0, f1):
    p = _progress_expr(seg)
    e = _ease_expr(p, seg.get("ease", "linear"))
    return "(%.4f+(%.4f-%.4f)*%s)" % (seg[f0], seg[f1], seg[f0], e)


def _piecewise(segs, f0, f1):
    """seg 列表 -> 一个逐帧求值的表达式（末尾兜底取最后一段）。"""
    out = _lerp_expr(segs[-1], f0, f1)
    for s in reversed(segs[:-1]):
        out = "if(lt(t,%.4f),%s,%s)" % (s["t1"], _lerp_expr(s, f0, f1), out)
    return out


# ------------------------------------------------------------------ 人脸锚点
def estimate_anchor(base_mp4, t=1.0, log=None):
    """估人脸在画面中的位置，作为运镜/缩圈的锚点。

    用肤色在 YCbCr 上的稳定区间做掩膜，只在上半部找，并给"中上"一个高斯先验。
    纯 numpy，无模型依赖；肤色占比太低（不是人像）就退回默认位置。
    """
    try:
        import numpy as np
    except Exception:                                        # noqa: BLE001
        return ANCHOR_FALLBACK

    sw, sh = 90, 160
    acc = None
    for tt in (t, t + 1.2):
        p = subprocess.run(
            [paths.ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", "%.2f" % tt,
             "-i", str(base_mp4), "-frames:v", "1",
             "-vf", "scale=%d:%d" % (sw, sh), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True)
        if p.returncode != 0 or len(p.stdout) < sw * sh * 3:
            continue
        a = np.frombuffer(p.stdout, dtype="u1")[:sw * sh * 3]
        a = a.reshape(sh, sw, 3).astype("f4")
        r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
        yy = 0.299 * r + 0.587 * g + 0.114 * b
        cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b
        cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b
        m = (cb > 77) & (cb < 130) & (cr > 132) & (cr < 176) & (yy > 55)
        m[int(sh * 0.72):, :] = False                        # 只看上半部
        acc = m.astype("f4") if acc is None else acc + m

    if acc is None or acc.sum() < sw * sh * 0.012:
        return ANCHOR_FALLBACK

    gy, gx = np.mgrid[0:sh, 0:sw]
    prob = acc * np.exp(-(((gx - sw * 0.5) / (sw * 0.34)) ** 2
                          + ((gy - sh * 0.32) / (sh * 0.30)) ** 2))
    if prob.sum() <= 0:
        return ANCHOR_FALLBACK
    cx = float((prob * gx).sum() / prob.sum()) / sw * W
    cy = float((prob * gy).sum() / prob.sum()) / sh * H
    # 锚点必须是偶数，才和 yuv420p 的色度对齐
    return (int(round(cx / 2) * 2), int(round(cy / 2) * 2))


# ------------------------------------------------------------------ 计划
def _pip_center(style_cfg, r):
    """圆圈落在哪：复用该套模板算好的配图落点（那里既躲开标题也躲开人脸），
    取卡片中心作为圆心，再夹回画面内。"""
    per = (style_cfg or {}).get("imageLayer") or {}
    xy = per.get("xy")
    if not (isinstance(xy, (list, tuple)) and len(xy) == 2):
        xy = (round(W * 0.02), round(H * 0.02))
    cx = float(xy[0]) + 84.0
    cy = float(xy[1]) + 112.0
    m = r + 16
    cx = min(max(cx, m), W - m)
    cy = min(max(cy, m), H - m)
    return (int(round(cx / 2) * 2), int(round(cy / 2) * 2))


def _beat_at(beats, t):
    """t 落在哪个节拍里 -> (这个节拍有多长, 它从第几秒开始)。"""
    for b in beats:
        if b["t0"] - 1e-6 <= t < b["t1"] + 1e-6:
            return float(b["t1"]) - float(b["t0"]), float(b["t0"])
    return 0.0, 0.0


def build_plan(duration, beats, images, anchor, style_cfg=None, opts=None):
    """产出这一条视频的运镜计划（关键帧 + 圆圈段）。

    opts:
      pip   : auto | off | all     哪些配图节拍做成圆圈画中画
      zoom  : 推近幅度，0 表示不要运镜
      zooms : 整片做几次「推近 → 拉回」，默认 1 次
      pipR  : 圆圈半径
    """
    opts = opts or {}
    mode = (opts.get("pip") or "auto").lower()
    amp = float(opts.get("zoom", ZOOM_AMPLITUDE))
    zooms = int(opts.get("zooms", ZOOM_TIMES))
    r = int(opts.get("pipR", PIP_R))
    ax, ay = anchor

    # 哪几张配图做圆圈：auto 时隔一个做一次；外加两条纪律——
    #   1) 片头先让本尊出镜，开场那一拍不做圆圈；
    #   2) 圆圈总时长不超过全片的 PIP_BUDGET，短片子也不会"整片都在圆圈里"。
    picks = []
    if mode != "off" and images:
        if mode == "all":
            picks = list(range(len(images)))
        else:
            budget = duration * PIP_BUDGET
            used = 0.0
            for n in range(0, len(images), 2):
                span, t0 = _beat_at(beats, float(images[n]["t"]))
                if t0 < PIP_OPEN_SKIP:
                    continue
                if picks and used + span > budget:
                    break
                picks.append(n)
                used += span

    # 配图起始时间 -> beat 下标
    def beat_of(t):
        for i, b in enumerate(beats):
            if b["t0"] - 1e-6 <= t < b["t1"] + 1e-6:
                return i
        return max(0, len(beats) - 1)

    pip_by_beat = {}
    for n in picks:
        it = images[n]
        bi = beat_of(float(it["t"]))
        if bi in pip_by_beat:
            continue
        b = beats[bi]
        # 圆圈里露出的原片宽度 PIP_REGION，k = 直径 / 区域宽度
        kp = max(0.45, min(0.98, (r * 2.0) / float(PIP_REGION)))
        cx, cy = _pip_center(style_cfg, r)
        pip_by_beat[bi] = {"n": n, "cx": cx, "cy": cy, "r": r, "k": kp,
                           "item": it, "t0": b["t0"], "t1": b["t1"]}

    segs = []
    k, x, y = 1.0, ax, ay

    # ---- 运镜节奏：整片只做极少次「推近 → 拉回」（默认就一次）------------------
    # 每一次都放在最长的一段"没有圆圈"的连续区间里，对半切开：前半推近、后半拉回。
    # 圆圈本身已经在放大缩小了，再叠加高频推拉会让人晕。
    runs, cur = [], []
    for bi, b in enumerate(beats):
        if float(b["t1"]) - float(b["t0"]) < 0.5:
            continue
        if bi in pip_by_beat:
            if cur:
                runs.append(cur)
                cur = []
        else:
            cur.append(bi)
    if cur:
        runs.append(cur)

    def _run_span(run):
        return float(beats[run[0]]["t0"]), float(beats[run[-1]]["t1"])

    zoom_runs = {}
    if amp > 0.001 and zooms > 0:
        usable = [r for r in runs if _run_span(r)[1] - _run_span(r)[0] >= ZOOM_MIN_SPAN]
        # 挑最长的几段；长度差不多时优先靠前的——推拉放在片尾会显得突兀
        usable.sort(key=lambda r: (_run_span(r)[1] - _run_span(r)[0]) / ZOOM_SAME_SPAN,
                    reverse=True)
        if usable:
            best_span = _run_span(usable[0])[1] - _run_span(usable[0])[0]
            near = [r for r in usable
                    if _run_span(r)[1] - _run_span(r)[0] >= best_span - ZOOM_SAME_SPAN]
            near.sort(key=lambda r: _run_span(r)[0])
            for run in near[:zooms]:
                zoom_runs[run[0]] = (_run_span(run), run[-1])

    for bi, b in enumerate(beats):
        t0, t1 = float(b["t0"]), float(b["t1"])
        if t1 - t0 < 0.5:
            continue
        if bi in zoom_runs:                          # 这一段整段做一次推近 + 拉回
            (ta, tb), last = zoom_runs[bi]
            tm = (ta + tb) / 2.0
            segs.append({"t0": ta, "t1": tm, "k0": k, "k1": 1.0 + amp,
                         "x0": x, "y0": y, "x1": ax, "y1": ay, "ease": "smooth"})
            segs.append({"t0": tm, "t1": tb, "k0": 1.0 + amp, "k1": 1.0,
                         "x0": ax, "y0": ay, "x1": ax, "y1": ay, "ease": "smooth"})
            k, x, y = 1.0, ax, ay
            continue
        if any(bi <= last and bi >= start for start, (_, last) in zoom_runs.items()):
            continue                                 # 这段的余下节拍已经被合并处理了
        pip = pip_by_beat.get(bi)
        if pip:
            tr = min(PIP_TR, max(0.3, (t1 - t0) / 3.0))
            kp, px, py = pip["k"], pip["cx"], pip["cy"]
            segs.append({"t0": t0, "t1": t0 + tr, "k0": k, "k1": kp,
                         "x0": x, "y0": y, "x1": px, "y1": py, "ease": "smooth"})
            kk = kp
            if (t1 - tr) - (t0 + tr) > 0.25:                 # 圆圈里缓慢缓推
                kk = kp * 1.04
                segs.append({"t0": t0 + tr, "t1": t1 - tr, "k0": kp, "k1": kk,
                             "x0": px, "y0": py, "x1": px, "y1": py, "ease": "linear"})
            segs.append({"t0": t1 - tr, "t1": t1, "k0": kk, "k1": 1.0,
                         "x0": px, "y0": py, "x1": ax, "y1": ay, "ease": "smooth"})
            pip["tr"] = tr
            k, x, y = 1.0, ax, ay
        else:
            segs.append({"t0": t0, "t1": t1, "k0": k, "k1": k,       # 保持不动
                         "x0": x, "y0": y, "x1": x, "y1": y, "ease": "linear"})

    if not segs:
        segs = [{"t0": 0.0, "t1": duration, "k0": 1.0, "k1": 1.0,
                 "x0": ax, "y0": ay, "x1": ax, "y1": ay, "ease": "linear"}]

    pips = []
    for bi in sorted(pip_by_beat):
        p = pip_by_beat[bi]
        pips.append({
            "i": p["n"] + 1, "t0": round(p["t0"], 3), "t1": round(p["t1"], 3),
            "tr": round(p["tr"], 3), "lead": PIP_LEAD,
            "cx": p["cx"], "cy": p["cy"], "r": p["r"], "k": round(p["k"], 4),
            "image": p["item"].get("file") or "",
            "title": p["item"].get("title") or "", "sub": p["item"].get("subject") or "",
        })

    # 内切圆半径：缩略画面里能放下的最大圆，圆洞不超过它就不会露黑底
    inset = min(ax, W - ax, ay, H - ay)
    return {"anchor": [ax, ay], "segs": segs, "pips": pips,
            "inset": inset, "duration": round(float(duration), 3),
            "kmax": max(1.0, max(s["k0"] for s in segs), max(s["k1"] for s in segs))}


# ------------------------------------------------------------------ ffmpeg 侧
def filter_chain(plan, grade="", fade_pad=True):
    """产出把原片变成"运镜后画面"的滤镜链（yuv420p，尺寸/偏移全取偶）。"""
    segs = plan["segs"]
    ax, ay = plan["anchor"]
    ke = _piecewise(segs, "k0", "k1")
    xe = _piecewise(segs, "x0", "x1")
    ye = _piecewise(segs, "y0", "y1")
    smax = plan["kmax"]
    cw = max(W, int(W * smax / 2) * 2 + 2)
    ch = max(H, int(H * smax / 2) * 2 + 2)

    steps = []
    if grade:
        steps.append(grade)
    steps.append("scale=w='trunc(%d*(%s)/2)*2':h='trunc(%d*(%s)/2)*2':eval=frame"
                 % (W, ke, H, ke))
    steps.append("pad=%d:%d:0:0:color=black" % (cw, ch))
    # overlay 的偏移 = 锚点该落在哪 - 锚点在缩放后画面里的位置；取偶以对齐色度
    xe2 = "2*floor(((%s)-%d*(%s))/2)" % (xe, ax, ke)
    ye2 = "2*floor(((%s)-%d*(%s))/2)" % (ye, ay, ke)
    return {"vids": ",".join(steps), "cw": cw, "ch": ch,
            "xe": xe2, "ye": ye2, "k": ke, "x": xe, "y": ye}


def mux_chain(cam, dur):
    """把 [0:v] 变成最终画面：黑色画布 + 运镜后的原片 + 动效层。"""
    return (
        "color=c=black:s=%dx%d:r=%d:d=%.3f[cv];"
        "[0:v]%s[vids];"
        "[cv][vids]overlay=x='%s':y='%s':format=auto[cam];"
        "[1:v]format=rgba[ov];"
        "[cam][ov]overlay=0:0:eof_action=pass:format=auto,format=yuv420p[vout]"
        % (W, H, FPS, dur, cam["vids"], cam["xe"], cam["ye"])
    )


# ------------------------------------------------------------------ 浏览器侧
def js_plan(plan):
    """给渲染页的那一份（结构一致，浏览器用同一套插值）。"""
    return {"anchor": plan["anchor"], "segs": plan["segs"],
            "pips": plan["pips"], "inset": plan["inset"]}


def describe(plan):
    lines = ["锚点 (人脸) = %s" % (plan["anchor"],),
             "运镜段 %d，k 范围 1.00 - %.3f" % (len(plan["segs"]), plan["kmax"])]
    for p in plan["pips"]:
        lines.append("  圆圈 #%d  %05.2f-%05.2fs  圆心 (%d,%d) r=%d  B-roll=%s"
                     % (p["i"], p["t0"], p["t1"], p["cx"], p["cy"], p["r"], p["image"]))
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--duration", type=float, default=20.0)
    ap.add_argument("--beats", default="")
    a = ap.parse_args()
    if a.beats:
        beats = json.loads(Path(a.beats).read_text(encoding="utf-8"))
    else:
        n = max(2, int(a.duration / 4.0))
        beats = [{"t0": round(a.duration * i / n, 3), "t1": round(a.duration * (i + 1) / n, 3)}
                 for i in range(n)]
    for b in beats:
        b.setdefault("title", "")
    anchor = estimate_anchor(Path(a.input))
    imgs = [{"idx": i + 1, "t": (i + 0.5) * a.duration / 3.0,
             "file": "images/demo_%02d.jpg" % (i + 1), "title": "示例", "subject": "示例"}
            for i in range(3)]
    plan = build_plan(a.duration, beats, imgs, anchor)
    print(describe(plan))
    chain = filter_chain(plan, "eq=contrast=1.05")
    print("画布 %dx%d" % (chain["cw"], chain["ch"]))
    print("k  =", chain["k"][:220], "...")
