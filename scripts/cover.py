#!/usr/bin/env python3
"""抖音爆款封面图。

一条视频出片的同时给一张封面：**截首帧 + 从标题文案里提炼一行大字一行小字**，
按抖音爆款的版式排好（大字压住画面、小字补一句、亮色点睛），出 3:4 / 9:16 / 1:1。

三件事分开，都能单独跑、单独改：

1. `grab(src)`     —— 取首帧。默认就是第 0 秒那一帧；太暗就往后找一点。
2. `summarize()`   —— 从 titles / script / brand 里挑出最像钩子的一句当大字，
                      再配一句小字。规则打分，不用模型，毫秒出结果。
3. `render()`      —— 交给浏览器排版（`assets/cover.html`），截图成 PNG。

另外产出一份 `ai_prompts.json`：想让 OpenAI Image / Nano Banana 把底图重画得更好看，
把里面的 prompt 连同首帧一起丢给模型即可（首帧作为参考图，保持人物一致）。

用法：
    python3 cover.py --job=e2e_cam --styles=glass
    python3 cover.py --job=e2e_cam --styles=glass --big="3步搞定获客" --small="AI 替你干活"
    python3 cover.py --job=e2e_cam --styles=glass --layouts=bottom,top --ratios=3x4,9x16,1x1
"""

from pathlib import Path
import argparse
import json
import re
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import paths                                          # noqa: E402
import camera as cam_mod                              # noqa: E402

FFMPEG = paths.ffmpeg()

# 抖音主页封面推荐 3:4；视频内封面是 9:16；1:1 用来做方图投放
RATIOS = {"3x4": (1080, 1440), "9x16": (1080, 1920), "1x1": (1080, 1080)}

# 首帧到画布的放大倍率：3:4 / 1:1 画幅比人像窄，要放大把左右补满
FG_ZOOM = {"3x4": 1.16, "9x16": 1.04, "1x1": 1.26}

# 影调 -> 封面配色（和 pipeline.STYLE_GRADE 对齐）
GRADE_PALETTE = {
    "warm": "orange", "cool": "cyan", "punch": "yellow",
    "neon": "pink", "mono": "white",
}
STYLE_GRADE = {
    "crt": "warm", "retro": "mono", "dash": "cool", "report": "mono",
    "journal": "warm", "chalk": "cool", "promo": "punch", "danmu": "punch",
    "magazine": "mono", "minimal": "mono", "news": "cool", "product": "neon",
    "glass": "cool", "timeline": "cool", "quote": "warm", "podcast": "warm",
    "travel": "punch", "blueprint": "cool", "gradient": "neon", "card": "warm",
}

# 出现在标题里就说明"这句能当钩子"的词
HOOK_WORDS = [
    "免费", "揭秘", "避坑", "干货", "方法", "步骤", "技巧", "获客", "涨粉", "引流",
    "转化", "成本", "效率", "真相", "别再", "一定要", "为什么", "如何", "教你",
    "学会", "必看", "收藏", "爆", "翻倍", "省钱", "变现", "私域", "裂变", "套路",
    "清单", "复盘", "底层", "普通人", "小白", "老板", "实体店", "AI",
]

# 大字太长时，优先在这些字符处断开
BREAK_AT = re.compile(r"[\s，,、·:：。!！?？\-—/]")
TRIM_EDGE = re.compile(r"^[\s，,、·:：。!！?？\-—/]+|[\s，,、·:：。!！?？\-—/]+$")


# ------------------------------------------------------------------ 1. 首帧
def _frame(src, t, out):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", "%.3f" % max(0.0, t), "-i", str(src),
                    "-frames:v", "1", "-vf",
                    "scale=%d:%d:force_original_aspect_ratio=decrease,"
                    "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black"
                    % (paths.OUT_W, paths.OUT_H, paths.OUT_W, paths.OUT_H),
                    str(out)], check=True)
    return out


def _brightness(png):
    """平均亮度，用来跳过开头的黑场 / 淡入。"""
    try:
        import numpy as np
    except Exception:                                      # noqa: BLE001
        return 255.0
    p = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(png),
                        "-vf", "scale=32:56", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                       capture_output=True)
    if p.returncode != 0 or not p.stdout:
        return 255.0
    return float(np.frombuffer(p.stdout, dtype="u1").mean())


def grab(src, out, at=0.0, log=print):
    """取首帧。用户要的是"视频首帧"，所以默认就取第 0 秒；
    只有当这一帧基本是黑的（淡入 / 黑场）才往后挪一点，避免封面全黑。"""
    _frame(src, at, out)
    if _brightness(out) >= 18:
        log("  封面首帧 %.2fs（原首帧）" % at)
        return out
    for t in (0.35, 0.8, 1.4):
        _frame(src, t, out)
        if _brightness(out) >= 18:
            log("  封面首帧 %.2fs（第 0 秒是黑场，自动后移）" % t)
            return out
    log("  封面首帧 %.2fs（整段都偏暗，仍用首帧）" % at)
    _frame(src, at, out)
    return out


