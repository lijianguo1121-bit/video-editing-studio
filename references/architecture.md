# 技术架构：为什么这样设计

## 一句话

**原片做底，网页动效层叠上去。** 口播原声与口型来自原片本身，动效/配乐/配图全是后加的，
所以不存在"AI 换脸换声"的问题。

## 全景

```
        ┌─────────────┐
上传 →  │  探测 probe  │  时长 / 分辨率 / 有无音轨（只读 ffmpeg 输出，不引第三方库）
        └──────┬──────┘
               ▼
        ┌─────────────────────┐
        │ 自动分节拍 autobeat  │  titles > script > 按时长均分；同时算配图张数
        └──────┬──────────────┘
               ├──────────────► 配图规划 image_requests.json（每张一句生图提示词）
               ▼
        ┌─────────────┐
        │  底片 base   │  统一到 720×1280 / 30fps / H.264；原声音轨原样带过
        └──────┬──────┘
               ▼
        ┌─────────────┐
        │ 配乐 music_gen│  按模板各自的 BPM/和弦/音色，从零合成音乐轨 + 音效轨
        └──────┬──────┘
               ▼
        ┌──────────────────────────┐
        │ 渲染动效层 render（Playwright）│  逐帧截图 → 直接管道给 ffmpeg → 一个带 alpha 的 .mov
        └──────┬───────────────────┘
               ▼
        ┌────────────────────────────┐
        │ 合成 mux（FFmpeg 单次 filtergraph）│  调色 + overlay 动效层 + 侧链压乐 + 响度补偿
        └──────┬─────────────────────┘
               ▼
        ┌─────────────┐
        │ 质检 verify  │  成片与原片做音频包络互相关，确认原声没被动过
        └─────────────┘
```

## 逐帧渲染为什么走管道

最初的写法是「截图 → 写 `frame_000123.png` → 全部写完 → ffmpeg 读序列 → 删目录」。
两个问题：一段 20 秒视频就是 602 张 PNG（约 147MB），而且**收尾时批量删除会触发系统保护、
任务直接失败**。

现在改成：`page.screenshot()` 拿到的 PNG 字节**直接写进 ffmpeg 的 stdin**，
ffmpeg 用 `-f image2pipe -vcodec png` 接，编成 `qtrle/argb` 的带 alpha 通道 mov。
跑完只删一个文件。更快、不占盘、也不会被拦。

## 合成用的 filtergraph

```
[0:a] 原声音轨 → 高通 75Hz → 压缩器（-22dB / 3.5:1）        → voice
[1:a] BGM     → volume 0.36                                  → mus
[2:a] 音效     → volume 0.48                                 → q
        mus + q → amix（bed）
        bed 经 voice 侧链压缩（threshold 0.045 / ratio 9）    → duck
        voice + duck → amix(duration=first) → 立体声 48kHz    → aout

[0:v] 底片 → 调色（每套模板一个影调）                          → graded
[1:v] 动效层 mov → format=rgba                                 → ov
        graded + ov → overlay(eof_action=pass) → yuv420p       → vout
```

### 带运镜 / 圆圈画中画时的画面链

```
[cv] color=black:s=720x1280                              固定黑画布（主输入，时间轴由它定）
[0:v] 调色 → scale=w='trunc(720*k(t)/2)*2':eval=frame
              → pad=776x1378:0:0:color=black            撑到固定画布，位置交给 overlay
[1:v] 动效层 mov（含 B-roll 挖洞与圆环）
        [cv] + [vids] → overlay(x=cx(t)-ax*k(t), y=cy(t)-ay*k(t)) → cam
        [cam] + [ov]  → overlay(0,0, eof_action=pass) → yuv420p → vout
```

要点：
- **`pad` 的 `x/y` 不接受时间变量 `t`**（直接报 `Undefined constant`），所以位置必须交给 `overlay`。
- 偏移公式别漏项：锚点 `(ax,ay)` 要落在 `(cx,cy)`，偏移是 `cx - ax*k`、`cy - ay*k`。
  漏掉 `-ax*k` 就会把画面推出画外，表现为**整屏全黑**。
