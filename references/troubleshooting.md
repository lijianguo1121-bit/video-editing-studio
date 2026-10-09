# 排错手册

先跑 `python3 scripts/doctor.py`——八成的环境问题它直接指出来。

---

## 一、装不上的问题

### `ModuleNotFoundError: No module named 'playwright' / 'numpy'`

建议先建虚拟环境，避免污染系统 Python：

```bash
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install numpy playwright imageio-ffmpeg
python3 -m playwright install chromium
```

macOS 上 `pip install` 报 `EEXIST` 时，先换临时目录再装：

```bash
export TMPDIR=$(mktemp -d) && python3 -m pip install numpy playwright imageio-ffmpeg
```

### 找不到 ffmpeg

本流水线**不需要单独装 ffmpeg**，它用 `imageio-ffmpeg` 自带的静态二进制。
只有 `imageio-ffmpeg` 也没装时才会去找系统 `ffmpeg`。

### 端口 8760 被占

```bash
python3 server.py --port=8761
```

### 磁盘不够

一条 20 秒视频约需 200-400MB，60 秒视频 0.5-1.5GB（中间动效层是大头）。
换盘：

```bash
export VIDEO_STUDIO_WORKSPACE=/Volumes/大硬盘/VideoStudio
python3 server.py --open
```

### 沙箱环境里 `mkdir(exist_ok=True)` 报 `PermissionError: EEXIST`

某些受限沙箱的 shim 在目录已存在时也会抛错。脚本里已经统一改成
「先 `exists()` 判断再建」，如果你自己加代码，注意别直接 `mkdir(exist_ok=True)`。

---

## 二、画面问题

### 视频里没有配图

按顺序查：

1. `jobs/<id>/images/` 里有没有图？——没有说明这**套模板没有内置配图**，
   且任务里也没提供。解决办法见下一条。
2. 有图但画面不显示 → 早期版本的坑：图被引用的是**绝对路径或库内路径**，
   浏览器在任务目录下找不到就 404，然后自动隐藏。现在的版本会把选中的图
   **复制进任务目录**再引用，正常情况下不会再出现。
3. 关掉了开关？命令行 `--no-images` / 网页上的「自动配图」没勾。

补配图的两种方式：

- **用内置库**：把图放到 `assets/library/<style>_01.jpg` 起（建议 448×672），
  命名必须匹配 `^<style>_\d+\.(png|jpg|webp)$`。
- **给单条视频定制**：按 `jobs/<id>/image_requests.json` 里的提示词生图，
  命名成 `<style>_NN.png` 放进 `jobs/<id>/images/`，重跑。**任务内的图优先级最高。**

### 配图挡住了标题或人脸

默认的 `auto` 会按每套模板的版面自动选落点（`templates.json` 里每套的 `imageLayer.xy`），
正常情况下不会压到标题和人脸。如果换了模板、改了版式，或者你的视频里人物站位特殊：

```bash
python3 preview.py --job=我的任务 --styles=promo --pos=tr        # 强制换右上，几秒出单帧
python3 preview.py --job=我的任务 --styles=promo --size=150x200  # 卡片再小一点
```

确认合适后，把坐标写回 `templates.json` 该套的 `imageLayer.xy`，之后一直是这个位置。
实在挤不开就把配图层关掉：`--no-images`。

![修复前后对比](../docs/image-position-fix.jpg)

### 字幕变成豆腐块 / 一片空白

无头 Chromium **不渲染 emoji**。所有模板一律用纯文字，别在文案里塞 emoji。

### 中文显示成方框

模板按 macOS 常见中文字体族名回退：
`PingFang SC` → `Hiragino Sans GB` → `Songti SC` → `STSong`。
Linux 上跑的话装上 `Noto Sans CJK SC` 并把字体名加进对应 `#L_<id>` 里。

### 画面糊、人像被盖住

早期几个模板用了整屏纸质感遮罩，不透明度太高会糊住人脸。
手账/杂志类的遮罩不透明度已经压到 0.3-0.45。自己加模板时注意这点。