# ------------------------------------------------------------------ 2. 文案
def _clean(s):
    s = (s or "").strip()
    s = re.sub(r"\s+", "", s)
    return TRIM_EDGE.sub("", s)


def _has_hook(text):
    """力度词命中。纯 ASCII 的词要卡词边界，否则 "OPC-AID" 里的 AID 会被当成 "AI"。"""
    for w in HOOK_WORDS:
        if w.isascii():
            if re.search(r"(?<![A-Za-z])" + re.escape(w) + r"(?![A-Za-z])", text):
                return True
        elif w in text:
            return True
    return False


def _score(text, idx, tag="", kind="line"):
    """给一句候选文案打分。分数高 = 更像抖音封面上的那行大字。

    封面要的是"能勾住人的那句"：落在片头/片尾的过场句（开场、收尾）天然吃亏，
    正文里的拆解/方案/行动才是钩子；拍画面的空镜描述（sub）根本不该上封面。
    """
    n = len(text)
    s = 0
    if 5 <= n <= 12:
        s += 3
    elif 13 <= n <= 16:
        s += 1
    elif n < 5:
        s -= 2
    else:
        s -= 3
    if _has_hook(text):
        s += 4
    if re.search(r"\d", text):
        s += 2
    if tag in ("要点", "拆解", "方案", "行动"):
        s += 3
    elif tag in ("开场", "背景", "收尾"):
        s -= 2
    if kind == "sub":
        s -= 3
    s += {0: 2, 1: 2, 2: 1}.get(idx, 0)
    return s


def _shorten(s, limit=12):
    """超长就砍到 limit 字，优先在标点处断。"""
    s = _clean(s)
    if len(s) <= limit:
        return s
    head = s[:limit + 3]
    for m in BREAK_AT.finditer(head):
        if 4 <= m.start() <= limit:
            return _clean(head[:m.start()])
    return s[:limit]


def summarize(beats=None, brand=None, titles=None, script=None,
              big=None, small=None, kicker=None, no_kicker=False):
    """产出封面文案：一行大字 + 一行小字 +（可选）一个小标签。

    候选分四类：line（节拍标题）、sub（配图空镜描述）、brand（品牌名/口号）、
    script（文案切句）。空镜描述是给画图用的，永远只当最后的兜底。
    """
    brand = brand or {}
    cands = []          # [(text, kind, tag)]

    def add(text, kind, tag=""):
        t = _clean(text)
        if not t:
            return
        for i, (et, ek, etag) in enumerate(cands):
            if et == t:                       # 同一句出现两次：把节拍标签补上
                if tag and not etag:
                    cands[i] = (et, ek, tag)
                return
        cands.append((t, kind, tag))

    # 节拍先入：它们带着 tag（开场/拆解/行动…），是打分的关键信息
    for b in (beats or []):
        add(b.get("title"), "line", b.get("tag") or "")
    for t in (titles or []):
        add(t, "line", "")
    if brand.get("slogan"):
        add(brand["slogan"], "brand", "")
    if brand.get("name"):
        add(brand["name"], "brand", "")
    for s in re.split(r"[。！？!?；;\n]+", script or ""):
        add(s, "script", "")
    for b in (beats or []):
        add(b.get("sub"), "sub", b.get("tag") or "")

    scored = sorted(((_score(t, i, tag, kind), i, t, tag, kind)
                     for i, (t, kind, tag) in enumerate(cands)),
                    key=lambda x: (-x[0], x[1]))

    if big:
        big_text = _clean(big)
    elif scored:
        top = next((c for c in scored if c[4] != "sub"), scored[0])
        big_text = _shorten(top[2], 12)
    else:
        big_text = _shorten(brand.get("name") or "值得一看", 12)

    # 小标签先定：品牌名够短才当标签，否则用第一个节拍的 tag
    if no_kicker:
        kick = ""
    elif kicker:
        kick = _clean(kicker)
    else:
        kick = ""
        nm = _clean(brand.get("name") or "")
        if nm and len(nm) <= 10 and nm != big_text:
            kick = nm
        elif scored and scored[0][3]:
            kick = scored[0][3]

    # 小字：和标签、大字都不能重样。先挑别的正文钩子，空镜描述垫在最后
    pool = [c[2] for c in scored[1:] if c[4] != "sub"] + \
           [c[2] for c in scored[1:] if c[4] == "sub"] + \
           [brand.get("slogan"), brand.get("city"), brand.get("name")]
    small_text = ""
    if small:
        small_text = _clean(small)
    else:
        for cand in pool:
            c = _clean(cand)
            if c and c != big_text and c != kick and 3 <= len(c) <= 18:
                small_text = c
                break

    return {"big": big_text, "small": small_text, "kicker": kick}


