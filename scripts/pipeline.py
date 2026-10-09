#!/usr/bin/env python3
"""一键流水线（One-shot pipeline）。

    上传的视频  →  探测  →  自动分节拍  →  配图  →  配乐
                →  逐帧渲染透明动效  →  合成原声+配乐+配图+调色  →  原声保留质检  →  成片

这条链路的关键设计：
  * 口播原声永远是主轨，配乐由 FFmpeg 侧链压制后垫在下面，一个字都不会变。
  * 动效层是「透明 PNG 序列」（Playwright 逐帧导出），叠在原片之上，
    所以画质不被二次编码拖垮，配图也随动效层一起进去。
  * 每套模板 = 一段 CSS 皮肤 + 一个绘制函数，换风格只换一个 id。

命令行用法（调试/批量）：
    python3 pipeline.py --input=某视频.mp4 --styles=crt,dash --out=我的任务
    python3 pipeline.py --input=某视频.mp4 --styles=neon --script="这里是口播文案。第二句。"
"""

from pathlib import Path
import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
import time
import traceback

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import paths                                       # noqa: E402
import autobeat                                    # noqa: E402
import music_gen                                   # noqa: E402

PROJECT = paths.ASSETS                # 引擎目录（skill 的 assets/ 或旧工程根）
JOBS = paths.JOBS                     # 产物工作区
SRC_PAGE = paths.SRC_PAGE             # 20 套模板的渲染页
TPL_JSON = paths.TPL_JSON
IMG_LIB = paths.LIBRARY               # 内置配图库
FFMPEG = paths.ffmpeg()
FPS = paths.FPS
OUT_W, OUT_H = paths.OUT_W, paths.OUT_H

# ------------------------------------------------------------------ 调色
# 每套模板给一个基础影调，让动效层"坐"在画面上而不是浮在上面
GRADE = {
    "warm":  "eq=contrast=1.05:saturation=1.06:brightness=-0.004",
    "cool":  "eq=contrast=1.08:saturation=1.02:brightness=-0.010,"
             "colorbalance=bs=0.035:gs=-0.010",
    "punch": "eq=contrast=1.12:saturation=1.20:brightness=-0.010",
    "neon":  "eq=contrast=1.16:saturation=1.28:brightness=-0.016,"
             "colorbalance=bs=0.06:bm=0.03:gs=-0.02",
    "mono":  "eq=contrast=1.04:saturation=0.92:brightness=-0.006",
}
STYLE_GRADE = {
    "crt": "warm", "retro": "mono", "dash": "cool", "report": "mono",
    "journal": "warm", "chalk": "cool", "promo": "punch", "danmu": "punch",
    "magazine": "mono", "minimal": "mono", "news": "cool", "product": "neon",
    "glass": "cool", "timeline": "cool", "quote": "warm", "podcast": "warm",
    "travel": "punch", "blueprint": "cool", "gradient": "neon", "card": "warm",
}

# 内置配图不够时，用同一张图的不同裁切/镜像凑出可区分的画面
VARIANTS = [
    {"pos": "50% 50%", "zoom": 1.00},
    {"pos": "50% 28%", "zoom": 1.30},
    {"pos": "50% 74%", "zoom": 1.36, "flip": True},
    {"pos": "34% 50%", "zoom": 1.46},
    {"pos": "66% 42%", "zoom": 1.52, "flip": True},
]


def run(args, check=True, capture=False):
    return subprocess.run(args, check=check, text=True, capture_output=capture)


# ================================================================== 进度
class Status:
    """把任务进度落盘，前端轮询 status.json 就能看到实时状态。"""

    def __init__(self, path, total_styles):
        self.path = Path(path)
        self.total = max(1, total_styles)
        self.done = 0
        self.t0 = time.time()
        self.data = {"state": "running", "step": "init", "pct": 0,
                     "message": "初始化", "styles": {}, "outputs": []}

    def write(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.path)

    def set(self, step=None, message=None, pct=None):
        if step is not None:
            self.data["step"] = step
        if message is not None:
            self.data["message"] = message
        if pct is not None:
            self.data["pct"] = int(max(0, min(100, pct)))
        self.data["elapsed"] = round(time.time() - self.t0, 1)
        self.write()

    def style(self, sid, state, pct=None, note=""):
        self.data["styles"][sid] = {"state": state, "note": note}
        if pct is not None:
            self.data["pct"] = int(max(0, min(100, pct)))
        self.data["elapsed"] = round(time.time() - self.t0, 1)
        self.write()

    def fail(self, msg):
        self.data.update({"state": "failed", "message": msg, "pct": 100})
        self.data["elapsed"] = round(time.time() - self.t0, 1)
        self.write()

    def finish(self, outputs):
        self.data.update({"state": "done", "step": "done", "pct": 100,
                          "message": "全部完成", "outputs": outputs})
        self.data["elapsed"] = round(time.time() - self.t0, 1)
        self.write()