- 画布尺寸取 `kmax` 对应的最大画面（`776x1378`），保证 overlay 只做平移、永不裁掉帧。
- 所有尺寸/偏移都写成 `2*floor((...)/2)` 取偶，避免 yuv420p 色度重采样。
- 单条表达式超过约 2000 字符后 ffmpeg 报 `Invalid argument`：段数多时简单线性段用
  `max(0,min(...,1))`，只有圆圈段才用 smoothstep 缓动。

要点：
- **侧链压缩**让配乐在人声出现时自动退让，人声一停就回来，不用手调音量曲线。
- `amix` 用 `duration=first`，保证输出长度精确等于原片长度。
- `eof_action=pass`：动效层比原片短时不报错、直接透传（早期用 `closest` 是非法值会炸）。

> 注意：`eof_action=pass` 只适用于**动效层与原片等长**的成片链路。
> `preview.py` 的单帧对比里动效层只有一帧（pts=0），必须用默认的 `repeat`，
> 否则会被判定成已 EOF，主画面直接透传、动效层消失。

## 圆圈画中画：两侧同一份关键帧

`camera.py` 产出唯一一份 segs（关键帧）与 pips（圆圈段），ffmpeg 侧与浏览器侧各自解释，
用**时间**当唯一真值，避免两条实现漂移：

| 侧 | 做什么 |
|---|---|
| ffmpeg | `scale(eval=frame)` 按 `k(t)` 改尺寸，`overlay` 的 `x/y` 按 `(cx,cy)` 搬位 |
| 浏览器 | 用同一份 segs 算 `k(t)` 与圆心，在 B-roll 图上 `mask-image: radial-gradient(circle at cx cy, transparent 0 → transparent (hole-2.2)px → black hole px)` 挖洞 |

洞半径恒 ≤ 缩略画面的内切圆半径（`inset*k*0.995`），所以**永远露不出 ffmpeg 的黑底**。
圆环是 `stage` 上另一层 `.g-pipring`（`box-shadow` 三环 + 投影），紧贴洞边。

**同一个洞要开在两层上**：B-roll 压在模板层下面（这样模板的文字/UI 始终盖在配图上），
而模板自己也常有近乎不透明的面板（`glass` 在画面左上角就有一块 α≈0.98 的玻璃），
正好会把圆圈里的人脸盖住。所以 `mkBroll` 会拿到模板层 `T.host`，
在模板层上打**同一个** `mask-image` —— 圆洞才能真正透到最下面的主画面。
不播圆圈时把两层的 `mask-image` 一起清成 `none`。

配图层也要避让：圆圈圆心就是从该套模板的配图落点（`imageLayer.xy`）算出来的，
所以非圆圈的那张配图卡会和圆圈重叠。`drawBroll` 把当前 B-roll 浓度写到
`window.__BROLLOP`，`drawImgLayer` 拿它乘卡片自身的不透明度 ——
圆圈期间 B-roll 已经在铺图，卡片跟着淡出，交叉淡化比硬切自然。

人脸锚点用 YCbCr 肤色掩膜 + 高斯位置先验估计（纯 NumPy，无模型依赖），
肤色占比太低就退回默认点（画面上中位置）。锚点必须取偶，才和 yuv420p 的色度对齐。

`auto` 模式的两条纪律：开场 1.5s 内不做圆圈（先让人物出镜）；圆圈总时长 ≤ 全片 45%。

**推拉运镜整片只做一次**：圆圈本身已经在放大缩小，再叠加高频推拉会让人晕。
`build_plan` 先算出所有"没有圆圈"的连续区间，挑最长的那段（长度差不多时取靠前的），
对半切成两段 —— 前半 `1.0 → 1+amp`，后半 `1+amp → 1.0`，都用 smoothstep。
整段短于 `ZOOM_MIN_SPAN`(2.5s) 就直接不做。想要两次就 `--zooms=2`。

圆圈里露出多少原片由 `PIP_REGION` 决定（`k = 直径 / PIP_REGION`）：
默认 600 ≈ 头 + 肩，整张脸和肩线都在圈里；调小脸更大但会切到下巴。
注意它和"不漏黑底"是**同向**的：k 越小，画面缩得越多、
`inset*k*0.995` 越大反而越安全；k 接近 1 时（圆圈几乎不缩）才最容易露黑底。