# ------------------------------------------------------------------ 3. 渲染
def _write_cfg(cover_dir, cfg):
    (cover_dir / "cover_cfg.js").write_text(
        "window.__COVER = " + json.dumps(cfg, ensure_ascii=False, indent=1) + ";\n",
        encoding="utf-8")


async def _shoot(page_path, w, h, png_out):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=["--no-sandbox", "--allow-file-access-from-files"])
        page = await browser.new_page(viewport={"width": w, "height": h},
                                      device_scale_factor=1)
        await page.goto(page_path.as_uri(), wait_until="load")
        await page.wait_for_function("window.ready === true", timeout=60000)
        await page.screenshot(path=str(png_out), type="png")
        layout = await page.evaluate("() => window.__LAYOUT")
        await browser.close()
    return layout


def render(cover_dir, cfg, png_out):
    import asyncio
    _write_cfg(cover_dir, cfg)
    return asyncio.run(_shoot(cover_dir / "cover.html", cfg["w"], cfg["h"], png_out))


# ------------------------------------------------------------------ AI 重画提示词
def ai_prompt(style_cfg, text, ratio="3x4"):
    """给 OpenAI Image / Nano Banana 的底图重绘提示词（首帧作为参考图一起传）。"""
    look = (style_cfg or {}).get("imageStyle") or \
        "clean modern commercial photography, cinematic lighting, shallow depth of field"
    name = (style_cfg or {}).get("name") or ""
    head = ("把参考图中的人物原样保留（同一张脸、同样的衣服、同样的景别和姿势），"
            "只重绘背景与光线，做成适合抖音竖版封面的底图。")
    body = ("画面整体风格：%s。构图：人物居中偏上，视线看向镜头，"
            "画面上下各留出一条干净的虚化区域，方便后面压大字。"
            "要求：真实摄影质感、高清晰度、氛围光、背景虚化，不要出现任何文字、"
            "水印、logo、边框。" % look)
    ratio_hint = ("输出画幅 3:4 竖版（1080x1440）" if ratio == "3x4"
                  else "输出画幅 9:16 竖版（1080x1920）")
    return {
        "template": name,
        "ratio": ratio,
        "prompt": " ".join([head, body, ratio_hint + "。"]),
        "negative": "文字, 字幕, 水印, logo, 边框, 多余的手指, 畸形人脸, 过度磨皮",
        "suggestedText": text,
        "models": ["OpenAI gpt-image-1 / gpt-image-2", "Google Nano Banana / Nano Banana Pro"],
    }