# ================================================================== 探测
def probe(path):
    """读时长 / 分辨率 / 有没有音轨，只靠 ffmpeg 自身的输出。"""
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)],
                       capture_output=True, text=True)
    err = p.stderr or ""
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if not m:
        raise RuntimeError("无法读取视频时长，文件可能损坏：%s" % path)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    v = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", err)
    w, h = (int(v.group(1)), int(v.group(2))) if v else (OUT_W, OUT_H)
    return {"duration": round(dur, 3), "width": w, "height": h,
            "hasAudio": "Audio:" in err}


# ================================================================== 底片
def build_base(src, dst, info):
    """统一到 720x1280 / 30fps：竖屏裁切填满，横屏加模糊底衬避免裁掉画面。"""
    portrait = info["height"] >= info["width"]
    if portrait:
        vf = ("[0:v]scale=%d:%d:force_original_aspect_ratio=increase,"
              "crop=%d:%d,fps=%d,format=yuv420p[v]" % (OUT_W, OUT_H, OUT_W, OUT_H, FPS))
    else:
        vf = ("[0:v]split=2[bg][fg];"
              "[bg]scale=%d:%d:force_original_aspect_ratio=increase,"
              "crop=%d:%d,gblur=sigma=26[bgb];"
              "[fg]scale=%d:%d:force_original_aspect_ratio=decrease[fgs];"
              "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,fps=%d,format=yuv420p[v]"
              % (OUT_W, OUT_H, OUT_W, OUT_H, OUT_W, OUT_H, FPS))
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
            "-filter_complex", vf, "-map", "[v]"]
    if info["hasAudio"]:
        args += ["-map", "0:a", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    args += ["-c:v", "libx264", "-preset", "slow", "-crf", "17",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dst)]
    run(args)


# ================================================================== 配图
def prepare_images(job_dir, style, plan, log):
    """给一套模板准备配图。

    优先用任务自带的定制配图（jobs/<id>/images/<style>_NN.png，
    由 AI 或人工按 image_requests.json 生成），不够就用内置库里该套风格的图，
    再不够就用同一张图的不同裁切顶上，保证张数永远达标。
    """
    out = job_dir / "images"
    out.mkdir(parents=True, exist_ok=True)
    custom = sorted(out.glob("%s_*" % style))
    lib = [p for p in sorted(IMG_LIB.glob("%s_*" % style))
           if re.match(r"^%s_\d+\.(png|jpe?g|webp)$" % re.escape(style),
                       p.name, re.I)]

    pool = custom or lib
    source = "custom" if custom else ("lib" if lib else "none")
    if not pool:
        return [], source

    # 把选中的图搬进任务目录，让任务自包含（也统一了定制图与内置图的取用路径）
    for p in pool:
        if p.parent != out:
            dst = out / p.name
            if not dst.exists():
                shutil.copy2(p, dst)

    items = []
    for k, p in enumerate(plan):
        pick = pool[k % len(pool)]
        rnd = k // len(pool)
        v = VARIANTS[rnd % len(VARIANTS)]
        items.append({
            "idx": p["idx"], "t": p["t"], "beat": p["beat"],
            "title": p["title"], "subject": p["subject"],
            "file": "images/" + pick.name,
            "variant": v,
        })
    log("  配图 %d 张（%s，库内 %d 张）" % (len(items), source, len(pool)))
    return items, source