## 封面：首帧 + 文案提炼 + 浏览器排版

`cover.py` 三段解耦，都能单独跑：

1. `grab()` —— 取首帧（默认第 0 秒；这一帧基本是黑的才自动后移）。
2. `summarize()` —— 规则打分挑大字 + 配小字，不调模型，毫秒出结果。
   候选分四类：`line`（节拍标题）/ `sub`（配图空镜描述）/ `brand` / `script`。
   正文（拆解 / 方案 / 行动）比片头片尾（开场 / 背景 / 收尾）得分高；
   纯 ASCII 的力度词卡词边界，否则 `OPC-AID` 里的 `AID` 会被误判成 `AI`。
3. `render()` —— 交给 `assets/cover.html` 排版后截图。字号靠「先定行数再二分字号」撑满可用宽度；
   数字自动上高亮色；默认出 3:4（主页）与 9:16（视频内）。

配置全部落在 `covers/cover_cfg.js`，用户双击 `covers/cover.html` 就能手改重截。

## 响度：两步法

`loudnorm` 的**单次**模式在短视频这种短素材上非常不稳（一次测量直接套用经常过冲）。
所以改成两步：

1. 先跑一遍 `loudnorm=print_format=json` **只测量**，解析出 `input_i` / `input_tp`；
2. 算出补增益 `gain = min(-15.0 - input_i, +7.5)`，套 `volume`，再用
   `alimiter(limit=0.794 ≈ -1.7dBFS, attack=5ms, release=80ms)` 兜住峰值。

目标 **-15 LUFS / 真峰值 -1.7 dBFS / 立体声**——短视频平台的安全区。

## 原声质检：音频包络互相关

```
audio_envelope(path):  ffmpeg 降到 8kHz 单声道 → 100 帧/秒的绝对值包络
verify(base, out):     两条包络去均值后算归一化互相关
```

- ≥ 0.95：原声基本逐样本保留
- 0.90 ~ 0.95：正常（叠加音乐会让包络略有变化）
- < 0.90：**说明原声被动了**，要查是不是走错了链路

实测三条成片：0.947 / 0.941 / 0.947。这个数是流水线自带的质量门禁。

## 配乐是合成出来的，不是素材

`music_gen.py` 用 NumPy 从零合成，没有版权问题、不需要外部素材：

| 参数 | 说明 |
|---|---|
| BPM / 和弦进行 | 每套模板一套，决定情绪（稳重 / 明快 / 紧张 / 温暖…） |
| `flavor` | 音色风格，决定基波与包络（正弦垫 / 锯齿 / 方波 / 拨弦…） |
| `events` | 节拍切换点，在切换处放一个音效（whoosh / tick / hit） |

输出两条 wav：`music_<style>.wav`（铺底音乐）与 `sfx_<style>.wav`（音效点），
在合成阶段分别给 0.36 / 0.48 的音量再混合。

## 20 套模板的复用方式

- 一套模板 = 一段 `#L_<id>` CSS 皮肤 + 一个 `S.<id> = {build, fit, draw}` 绘制函数
- 通用骨架（字幕节拍、进出场、自适应字号）由 `mkBeats/skinBeats/fitBeats/showBeats` 提供
- **配图层实现一次，20 套共享**——所以加模板完全不用管配图逻辑

细节见 `templates.md`。

## 并发与排队

逐帧渲染是 CPU 密集的，**并行只会互相拖慢**（还容易把浏览器渲染进程拖崩）。
所以服务端用一个 `queue.Queue` + 单 worker 线程串行处理任务，
网页上显示"前面还有 N 个排队"。

## 目录与路径解耦

`scripts/paths.py` 把「引擎在哪」与「产物写哪」分开：

- **引擎**：优先 `<skill>/assets/`，其次旧工程根目录（便于在原工程里直接开发）
- **产物**：默认 `~/VideoStudio`，可用 `VIDEO_STUDIO_WORKSPACE` 覆盖

这样 skill 目录本体永远干净，不会被几百兆中间文件污染，换盘也只是改一个环境变量。
