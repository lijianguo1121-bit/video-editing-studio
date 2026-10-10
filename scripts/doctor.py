#!/usr/bin/env python3
"""环境自检 —— 装完先跑这个，缺什么它会直接告诉你补哪一条命令。

    python3 scripts/doctor.py

检查项：Python 版本 / numpy / playwright / chromium / ffmpeg / 引擎文件 / 工作区可写。
"""

from pathlib import Path
import importlib
import os
import shutil
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

OK, WARN, BAD = "  ✓", "  !", "  ✗"
problems = []
warnings = []


def line(mark, title, detail=""):
    print("%s %-22s %s" % (mark, title, detail))


print("=" * 68)
print("视频剪辑工作流 · 环境自检")
print("=" * 68)

# ---------------------------------------------------------------- Python
print("\n[运行时]")
v = sys.version_info
if v >= (3, 8):
    line(OK, "Python", "%d.%d.%d  %s" % (v.major, v.minor, v.micro, sys.executable))
else:
    line(BAD, "Python", "%d.%d.%d —— 需要 3.8+" % (v.major, v.minor, v.micro))
    problems.append("Python 版本过低，装 3.10+ 更稳")

# ---------------------------------------------------------------- 依赖
print("\n[依赖包]")
for mod, pipname, why in (
    ("numpy", "numpy", "配乐合成与音频质检"),
    ("playwright", "playwright", "逐帧渲染动效层"),
    ("imageio_ffmpeg", "imageio-ffmpeg", "内置 ffmpeg 二进制"),
):
    try:
        m = importlib.import_module(mod)
        line(OK, mod, "%s  (%s)" % (getattr(m, "__version__", ""), why))
    except Exception:                                   # noqa: BLE001
        line(BAD, mod, "缺失 —— 负责 %s" % why)
        problems.append("pip install %s" % pipname)

# ---------------------------------------------------------------- ffmpeg
print("\n[ffmpeg]")
try:
    import paths
    ff = paths.ffmpeg()
    line(OK, "ffmpeg", ff)
except Exception as e:                                  # noqa: BLE001
    line(BAD, "ffmpeg", str(e))
    problems.append("pip install imageio-ffmpeg")

# ---------------------------------------------------------------- chromium
print("\n[浏览器内核]")
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        exe = p.chromium.executable_path
        if exe and Path(exe).exists():
            line(OK, "chromium", exe)
        else:
            raise RuntimeError("未安装：%s" % exe)
except Exception as e:                                  # noqa: BLE001
    line(BAD, "chromium", str(e).split("\n")[0])
    problems.append("python3 -m playwright install chromium")

# ---------------------------------------------------------------- 引擎文件
print("\n[引擎文件]")
import json                                               # noqa: E402
d = paths.describe()
for key, title, must in (
    ("renderPage", "渲染页 gallery.html", True),
    ("coverPage", "封面页 cover.html", True),
    ("templates", "模板配置 templates.json", True),
    ("web", "前端页面 web/index.html", True),
):
    ok = d.get(key + "OK") if key != "web" else d.get("webOK")
    line(OK if ok else BAD, title, d.get(key, ""))
    if must and not ok:
        problems.append("引擎文件缺失：%s" % d.get(key))

n_lib = d["libraryCount"]
if n_lib:
    line(OK, "内置配图库", "%d 张  %s" % (n_lib, d["library"]))
else:
    line(WARN, "内置配图库", "空 —— 画面里不会自动出配图（可用提示词自行生成后放入）")
    warnings.append("内置配图库为空，配图层将不出图")

# ---------------------------------------------------------------- 工作区
print("\n[工作区]")
ws = Path(d["workspace"])
try:
    (ws / "jobs").mkdir(parents=True, exist_ok=True)
    probe = ws / ".write_test"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    line(OK, "可写", str(ws))
except Exception as e:                                  # noqa: BLE001
    line(BAD, "不可写", "%s（%s）" % (ws, e))
    problems.append("工作区不可写，换一个：export VIDEO_STUDIO_WORKSPACE=~/VideoStudio")

free = shutil.disk_usage(ws if ws.exists() else Path.home()).free / 2**30
line(OK if free > 5 else WARN, "剩余空间", "%.1f GB（一条 60s 视频约需 0.5-1.5 GB）" % free)
if free <= 5:
    warnings.append("磁盘剩余不足 5GB，长视频可能中途失败")

# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 68)
if problems:
    print("需要处理 %d 项：" % len(problems))
    for i, p in enumerate(problems, 1):
        print("  %d) %s" % (i, p))
    print("\n一键补齐（建议先建虚拟环境，避免污染系统 Python）：")
    print("  python3 -m venv .venv && source .venv/bin/activate")
    print("  python3 -m pip install numpy playwright imageio-ffmpeg")
    print("  python3 -m playwright install chromium")
elif warnings:
    print("环境可用，但有 %d 条提醒：" % len(warnings))
    for w in warnings:
        print("  · %s" % w)
else:
    print("环境就绪 ✓  直接启动：")
    print("  %s %s/server.py --open" % (sys.executable, HERE))
print("=" * 68)

sys.exit(1 if problems else 0)