def write_image_requests(job_dir, styles, plan, cfg):
    """把每张配图的生图提示词落盘，供 AI/人工生成定制配图后再重跑。"""
    out = {}
    for sid in styles:
        st = next((s for s in cfg["styles"] if s["id"] == sid), None)
        style_hint = (st or {}).get("imageStyle", "")
        out[sid] = [{
            "n": p["idx"], "t": p["t"], "title": p["title"],
            "file": "images/%s_%02d.png" % (sid, p["idx"]),
            "prompt": ("%s. Scene: %s. Vertical 9:16 short-video b-roll, "
                       "cinematic, rich detail, no text, no watermark"
                       % (style_hint, p["subject"])),
        } for p in plan]
    (job_dir / "image_requests.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


# ================================================================== 配置
def build_cfg(job_dir, opts, duration, beats, images):
    """生成这个任务专属的模板配置（templates.json 的结构，但换了内容和时长）。"""
    base = json.loads(TPL_JSON.read_text(encoding="utf-8"))
    brand = dict(base.get("brand", {}))
    brand.update({k: v for k, v in (opts.get("brand") or {}).items() if v})
    # 每套模板自带配图落点（templates.json 的 imageLayer.xy），默认走它；
    # 前端显式选了方位才整片统一覆盖。
    pos = (opts.get("imagePos") or "auto").strip()
    styles = json.loads(json.dumps(base.get("styles", [])))
    if pos and pos != "auto":
        for s in styles:
            il = dict(s.get("imageLayer") or {})
            il["pos"] = pos
            il.pop("xy", None)
            s["imageLayer"] = il
    cfg = {
        "duration": round(duration, 3),
        "brand": brand,
        "beats": beats,
        "keywords": opts.get("keywords") or base.get("keywords", []),
        "metrics": base.get("metrics", []),
        "chart": base.get("chart", []),
        "danmu": base.get("danmu", []),
        "images": images,
        "imageLayer": {
            "enabled": bool(opts.get("images", True)),
            "pos": "tl" if pos == "auto" else pos,
            "opacity": float(opts.get("imageOpacity", 0.88)),
        },
        "styles": styles,
    }
    (job_dir / "templates.js").write_text(
        "/* AUTO-GENERATED for this job — edit job.json and re-run instead. */\n"
        "window.__TPL = " + json.dumps(cfg, ensure_ascii=False, indent=1) + ";\n",
        encoding="utf-8")
    shutil.copy2(SRC_PAGE, job_dir / "gallery.html")
    return cfg


# ================================================================== 渲染
async def _render(browser, page_path, style, duration, out_mov, log, on_tick=None):
    """逐帧渲染并把 PNG 直接管道给 ffmpeg，落一个带 alpha 的 mov。

    不落地几千张 PNG：省磁盘、少一堆临时文件，合成时也只有一个输入。
    """
    count = int(round(duration * FPS))
    page = await browser.new_page(viewport={"width": OUT_W, "height": OUT_H},
                                  device_scale_factor=1)
    await page.goto(page_path.as_uri() + "?style=%s&mode=overlay" % style,
                    wait_until="load")
    await page.wait_for_function("window.ready === true", timeout=60000)

    proc = subprocess.Popen(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "image2pipe", "-vcodec", "png", "-framerate", str(FPS), "-i", "-",
         "-c:v", "qtrle", "-pix_fmt", "argb", "-r", str(FPS), str(out_mov)],
        stdin=subprocess.PIPE)
    try:
        for i in range(count):
            await page.evaluate("t => window.render(t)", i / FPS)
            proc.stdin.write(await page.screenshot(type="png", omit_background=True))
            if on_tick and i % 30 == 0:
                on_tick(i, count)
    finally:
        try:
            proc.stdin.close()
        except Exception:                              # noqa: BLE001
            pass
        rc = proc.wait()
        await page.close()
    if rc != 0:
        raise RuntimeError("动效层编码失败（ffmpeg rc=%d）" % rc)
    return count


def render_all(job_dir, styles, duration, log, status=None, base_pct=25, span=60):
    """渲染若干套模板的透明动效层（串行，浏览器只起一次）。"""
    path = job_dir / "gallery.html"
    done = {"n": 0}

    async def main():
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True, args=["--no-sandbox", "--allow-file-access-from-files"])
            for sid in styles:
                fdir = job_dir / "frames"
                fdir.mkdir(parents=True, exist_ok=True)
                out_mov = fdir / ("%s.mov" % sid)
                if out_mov.exists():
                    out_mov.unlink()
                if status:
                    status.style(sid, "render", base_pct, "渲染动效帧")
                log("  渲染 %s 的动效层…" % sid)
                t0 = time.time()

                def tick(i, c, _b=base_pct, _s=span):
                    if status:
                        status.set(pct=_b + _s * (i / max(1, c)))

                n = await _render(browser, path, sid, duration, out_mov, log, tick)
                log("  %s：%d 帧，用时 %.1fs" % (sid, n, time.time() - t0))
                done["n"] += 1
            await browser.close()

    asyncio.run(main())
    return done["n"]