# ------------------------------------------------------------------ 主流程
def make_covers(job_dir, style, style_cfg, brand, beats, script, opts=None,
                log=print, base=None):
    """给一套模板出一组封面，返回 [{file, ratio, layout}]。"""
    opts = opts or {}
    job_dir = Path(job_dir)
    style = style or "cover"
    cover_dir = job_dir / "covers"
    cover_dir.mkdir(exist_ok=True)

    ratios = opts.get("ratios") or ["3x4", "9x16"]
    layouts = opts.get("layouts") or ["bottom"]

    # 渲染页与配置放一起，用户可以自己打开 cover.html 改字改色再截
    shutil.copy2(paths.SRC_COVER, cover_dir / "cover.html")

    src = Path(base or (job_dir / "base.mp4"))
    if not src.exists():
        src = job_dir / "input.mp4"

    text = summarize(beats, brand, opts.get("titles"), script,
                     opts.get("big"), opts.get("small"), opts.get("kicker"),
                     no_kicker=bool(opts.get("noKicker")))
    log("  封面文案：「%s」/「%s」%s"
        % (text["big"], text["small"],
           ("  标签：" + text["kicker"]) if text["kicker"] else ""))

    # 首帧只取一次，所有画幅共用
    fg = cover_dir / "firstframe.jpg"
    at = float(opts.get("at", 0.0) or 0.0)
    grab(src, fg, at=at, log=log)

    anchor = opts.get("anchor") or cam_mod.estimate_anchor(src)
    palette = opts.get("palette") or GRADE_PALETTE.get(
        STYLE_GRADE.get(style, "warm"), "yellow")

    out = []
    for ratio in ratios:
        w, h = RATIOS.get(ratio, RATIOS["3x4"])
        for layout in layouts:
            cfg = {
                "w": w, "h": h, "ratio": ratio, "layout": layout,
                "palette": palette,
                # 底图与首帧都用首帧：底图模糊铺满，首帧清晰压在上面
                "bg": "firstframe.jpg", "fg": "firstframe.jpg",
                "anchor": list(anchor), "fgZoom": FG_ZOOM.get(ratio, 1.22),
                "kicker": text["kicker"], "big": text["big"], "small": text["small"],
                "meta": opts.get("meta", "") or "",
            }
            name = "%s_cover_%s%s.png" % (style, ratio, "" if layout == "bottom" else "_" + layout)
            png = cover_dir / name
            lay = render(cover_dir, cfg, png)
            out.append({"style": style, "file": str(png.relative_to(job_dir)),
                        "ratio": ratio, "layout": layout,
                        "w": w, "h": h, "text": text,
                        "fontBig": lay.get("fsBig"), "lines": lay.get("lines")})
            log("  封面 %s  %dx%d  %s  大字 %s" % (png.name, w, h, layout, lay))

    # AI 重画提示词（首帧作参考图，保持人物一致）
    (cover_dir / "ai_prompts.json").write_text(json.dumps(
        {"firstFrame": "firstframe.jpg",
         "text": text,
         "items": [ai_prompt(style_cfg, text, r) for r in ratios]},
        ensure_ascii=False, indent=2), encoding="utf-8")

    (cover_dir / "cover.json").write_text(json.dumps(
        {"style": style, "text": text, "palette": palette, "anchor": list(anchor),
         "covers": out}, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ------------------------------------------------------------------ CLI
def main():
    ap = argparse.ArgumentParser(description="抖音爆款封面图")
    ap.add_argument("--job", required=True, help="任务目录名（~/VideoStudio/jobs/<job>）")
    ap.add_argument("--styles", default="", help="只给这几套模板出封面，逗号分隔；留空=全部")
    ap.add_argument("--ratios", default="3x4,9x16", help="画幅：3x4,9x16,1x1")
    ap.add_argument("--layouts", default="bottom", help="版式：bottom（大字压底）/top（压顶）/center")
    ap.add_argument("--palette", default="", help="配色：yellow/orange/cyan/pink/white")
    ap.add_argument("--big", default="", help="手动指定大字")
    ap.add_argument("--small", default="", help="手动指定小字")
    ap.add_argument("--kicker", default="", help="手动指定小标签")
    ap.add_argument("--no-kicker", action="store_true", help="不要小标签，只留一行大字一行小字")
    ap.add_argument("--at", default="0.0", help="截第几秒当首帧（默认 0）")
    ap.add_argument("--input", default="", help="直接指定底片/视频路径（默认 job/base.mp4）")
    a = ap.parse_args()

    job_dir = paths.JOBS / a.job
    if not job_dir.is_dir():
        print("没有这个任务目录：%s" % job_dir, file=sys.stderr)
        sys.exit(1)

    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8")) \
        if (job_dir / "job.json").exists() else {}
    cfg = {}
    if (job_dir / "templates.js").exists():
        m = re.search(r"window\.__TPL\s*=\s*(\{.*\})\s*;",
                      (job_dir / "templates.js").read_text(encoding="utf-8"), re.S)
        if m:
            cfg = json.loads(m.group(1))
    if not cfg:
        cfg = json.loads(paths.TPL_JSON.read_text(encoding="utf-8"))

    brand = job.get("brand") or cfg.get("brand") or {}
    beats = cfg.get("beats") or []
    script = job.get("script") or ""
    styles = [s.strip() for s in a.styles.split(",") if s.strip()] or \
             [s["id"] for s in (cfg.get("styles") or [])][:1]

    base = Path(a.input).expanduser() if a.input else None
    src = base or (job_dir / "base.mp4")
    if not src.exists():
        src = job_dir / "input.mp4"
    anchor = cam_mod.estimate_anchor(src)

    opts = {"ratios": [r.strip() for r in a.ratios.split(",") if r.strip()],
            "layouts": [l.strip() for l in a.layouts.split(",") if l.strip()],
            "palette": a.palette, "big": a.big, "small": a.small,
            "kicker": a.kicker, "at": float(a.at), "anchor": anchor,
            "titles": job.get("titles") or [], "noKicker": a.no_kicker}

    print("任务 %s，%d 套模板，锚点 %s" % (a.job, len(styles), anchor))
    for sid in styles:
        st = next((s for s in (cfg.get("styles") or []) if s["id"] == sid), None)
        print("── %s ──" % sid)
        make_covers(job_dir, sid, st, brand, beats, script, opts, base=src)
    print("\n封面已写到 %s" % (job_dir / "covers"))


if __name__ == "__main__":
    main()
