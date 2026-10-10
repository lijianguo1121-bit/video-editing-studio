#!/usr/bin/env python3
"""统一路径解析层。

同一套脚本要能跑在两种布局下，所以这里把「引擎在哪」和「产物写哪」拆开：

    A) skill 布局（安装后的默认形态）
       <skill>/scripts/pipeline.py
       <skill>/assets/gallery.html, templates.json, web/index.html, library/, thumbs/

    B) 旧工程布局（开发这套东西时的原始工程）
       <project>/studio/pipeline.py
       <project>/gallery.html, templates.json, gallery_images/, studio/web/

产物（jobs / uploads / 成片）一律写到**工作区**，默认 `~/VideoStudio`，
可以用环境变量 `VIDEO_STUDIO_WORKSPACE` 覆盖。这样 skill 目录本体保持干净、
不会被渲染出来的几百兆中间文件污染，也方便整体删除或换盘。
"""

from pathlib import Path
import os

HERE = Path(__file__).resolve().parent


# ------------------------------------------------------------------ 引擎位置
def _first_dir(*cands):
    for c in cands:
        if c and Path(c).is_dir():
            return Path(c).resolve()
    return Path(cands[-1])


SKILL_ROOT = HERE.parent if HERE.name == "scripts" else HERE

# 引擎根：优先 skill 布局的 assets/，其次旧工程根目录，最后回落到脚本同级
ASSETS = _first_dir(
    SKILL_ROOT / "assets",
    HERE.parent,          # 旧工程：<project>/studio/.. == <project>
    HERE,
)

SRC_PAGE = next((p for p in (ASSETS / "gallery.html",
                             HERE.parent / "gallery.html",
                             HERE / "gallery.html") if p.exists()),
                ASSETS / "gallery.html")

# 封面渲染页（抖音爆款封面版式：一行大字 + 一行小字）
SRC_COVER = next((p for p in (ASSETS / "cover.html",
                              HERE.parent / "cover.html",
                              HERE / "cover.html") if p.exists()),
                 ASSETS / "cover.html")

TPL_JSON = next((p for p in (ASSETS / "templates.json",
                             HERE.parent / "templates.json",
                             HERE / "templates.json") if p.exists()),
                ASSETS / "templates.json")

WEB = _first_dir(ASSETS / "web", HERE / "web", HERE.parent / "studio" / "web")

# 内置配图库（压缩过的 JPEG）；旧工程里是原始 PNG 目录
LIBRARY = _first_dir(ASSETS / "library", HERE.parent / "gallery_images",
                     HERE / "gallery_images", ASSETS / "library")

# 选择页用的模板缩略图
THUMBS = _first_dir(ASSETS / "thumbs", HERE.parent / "gallery_previews",
                    HERE / "gallery_previews", ASSETS / "thumbs")


# ------------------------------------------------------------------ 工作区
def _workspace():
    env = os.environ.get("VIDEO_STUDIO_WORKSPACE")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / "VideoStudio").resolve()


WORKSPACE = _workspace()
JOBS = WORKSPACE / "jobs"
UPLOADS = WORKSPACE / "uploads"


def ensure_workspace():
    JOBS.mkdir(parents=True, exist_ok=True)
    UPLOADS.mkdir(parents=True, exist_ok=True)
    return WORKSPACE


# ------------------------------------------------------------------ 依赖定位
def ffmpeg():
    """优先用 imageio-ffmpeg 自带的静态二进制（免装 ffmpeg），否则退回系统 PATH。"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:                                  # noqa: BLE001
        import shutil
        exe = shutil.which("ffmpeg")
        if not exe:
            raise RuntimeError(
                "找不到 ffmpeg。装一个最省事：pip install imageio-ffmpeg")
        return exe


# 出片规格：竖屏 720x1280 / 30fps，短视频平台通用
FPS = 30
OUT_W, OUT_H = 720, 1280


def describe():
    """自检时打印用。"""
    return {
        "skillRoot": str(SKILL_ROOT),
        "assets": str(ASSETS),
        "renderPage": str(SRC_PAGE),
        "renderPageOK": SRC_PAGE.exists(),
        "coverPage": str(SRC_COVER),
        "coverPageOK": SRC_COVER.exists(),
        "templates": str(TPL_JSON),
        "templatesOK": TPL_JSON.exists(),
        "web": str(WEB),
        "webOK": (WEB / "index.html").exists(),
        "library": str(LIBRARY),
        "libraryCount": len(list(LIBRARY.glob("*.jpg"))) + len(list(LIBRARY.glob("*.png")))
        if LIBRARY.is_dir() else 0,
        "workspace": str(WORKSPACE),
    }