# ================================================================== 合成
def build_premix(job_dir, base, style, duration):
    """原声 + 配乐 + 音效，配乐被原声侧链压制，先不做响度归一。"""
    premix = job_dir / ("premix_%s.wav" % style)
    graph = (
        "[0:a]aresample=48000,aformat=channel_layouts=stereo,highpass=f=75,"
        "acompressor=threshold=-22dB:ratio=3.5:attack=10:release=220:makeup=6[voice];"
        "[1:a]aresample=48000,aformat=channel_layouts=stereo,volume=0.36[mus];"
        "[2:a]aresample=48000,aformat=channel_layouts=stereo,volume=0.48[q];"
        "[mus][q]amix=inputs=2:duration=longest:normalize=0[bed];"
        "[bed][voice]sidechaincompress=threshold=0.045:ratio=9:attack=8:release=420[duck];"
        "[voice][duck]amix=inputs=2:duration=first:normalize=0,"
        "aformat=channel_layouts=stereo,aresample=48000[aout]"
    )
    run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(base),
         "-i", str(job_dir / "audio" / ("music_%s.wav" % style)),
         "-i", str(job_dir / "audio" / ("sfx_%s.wav" % style)),
         "-filter_complex", graph, "-map", "[aout]", "-t", "%.3f" % duration,
         "-c:a", "pcm_s16le", "-ac", "2", "-ar", "48000", str(premix)])
    return premix


def measure_loudness(path):
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path),
                        "-af", "loudnorm=print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", p.stderr or "", re.S)
    if not m:
        return None
    d = json.loads(m[-1])
    return float(d["input_i"]), float(d["input_tp"])


def mux(job_dir, base, style, duration, target):
    """把透明动效层叠到原片上，配乐按测得的响度补齐增益后混入。"""
    premix = build_premix(job_dir, base, style, duration)
    meas = measure_loudness(premix)
    gain = 0.0
    if meas:
        # 两步法：先量后补。-15 LUFS 是短视频平台的安全区，后级限幅兜底。
        gain = min(-15.0 - meas[0], 7.5)
    grade = GRADE[STYLE_GRADE.get(style, "warm")]
    graph = (
        "[2:a]volume=%.2fdB,alimiter=level_in=1:level_out=1:limit=0.794:"
        "attack=5:release=80:level=disabled,aformat=channel_layouts=stereo[aout];"
        "[0:v]%s[graded];"
        "[1:v]format=rgba[ov];"
        "[graded][ov]overlay=0:0:eof_action=pass:format=auto,format=yuv420p[vout]"
        % (gain, grade)
    )
    run([FFMPEG, "-hide_banner", "-loglevel", "warning", "-y",
         "-i", str(base),
         "-i", str(job_dir / "frames" / ("%s.mov" % style)),
         "-i", str(premix),
         "-filter_complex", graph,
         "-map", "[vout]", "-map", "[aout]", "-t", "%.3f" % duration,
         "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
         "-movflags", "+faststart", str(target)])
    return meas, gain


# ================================================================== 质检
def audio_envelope(path, fps=100):
    """用 ffmpeg 把音轨降到 8kHz 单声道再取包络，用来验证原声是否原样保留。"""
    import numpy as np
    p = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path),
                        "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                       capture_output=True)
    x = np.frombuffer(p.stdout, dtype="<i2").astype(np.float64)
    if not len(x):
        return None
    win = max(1, 8000 // fps)
    k = len(x) // win
    x = np.abs(x[:k * win]).reshape(k, win).mean(axis=1)
    return x


def verify(base, out):
    import numpy as np
    a, b = audio_envelope(base), audio_envelope(out)
    if a is None or b is None:
        return None
    n = min(len(a), len(b))
    a, b = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
    den = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / den) if den else 0.0


