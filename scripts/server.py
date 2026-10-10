#!/usr/bin/env python3
"""剪辑工作流服务（standard-library HTTP server, no extra deps）。

启动之后浏览器打开 http://127.0.0.1:8760 —— 把视频拖进去、选个序号，剩下的交给流水线。

接口：
    GET  /                          上传/选号页面
    GET  /api/templates             20 套模板 + 缩略图 + 内置配图
    POST /api/upload?name=x.mp4     原始字节直传，落地后立刻探测时长/分辨率
    POST /api/jobs                  建任务并排队
    GET  /api/jobs                  任务列表
    GET  /api/jobs/<id>             任务状态（前端轮询用）
    GET  /api/jobs/<id>/view/<f>    在线播放成片（支持 Range）
    GET  /api/jobs/<id>/get/<f>     下载成片
    GET  /media/thumbs/<f>          模板缩略图
    GET  /media/library/<f>         内置配图

产物写在 `~/VideoStudio`（可用 VIDEO_STUDIO_WORKSPACE 改），skill 目录保持干净。
"""

from pathlib import Path
import json
import mimetypes
import queue
import re
import shutil
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import paths                                          # noqa: E402
import pipeline                                       # noqa: E402

PROJECT = paths.ASSETS
JOBS = paths.JOBS
UPLOADS = paths.UPLOADS
WEB = paths.WEB
TPL_JSON = paths.TPL_JSON
THUMBS = paths.THUMBS
LIBRARY = paths.LIBRARY

paths.ensure_workspace()

# /media/<prefix>/<file> 的路由表：模板缩略图与内置配图各挂一个前缀
MEDIA_ROOTS = {"thumbs": THUMBS, "library": LIBRARY}

# 渲染很吃 CPU，任务串行排队，先来先做
QUEUE = queue.Queue()
RUNNING = {"id": None}


def worker():
    while True:
        job_id, opts = QUEUE.get()
        RUNNING["id"] = job_id
        job_dir = JOBS / job_id
        status = pipeline.Status(job_dir / "status.json", len(opts["styles"]))
        logfile = open(job_dir / "log.txt", "a", encoding="utf-8")

        def log(msg):
            line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
            logfile.write(line + "\n")
            logfile.flush()
            print(line, flush=True)

        status.write()
        try:
            res = pipeline.run_job(job_dir, opts, log=log, status=status)
            status.finish(res["outputs"])
        except Exception as e:                        # noqa: BLE001
            status.fail(str(e))
            log("FAILED: %s\n%s" % (e, traceback.format_exc()))
        finally:
            logfile.close()
            RUNNING["id"] = None
            QUEUE.task_done()


threading.Thread(target=worker, daemon=True).start()


# ================================================================== 工具
def _thumb_url(sid):
    """模板缩略图：优先压缩过的 jpg，其次预览截图，都没有就返回 None。"""
    for pat in ("%s.jpg" % sid, "%s.png" % sid, "%s_009.5s.png" % sid):
        p = THUMBS / pat
        if p.exists():
            return "/media/thumbs/" + p.name
    return None