### 动效元素压在人脸上

竖屏安全区：人脸大致在 **y 240-800**（720×1280 画布）。霓虹光带、HUD 路径节点
一律要避开这个区间——想放就往下挪到胸口区。

---

## 三、声音问题

### 原声被改了 / 声音和口型对不上

**这是走错链路了。** 云端视频重生成模型只拿到一张静帧，拿不到原片音轨，
必然重新生成画面与声音 → 换脸换声。要保原声只能用本流水线的方式（原片做底 + 动效层叠加）。

### 成片里听不到原声 / 只有配乐

检查 `jobs/<id>/log.txt` 里的「原声保留」数值：

- `nan` 或明显偏低 → 原片**本来就没有音轨**（用 `python3 -c` 调 `pipeline.probe()` 确认 `hasAudio`）。
- 原片有音轨但成片没有 → 底片那一步 `-map 0:a` 没生效，看 log 里底片那行有没有报错。

### 配乐太吵 / 听不清人声

合成里的侧链压缩参数在 `pipeline.py` 的 `build_premix()`：

```python
"[1:a]...volume=0.36[mus];"     # BGM 基础音量
"[bed][voice]sidechaincompress=threshold=0.045:ratio=9:attack=8:release=420[duck];"
```

BGM 调小就把 `0.36` 降到 `0.28`；人声一停配乐回不来，就把 `release=420` 调小。

### 成片音量忽大忽小 / 爆音

响度补偿是**先测量后补增益**的两步法（目标 -15 LUFS + `alimiter` 限幅到 -1.7dBFS）。
如果原片本身动态极大（比如有喊麦），可以把 `pipeline.py` 里 `gain` 的上限从 `7.5` 调小。

---

## 四、性能与失败

### 任务失败：`动效层编码失败（ffmpeg rc=...）`

先看 `jobs/<id>/log.txt` 末尾。常见原因：磁盘满、chromium 崩溃、原片时长异常（过大）。
极长的视频（>10 分钟）会一次性生成几万帧，建议先剪短或按段处理。

### 任务卡在「渲染动效帧」

逐帧渲染本身就是慢：20 秒视频一套模板约 1-2 分钟，60 秒约 4-6 分钟，机器越差越慢。
**不要开多个任务并行**——渲染是 CPU 密集的，并行只会互相拖慢甚至拖崩浏览器。
服务端已经用单 worker 串行排队，网页上能看到"前面还有 N 个排队"。

### 收尾时任务失败，报删除相关的错

早期版本逐帧落地 PNG 再批量删目录，会触发系统对批量删除的保护。
现在渲染时直接把帧**管道给编码器**，只落一个带 alpha 的中间文件，用完删单个文件。
如果你回退到了旧写法，记得改回来。

### 成片时长不对

`amix` 用了 `duration=first`、`overlay` 用了 `eof_action=pass`，
输出长度由 `-t duration` 精确锁定。若不对，检查原片探测到的时长是否准确
（`ffmpeg -i` 的 `Duration` 行）。

---

## 五、开发时容易踩的

| 现象 | 原因 / 解法 |
|---|---|
| 页面数据加载不出来 | `file://` 下 `fetch()` 被禁。浏览器读的是 `templates.js`（`window.__TPL = {...}`），改内容要改 `templates.json` 并重新生成 js |
| 改了 `gallery.html` 但成片没变 | 渲染用的是任务目录里**复制过去的那一份**。重跑任务，或用 `preview.py`（它每次都会同步最新的渲染页） |
| `overlay=eof_action=closest` 报错 | 非法值，用 `pass` |
| `loudnorm` 单次结果不稳定 | 用两步法：先测量再补增益 + 限幅 |
| 配图卡片在浅色/亮色模板上看不清 | 卡片带独立阴影与外发光，边框 1.1px，不透明度 0.88；亮底模板配 `--image-pos=tr` 更清爽 |
