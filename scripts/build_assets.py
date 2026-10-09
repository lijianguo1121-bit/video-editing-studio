#!/usr/bin/env python3
"""把原始素材压成 skill 包里的轻量素材（一次性构建脚本，不是运行时依赖）。

原工程里配图是 60 张 1024x1536 的 PNG（共 136MB）、模板预览是 122 张 PNG（92MB）。
直接打包进 skill 太重，这里压成：

    assets/thumbs/<style>.jpg      模板缩略图（选择页用，宽 360）
    assets/library/<style>_NN.jpg  内置配图（视频里的配图卡片，512x768）

用法（在原始工程根目录跑）：
    python3 build_assets.py --src=<原工程根> --dst=<skill>/assets
"""

from pathlib import Path
import argparse
import json
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))


def run(args, **kw):
    return subprocess.run(args, check=True, **kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="原始工程根目录")
    ap.add_argument("--dst", required=True, help="skill 的 assets 目录")
    ap.add_argument("--ffmpeg", default="")
    ap.add_argument("--per-style", type=int, default=3)
    ap.add_argument("--size", default="448:672", help="内置配图尺寸（2:3 竖版）")
    ap.add_argument("--quality", default="7", help="JPEG 质量档（ffmpeg q:v，数字越大越小）")
    a = ap.parse_args()

    import paths
    ff = a.ffmpeg or paths.ffmpeg()

    src = Path(a.src).expanduser()
    dst = Path(a.dst).expanduser()
    thumbs = dst / "thumbs"
    library = dst / "library"
    thumbs.mkdir(parents=True, exist_ok=True)
    library.mkdir(parents=True, exist_ok=True)

    cfg = json.loads((src / "templates.json").read_text(encoding="utf-8"))
    ids = [s["id"] for s in cfg["styles"]]
    print("模板 %d 套" % len(ids))

    ok_t = ok_l = miss_t = miss_l = 0

    for sid in ids:
        # ---- 缩略图 ----
        cand = [src / "gallery_previews" / ("%s_009.5s.png" % sid),
                src / "gallery_previews" / ("%s.jpg" % sid)]
        src_thumb = next((p for p in cand if p.exists()), None)
        if src_thumb:
            out = thumbs / ("%s.jpg" % sid)
            run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src_thumb),
                 "-vf", "scale=360:-2", "-q:v", "5", str(out)])
            ok_t += 1
        else:
            miss_t += 1
            print("  缩略图缺失：%s" % sid)

        # ---- 内置配图 ----
        got = 0
        for pat in sorted((src / "gallery_images").glob("%s_*" % sid)):
            if pat.suffix.lower() != ".png" or got >= a.per_style:
                continue
            out = library / ("%s_%02d.jpg" % (sid, got + 1))
            run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(pat),
                 "-vf", "scale=%s:force_original_aspect_ratio=increase,crop=%s"
                 % (a.size, a.size),
                 "-q:v", a.quality, str(out)])
            got += 1
        ok_l += got
        if got == 0:
            miss_l += 1
            print("  配图缺失：%s" % sid)

    print("\n缩略图 %d 张（缺 %d），内置配图 %d 张（缺 %d 套）" % (ok_t, miss_t, ok_l, miss_l))
    for d in (thumbs, library):
        n = len(list(d.iterdir()))
        size = sum(p.stat().st_size for p in d.iterdir()) / 1048576
        print("  %-8s %3d 个文件  %.2f MB" % (d.name, n, size))


if __name__ == "__main__":
    main()