def _lib_urls(sid, n=3):
    """这套模板的内置配图（压缩后的 jpg 优先）。"""
    files = [p for p in sorted(LIBRARY.glob("%s_*" % sid))
             if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
    return ["/media/library/" + p.name for p in files[:n]]


def templates_meta():
    cfg = json.loads(TPL_JSON.read_text(encoding="utf-8"))
    out = []
    for s in cfg["styles"]:
        sid = s["id"]
        out.append({
            "num": s["num"], "id": sid, "name": s["name"],
            "group": s.get("group", ""), "imageStyle": s.get("imageStyle", ""),
            "thumb": _thumb_url(sid),
            "images": _lib_urls(sid),
        })
    return {"templates": out, "brand": cfg.get("brand", {}),
            "keywords": cfg.get("keywords", []),
            "imagePolicy": cfg.get("imagePolicy", {}),
            "workspace": str(paths.WORKSPACE)}


def safe_child(root, rel):
    """把相对路径限制在 root 内，挡住 ../ 穿越。"""
    p = (root / rel).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return None
    return p


def fix_encoding(s):
    """HTTP 请求行是按 latin-1 解出来的；客户端若直接塞了原始 UTF-8 字节，
    这里再按 utf-8 解一次，避免中文文件名变成乱码。"""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


class Handler(BaseHTTPRequestHandler):
    server_version = "OpusStudio/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):                # 静音访问日志
        pass

    # ---------------------------------------------------------- helpers
    def _send(self, code, body=b"", ctype="application/octet-stream", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def serve_file(self, path, download=False, ctype=None):
        if not path or not path.exists() or not path.is_file():
            return self.json({"error": "not found"}, 404)
        size = path.stat().st_size
        ctype = ctype or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.end_headers()
                    return
                code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        if download:
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % path.name)
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(262144, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    # ---------------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)

        if path in ("/", "/index.html"):
            return self.serve_file(WEB / "index.html", ctype="text/html; charset=utf-8")

        if path == "/api/templates":
            return self.json(templates_meta())

        if path == "/api/jobs":
            items = []
            for d in sorted(JOBS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                sp = d / "status.json"
                if sp.exists():
                    st = json.loads(sp.read_text(encoding="utf-8"))
                    items.append({"id": d.name, "state": st.get("state"),
                                  "pct": st.get("pct"), "message": st.get("message"),
                                  "outputs": st.get("outputs", []),
                                  "mtime": d.stat().st_mtime})
            return self.json({"jobs": items[:30], "running": RUNNING["id"],
                              "queued": QUEUE.qsize()})

        m = re.match(r"^/api/jobs/([^/]+)$", path)
        if m:
            job_dir = JOBS / m.group(1)
            sp = job_dir / "status.json"
            if not sp.exists():
                return self.json({"error": "no such job"}, 404)
            st = json.loads(sp.read_text(encoding="utf-8"))
            jp = job_dir / "job.json"
            st["job"] = json.loads(jp.read_text(encoding="utf-8")) if jp.exists() else {}
            if st.get("state") == "done":
                outs = []
                for o in st.get("outputs", []):
                    o = dict(o, url="/api/jobs/%s/view/%s" % (m.group(1), o["file"]),
                             download="/api/jobs/%s/get/%s" % (m.group(1), o["file"]))
                    o["covers"] = [dict(c, url="/api/jobs/%s/view/%s" % (m.group(1), c["file"]),
                                        download="/api/jobs/%s/get/%s" % (m.group(1), c["file"]))
                                   for c in (o.get("covers") or [])]
                    outs.append(o)
                st["outputs"] = outs
            else:
                st["queuedAhead"] = QUEUE.qsize()
            return self.json(st)

        m = re.match(r"^/api/uploads/([^/]+)/(.+)$", path)
        if m:
            f = safe_child(UPLOADS / m.group(1), unquote(m.group(2)))
            return self.serve_file(f)

        m = re.match(r"^/api/jobs/([^/]+)/(view|get)/(.+)$", path)
        if m:
            job_dir = JOBS / m.group(1)
            f = safe_child(job_dir, unquote(m.group(3)))
            return self.serve_file(f, download=(m.group(2) == "get"))

        if path.startswith("/media/"):
            rel = unquote(path[len("/media/"):])
            head, _, rest = rel.partition("/")
            root = MEDIA_ROOTS.get(head)
            if root is not None and rest:
                return self.serve_file(safe_child(root, rest))
            # 旧工程布局的兜底：media/<相对工程根的路径>
            return self.serve_file(safe_child(PROJECT, rel))

        return self.json({"error": "not found"}, 404)

    # ---------------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)

        # 原始字节直传，省掉 multipart 解析
        if path == "/api/upload":
            name = fix_encoding((q.get("name") or ["video.mp4"])[0])
            name = re.sub(r"[^\w.\-]+", "_", name)[-80:] or "video.mp4"
            data = self.body()
            if not data:
                return self.json({"error": "空文件"}, 400)
            uid = "u_" + time.strftime("%m%d_%H%M%S")
            d = UPLOADS / uid
            d.mkdir(parents=True, exist_ok=True)
            f = d / name
            f.write_bytes(data)
            try:
                info = pipeline.probe(f)
            except Exception as e:                    # noqa: BLE001
                return self.json({"error": "无法解析这个视频：%s" % e}, 400)
            return self.json({"uploadId": uid, "name": name, "size": len(data),
                              "duration": info["duration"],
                              "width": info["width"], "height": info["height"],
                              "hasAudio": info["hasAudio"],
                              "url": "/api/uploads/%s/%s" % (uid, name)})

        if path == "/api/jobs":
            try:
                req = json.loads(self.body() or b"{}")
            except json.JSONDecodeError:
                return self.json({"error": "bad json"}, 400)
            uid = req.get("uploadId")
            styles = [s for s in (req.get("styles") or [])]
            known = {s["id"] for s in json.loads(TPL_JSON.read_text(encoding="utf-8"))["styles"]}
            styles = [s for s in styles if s in known]
            if not uid or not styles:
                return self.json({"error": "缺上传文件或没选模板"}, 400)
            src = UPLOADS / uid
            files = [p for p in src.iterdir() if p.is_file()] if src.exists() else []
            if not files:
                return self.json({"error": "上传的文件不见了，请重新上传"}, 400)

            jid = "j_" + time.strftime("%m%d_%H%M%S")
            job_dir = JOBS / jid
            job_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(files[0], job_dir / "input.mp4")

            opts = {
                "styles": styles,
                "script": req.get("script", ""),
                "titles": [t for t in (req.get("titles") or []) if t.strip()],
                "brand": req.get("brand") or {},
                "keywords": req.get("keywords") or None,
                "imagePos": req.get("imagePos", "auto"),
                "images": bool(req.get("images", True)),
                "imageOpacity": req.get("imageOpacity", 0.88),
                "pip": req.get("pip", "auto"),
                "zoom": float(req.get("zoom", 0.075) or 0),
                "pipR": int(req.get("pipR", 150) or 150),
                "covers": bool(req.get("covers", True)),
                "ratios": req.get("coverRatios") or ["3x4", "9x16"],
                "layouts": req.get("coverLayouts") or ["bottom"],
                "big": req.get("coverBig", ""),
                "small": req.get("coverSmall", ""),
            }
            (job_dir / "job.json").write_text(
                json.dumps(opts, ensure_ascii=False, indent=2), encoding="utf-8")
            pipeline.Status(job_dir / "status.json", len(styles)).set(
                step="queued", message="排队中", pct=0)
            QUEUE.put((jid, opts))
            return self.json({"jobId": jid, "queued": QUEUE.qsize()})

        return self.json({"error": "not found"}, 404)


def main():
    import argparse
    import errno
    import webbrowser
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8760)
    ap.add_argument("--host", default="127.0.0.1",
                    help="默认只监听本机。改成 0.0.0.0 会让同网段的人都能访问，且服务无鉴权。")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    a = ap.parse_args()

    try:
        srv = ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, errno.EACCES):
            print("端口 %d 已被占用（可能已经有一个工作台在跑）。" % a.port)
            print("换个端口：python3 server.py --port=%d --open" % (a.port + 1))
            print("或先停掉旧的：lsof -ti tcp:%d | xargs kill" % a.port)
            sys.exit(1)
        raise

    url = "http://%s:%d/" % (a.host, a.port)
    print("剪辑工作流已启动：%s" % url)
    print("工作区：%s" % paths.WORKSPACE)
    print("把视频拖进页面 → 选序号 → 开始生成。Ctrl+C 退出。")
    if a.host not in ("127.0.0.1", "localhost"):
        print("⚠ 服务暴露在 %s，无鉴权，请确认网络环境可控。" % a.host)
    if a.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
