# 模板系统：20 套皮肤是怎么组织与扩展的

## 三层结构

```
assets/templates.json        ← 唯一可编辑的配置源：文案 / 节拍 / 指标 / 配图风格
assets/gallery.html          ← 渲染页：所有模板的 CSS 皮肤 + 绘制函数 + 配图层
assets/templates.js          ← 由 json 生成的浏览器可读版本（gallery.html 用它）
```

> `templates.js` 是为了绕开 `file://` 下 `fetch()` 被禁的限制。
> 改内容永远改 `templates.json`；`templates.js` 会在每次跑任务时按任务参数重新生成。

## templates.json 字段

| 字段 | 说明 |
|---|---|
| `duration` | 默认时长（实际以每条视频探测到的时长为准） |
| `brand` | 品牌信息：`name` / `city` / `slogan` / `unit` / `issue` / `year`，各模板自行取用 |
| `beats` | 节拍数组，每条 `{t0, t1, tag, title, sub, en, img}` |
| `keywords` | 关键词，部分模板做高亮用 |
| `metrics` | 数据指标 `{label, value, unit, delta}`，仪表盘/研报类模板用 |
| `chart` | 折线柱状数据，仪表盘类模板用 |
| `danmu` | 弹幕池，弹幕类模板循环取用 |
| `imagePolicy` | 通用配图规则（张数密度、上下限） |
| `styles[]` | 20 套模板：`{id, num, name, group, imageStyle}` |

`beats` 的一条长这样：

```json
{
  "t0": 0.3, "t1": 3.3,
  "tag": "起点",
  "title": "深圳 · 宝安",
  "sub": "中国有个地方叫深圳",
  "en": "SHENZHEN · BAO'AN",
  "img": "一座南方沿海城市的清晨天际线……（这张配图的生图描述）"
}
```

> 跑任务时这三项会被自动覆盖：`duration` 用实测值，`beats` 由 `autobeat.py` 重新划分，
> `images` 由配图规划填。所以 `templates.json` 里的 `beats` 只是**默认演示数据**，
> 真正生效的是命令行 `--titles` / `--script` 或网页里填的文案。

## 一套模板 = CSS 皮肤 + 绘制函数

### 1) CSS 皮肤（写在 `gallery.html` 的 `<style>` 里）

共享骨架类：`.g-layer` / `.g-twrap` / `.g-tblock` / `.g-ttl` / `.g-in` / `.g-tag` / `.g-sub` / `.g-en`。
每套模板用 `#L_<id>` 作用域覆盖：

```css
#L_glass{font-family:"PingFang SC",sans-serif;}
#L_glass .g-ttl{font-size:66px;font-weight:800;letter-spacing:1px;color:#F2F7FF;}
#L_glass .g-tag{font-size:20px;letter-spacing:5px;color:rgba(160,215,255,.92);}
```

### 2) 绘制函数（写在 `gallery.html` 的 `S` 对象里）

```js
S.glass = {
  name: '流体玻璃拟态',
  build(host, C) {          // 建 DOM，跑一次。host 是该模板的图层容器
    host.appendChild(el('div', 'glass-blobs'));
    this.beats = mkBeats(host, C, {});   // 字幕节拍组
    skinBeats(this.beats.items, 'glass-card');   // 给节拍套皮肤
  },
  fit(C) { fitBeats(this.beats.items, 520); },   // 字体自适应，字体加载完后跑
  draw(t, C) {              // 每帧调用，t 是秒
    const i = showBeats(this.beats.items, t, C, 24, 0.5);  // 该显示第几拍
    /* 在这里按 t 更新你自己的动画元素 */
  },
};
```

**可用的内置助手**（在 `gallery.html` 里已定义，直接调）：

| 助手 | 作用 |
|---|---|
| `el(tag, cls, text)` | 建元素 |
| `mkBeats(host, C, opts)` | 按 `C.beats` 建字幕组，返回 `{items, ...}` |
| `skinBeats(items, cls)` | 给字幕组统一样式 |
| `fitBeats(items, maxW)` | 自动缩字号到不超宽 |
| `showBeats(items, t, C, y, fade)` | 按时间点显示对应节拍并做进出场，返回当前索引 |

### 3) 注册到 styles 列表

在 `templates.json` 的 `styles[]` 里加一条：

```json
{
  "id": "mycard",
  "num": 21,
  "name": "我的新模板",
  "group": "自定义向",
  "imageStyle": "描述这套配图长什么样的英文提示词"
}
```

`num` 就是网页上显示的编号、也是成片文件名前缀，别重复。

## 加一套新模板的完整步骤

1. 在 `gallery.html` 的 `<style>` 里加 `#L_mycard{...}` 皮肤；
2. 在 `gallery.html` 里加 `S.mycard = { name, build, fit, draw }`；
3. 在 `assets/templates.json` 的 `styles[]` 里加配置（`num: 21`）；
4. 需要缩略图与内置配图的话：
   - 缩略图放 `assets/thumbs/mycard.jpg`（建议 360px 宽）
   - 内置配图放 `assets/library/mycard_01.jpg` 起（建议 448×672）
   - 没有也能跑，页面上会用序号占位块，视频里配图层自动不出图
5. 重启服务，网页刷新即可看到第 21 号卡片。

## 配图层（20 套共享，不用每套单独实现）

配图层在 `gallery.html` 里实现一次，所有模板复用。它读任务配置的：

```json
"images": [ { "idx": 1, "t": 3.2, "file": "images/glass_01.jpg",
              "variant": {"pos": "50% 50%", "zoom": 1.0} } ],
"imageLayer": { "enabled": true, "pos": "tl", "opacity": 0.88 }
```

`imageLayer` 可以写在**顶层**（全局默认），也可以写在**每套 style 里**覆盖它 ——
后者是常态：每套模板的 `imageLayer.xy` 是**按该套版面算出来的绝对落点**，
因为 20 套排版不同，一个固定角落必然有哪套会压标题。

- `t` 是这张图出现的时间点；到点淡入、下一张到点前的 0.5s 淡出，带 Ken Burns 缓动。
- `xy` 优先：`[x, y]` 是卡片左上角在 720×1280 画布上的坐标；
  没有 `xy` 才退回 `pos` 锚点名 `tl / tr / ml / mr / bl / br / top / bottom / center`。
- `size` 是卡片尺寸，默认 `[168, 224]`。位置用锚点时按「锚点 + 18px 边距」自动贴合。
- 落点怎么来的：把模板层（不含底片）预乘 alpha 后算梯度得到「结构密度图」，
  再叠加"实心色块覆盖"和"人脸禁区重叠"两个惩罚项，在 720×1280 上网格搜索最小值。
  全屏均匀遮罩、纯渐变不会被误判成内容。

## 字体与 emoji

无头 Chromium **不渲染 emoji**（会变成豆腐块/空白），所以所有模板一律用纯文字。
中文字体按 macOS 常见族名回退：`PingFang SC` / `Songti SC` / `STSong` / `Hiragino Sans GB`。