# ================================================================== 主流程
def run_job(job_dir, opts, log=print, status=None):
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    styles = opts["styles"]
    log("任务目录：%s" % job_dir)
    log("模板：%s" % "、".join(styles))

    # 1) 探测
    if status:
        status.set(step="probe", message="探测视频", pct=3)
    info = probe(job_dir / "input.mp4")
    duration = info["duration"]
    log("原片：%.2fs %dx%d 音轨=%s"
        % (duration, info["width"], info["height"], "有" if info["hasAudio"] else "无"))

    # 2) 分节拍 + 配图规划
    if status:
        status.set(step="plan", message="自动分节拍与配图", pct=6)
    beats = autobeat.make_beats(duration, opts.get("script"), opts.get("titles"))
    plan, meta = autobeat.images_for(duration, beats, opts.get("imagePolicy"))
    log("节拍 %d 个，每套配图 %d 张（%s）" % (len(beats), meta["imagesPerStyle"], meta["rule"]))

    cfg = json.loads(TPL_JSON.read_text(encoding="utf-8"))
    write_image_requests(job_dir, styles, plan, cfg)

    # 3) 底片
    if status:
        status.set(step="base", message="统一画面规格", pct=10)
    base = job_dir / "base.mp4"
    build_base(job_dir / "input.mp4", base, info)
    log("底片已就绪（%dx%d / %dfps，原声不变）" % (OUT_W, OUT_H, FPS))

    # 4) 配乐
    if status:
        status.set(step="music", message="为每套模板生成配乐", pct=15)
    audio_dir = job_dir / "audio"
    audio_dir.mkdir(exist_ok=True)
    events = [b["t0"] for b in beats]
    for sid in styles:
        music_gen.build(sid, duration, events, audio_dir)
    log("已生成 %d 套配乐 + 音效轨" % len(styles))

    # 5) 配图 + 任务配置
    images_by_style = {}
    for sid in styles:
        items, _ = prepare_images(job_dir, sid, plan, log)
        images_by_style[sid] = items
    build_cfg(job_dir, opts, duration, beats, images_by_style.get(styles[0], []))

    # 6) 逐帧渲染（每套的配图不同，渲染前重建页面配置）
    if status:
        status.set(step="render", message="渲染动效帧", pct=25)
    outputs = []
    for si, sid in enumerate(styles):
        build_cfg(job_dir, opts, duration, beats, images_by_style.get(sid, []))
        if status:
            status.style(sid, "rendering", 25 + 60 * si / len(styles), "渲染动效帧")
        log("── %s ──" % sid)
        render_all(job_dir, [sid], duration, log, status, 25 + 60 * si / len(styles), 60 / len(styles))

        # 7) 合成
        if status:
            status.style(sid, "muxing", 86 + 10 * si / len(styles), "合成音画")
        try:
            num = [s["id"] for s in cfg["styles"]].index(sid) + 1
        except ValueError:
            num = si + 1
        out = job_dir / "out" / ("%02d_%s.mp4" % (num, sid))
        out.parent.mkdir(exist_ok=True)
        meas, gain = mux(job_dir, base, sid, duration, out)

        # 8) 质检
        corr = verify(base, out)
        log("  成片 %s  响度=%s 增益=%+.2fdB  原声保留=%.3f"
            % (out.name, meas, gain, corr if corr is not None else float("nan")))
        outputs.append({
            "style": sid,
            "file": str(out.relative_to(job_dir)),
            "name": out.name,
            "duration": duration,
            "loudness": meas[0] if meas else None,
            "voice_keep": round(corr, 4) if corr is not None else None,
        })
        if status:
            status.style(sid, "done", 86 + 14 * (si + 1) / len(styles), "已出片")
            status.data["outputs"] = outputs
            status.write()
        # 动效层中间文件用完即弃（只有一个文件，不产生批量临时文件）
        try:
            (job_dir / "frames" / ("%s.mov" % sid)).unlink()
        except OSError:
            pass

    return {"duration": duration, "beats": len(beats), "outputs": outputs,
            "info": info, "imagesPerStyle": meta["imagesPerStyle"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--styles", default="crt")
    ap.add_argument("--out", default="")
    ap.add_argument("--script", default="")
    ap.add_argument("--titles", default="")
    ap.add_argument("--image-pos", default="auto",
                    help="配图位置：auto(按模板自动避让) / tl / tr / ml / mr / bl / br")
    ap.add_argument("--no-images", action="store_true")
    a = ap.parse_args()

    jid = a.out or ("cli_" + time.strftime("%m%d_%H%M%S"))
    paths.ensure_workspace()
    job_dir = JOBS / jid
    job_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(a.input, job_dir / "input.mp4")

    opts = {
        "styles": [s.strip() for s in a.styles.split(",") if s.strip()],
        "script": a.script,
        "titles": [t for t in a.titles.split("|") if t.strip()],
        "imagePos": a.image_pos,
        "images": not a.no_images,
    }
    (job_dir / "job.json").write_text(
        json.dumps(opts, ensure_ascii=False, indent=2), encoding="utf-8")

    st = Status(job_dir / "status.json", len(opts["styles"]))
    st.write()
    try:
        res = run_job(job_dir, opts, log=lambda m: print(m, flush=True), status=st)
        st.finish(res["outputs"])
        print("\n完成：")
        for o in res["outputs"]:
            print("  %s  原声保留 %s" % (o["file"], o["voice_keep"]))
    except Exception as e:                                # noqa: BLE001
        st.fail(str(e))
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
