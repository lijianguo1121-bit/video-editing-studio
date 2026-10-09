#!/usr/bin/env python3
"""单帧预览（快速调版式用）。

不跑整条片子，只把动效层叠到底片的指定时间点上，几秒出图。
调模板版式、调配图位置时用它，比渲整片快几十倍。

用法：
    # 预览某个任务的某几套模板
    python3 preview.py --job=test02 --styles=crt,news --times=4.6,10.6,16.2
    # 只想看一套模板的全片节奏
    python3 preview.py --job=test02 --styles=neon --times=1,5,9,13,17,19
"""

from pathlib import Path
import argparse
import asyncio
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import paths                                          # noqa: E402
from pipeline import GRADE, STYLE_GRADE, VARIANTS     # noqa: E402

PROJECT = paths.ASSETS
JOBS = paths.JOBS
SRC_PAGE = paths.SRC_PAGE
FFMPEG = paths.ffmpeg()
OUT_W, OUT_H = paths.OUT_W, paths.OUT_H


def load_cfg(job_dir):
    import json
    import re as _re
    js = (job_dir / "templates.js").read_text(encoding="utf-8")
    m = _re.search(r"window\.__TPL\s*=\s*(\{.*\})\s*;", js, _re.S)
    return json.loads(m.group(1))


def style_cfg(cfg, job_dir, style, pos="", size=None):
    """按这套模板重建图片计划，口径和流水线一致：任务内配图优先，其次内置库。"""
    import json
    import os
    import autobeat
    plan, _ = autobeat.images_for(cfg["duration"], cfg["beats"])
    ok = (".png", ".jpg", ".jpeg", ".webp")
    imgs = sorted(p for p in (job_dir / "images").glob("%s_*" % style)
                  if p.suffix.lower() in ok)
    if not imgs:
        imgs = sorted(p for p in paths.LIBRARY.glob("%s_*" % style)
                      if p.suffix.lower() in ok)
    items = []
    for k, p in enumerate(plan):
        if not imgs:
            break
        pick = imgs[k % len(imgs)]
        rnd = k // len(imgs)
        items.append({
            "idx": p["idx"], "t": p["t"], "beat": p["beat"],
            "title": p["title"], "subject": p["subject"],
            "file": os.path.relpath(pick, job_dir).replace(os.sep, "/"),
            "variant": VARIANTS[rnd % len(VARIANTS)],
        })
    c = dict(cfg)
    c["images"] = items
    c["imageLayer"] = dict(c.get("imageLayer") or {})
    if pos == "off":
        c["imageLayer"]["enabled"] = False
    elif pos:
        c["imageLayer"]["pos"] = pos
    if size:
        c["imageLayer"]["size"] = list(size)
    return c


def write_cfg(job_dir, cfg):
    import json
    (job_dir / "templates.js").write_text(
        "/* AUTO-GENERATED for this job */\nwindow.__TPL = "
        + json.dumps(cfg, ensure_ascii=False, indent=1) + ";\n", encoding="utf-8")


async def shoot(page_path, style, t, png_out):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=["--no-sandbox", "--allow-file-access-from-files"])
        page = await browser.new_page(viewport={"width": OUT_W, "height": OUT_H},
                                      device_scale_factor=1)
        await page.goto(page_path.as_uri() + "?style=%s&mode=overlay" % style,
                        wait_until="load")
        await page.wait_for_function("window.ready === true", timeout=60000)
        await page.evaluate("t => window.render(t)", t)
        await page.screenshot(path=str(png_out), type="png", omit_background=True)
        await browser.close()


def composite(base, png, style, t, out, scale="540:960"):
    grade = GRADE[STYLE_GRADE.get(style, "warm")]
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
         "-ss", "%.3f" % t, "-i", str(base), "-i", str(png),
         "-filter_complex",
         "[0:v]%s[g];[1:v]format=rgba[ov];[g][ov]overlay=0:0,format=yuv420p,scale=%s[v]"
         % (grade, scale),
         "-map", "[v]", "-frames:v", "1", str(out)], check=True)


def grid(images, out, cols=3):
    if len(images) < 2:
        return
    cols = min(cols, len(images))
    rows = (len(images) + cols - 1) // cols
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    for im in images:
        args += ["-i", str(im)]
    filters = []
    for i in range(len(images)):
        filters.append("[%d:v]scale=360:640,setsar=1[v%d]" % (i, i))
    parts = "".join("[v%d]" % i for i in range(len(images)))
    if len(images) > 1:
        filters.append("%sxstack=inputs=%d:layout=%s[vout]"
                       % (parts, len(images),
                          "|".join("%d_%d" % ((i % cols) * 360, (i // cols) * 640)
                                   for i in range(len(images)))))
    else:
        filters.append("%snull[vout]" % parts)
    args += ["-filter_complex", ";".join(filters), "-map", "[vout]",
             "-frames:v", "1", str(out)]
    subprocess.run(args, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--styles", default="")
    ap.add_argument("--times", default="4.6,10.6,16.2")
    ap.add_argument("--pos", default="", help="临时改配图位置：ml/mr/tl/tr/bl/br/top/bottom/center")
    ap.add_argument("--size", default="", help="临时改配图尺寸，如 200x266")
    a = ap.parse_args()

    job_dir = JOBS / a.job
    if not (job_dir / "base.mp4").exists():
        print("任务目录里没有 base.mp4，先跑一次 pipeline：%s" % job_dir, file=sys.stderr)
        sys.exit(1)
    # 每次预览都用最新的渲染页，改完 gallery.html 立刻能看到效果
    shutil.copy2(SRC_PAGE, job_dir / "gallery.html")
    base_cfg = load_cfg(job_dir)

    import json
    styles = [s.strip() for s in a.styles.split(",") if s.strip()]
    if not styles:
        jp = job_dir / "job.json"
        styles = json.loads(jp.read_text(encoding="utf-8"))["styles"] if jp.exists() else ["crt"]
    times = [float(t) for t in a.times.split(",") if t.strip()]
    size = ([int(v) for v in a.size.lower().split("x")] if "x" in a.size.lower() else None)

    tmp = job_dir / "preview_tmp"
    tmp.mkdir(exist_ok=True)
    made = []
    for sid in styles:
        write_cfg(job_dir, style_cfg(base_cfg, job_dir, sid, a.pos, size))
        shots = []
        for t in times:
            png = tmp / ("%s_%.2f_raw.png" % (sid, t))
            comp = job_dir / ("preview_%s_%05.2fs.png" % (sid, t))
            asyncio.run(shoot(job_dir / "gallery.html", sid, t, png))
            composite(job_dir / "base.mp4", png, sid, t, comp, "720:1280")
            shots.append(comp)
            made.append(comp)
            print("  %s @ %.2fs  ->  %s" % (sid, t, comp.name), flush=True)
        sheet = job_dir / ("preview_sheet_%s.png" % sid)
        grid(shots, sheet)
        if sheet.exists():
            made.append(sheet)
    write_cfg(job_dir, base_cfg)
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n共 %d 张预览图，已写到 %s" % (len(made), job_dir))


if __name__ == "__main__":
    main()
