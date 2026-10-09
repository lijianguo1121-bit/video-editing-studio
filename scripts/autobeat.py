#!/usr/bin/env python3
"""自动分节拍 + 配图规划（Auto beat / image planner）。

把任意一段口播视频切成若干"节拍"，再按行业惯例决定这段视频该配几张图。

分节拍优先级：
    1. titles —— 用户在前端一行一条填的分镜标题（最听人话）
    2. script —— 粘进来的口播文案，按标点切句后合成段
    3. 都没有 —— 按时长均分（20s 约 5 拍，60s 约 8 拍）

配图张数（用户口径）：
    30 秒 → 3-5 张      60 秒 → 6-10 张
    换算密度约每 7.5 秒一张，最少 3 张、最多 12 张。
"""

from pathlib import Path
import math
import re

# 无文案时的通用空镜池，保证画面不空
FILLER_SUBJECTS = [
    "城市清晨的空镜，写字楼玻璃幕墙反射晨光",
    "明亮的联合办公空间，长桌、屏幕与绿植",
    "桌面特写，笔记本电脑与手写笔记",
    "城市街道的人流与车流，轻微运动模糊",
    "手机屏幕特写，界面滑动中的手势",
    "傍晚暖光下的店铺门头与往来客人",
    "数据可视化界面特写，图表与折线",
    "收尾镜头，暖色调的逆光剪影",
]

TAG_POOL = ["开场", "背景", "要点", "拆解", "方案", "进度", "行动", "收尾"]

SENT_END = re.compile(r"[。！？!?；;\n]+")
CLAUSE = re.compile(r"[，,、]+")


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _beats_for(duration):
    """未提供任何文案时，按每 4.2 秒一拍切分，夹在 [4, 8] 拍。"""
    return int(_clamp(round(duration / 4.2), 4, 8))


def _sentences(script):
    parts = [s.strip() for s in SENT_END.split(script or "") if s.strip()]
    return parts


def _balance(sentences, target):
    """把句子合并成 target 段，尽量让每段的字数均衡。"""
    if target >= len(sentences):
        return sentences
    total = sum(len(s) for s in sentences)
    per = total / target
    out, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) > per * 1.15 and len(out) < target - 1:
            out.append(cur)
            cur = s
        else:
            cur = (cur + "，" + s) if cur else s
    if cur:
        out.append(cur)
    while len(out) > target:                      # 兜底：把最尾几段并起来
        out[-2] = out[-2] + "，" + out[-1]
        out.pop()
    return out


def _title_of(text, limit=12):
    """从一段话里挑个像标题的短句。"""
    head = CLAUSE.split(text)[0].strip()
    if len(head) <= limit:
        return head
    return head[:limit].rstrip("，,。.")


def make_beats(duration, script=None, titles=None):
    """产出节拍列表：[{t0,t1,tag,title,sub,en,img}]"""
    titles = [t.strip() for t in (titles or []) if t and t.strip()]

    if titles:
        segs = titles
        subs = [""] * len(titles)
    elif script and _sentences(script):
        n = _clamp(_beats_for(duration), 4, 8)
        segs = _sentences(script)
        if len(segs) < n:                 # 句子太少就按逗号再切细，画面节奏才够
            finer = []
            for s in segs:
                parts = [p.strip() for p in CLAUSE.split(s) if p.strip()]
                finer.extend(parts if len(parts) > 1 else [s])
            segs = finer
        segs = _balance(segs, n)
        subs = [s if s != _title_of(s) else "" for s in segs]
    else:
        n = _beats_for(duration)
        segs = ["片段 %02d" % (i + 1) for i in range(n)]
        subs = [FILLER_SUBJECTS[i % len(FILLER_SUBJECTS)] for i in range(n)]

    k = len(segs)
    span = duration / k
    beats = []
    for i, text in enumerate(segs):
        t0 = round(i * span, 2)
        t1 = round(duration if i == k - 1 else (i + 1) * span, 2)
        tag = TAG_POOL[min(len(TAG_POOL) - 1,
                           int(i * len(TAG_POOL) / max(1, k)))]
        title = _title_of(text)
        sub = (subs[i] if i < len(subs) else "") or ""
        if len(sub) > 22:
            sub = sub[:22].rstrip("，,。.")
        beats.append({
            "t0": t0,
            "t1": t1,
            "tag": tag,
            "title": title,
            "sub": sub or ("%s · %02d" % (tag, i + 1)),
            "en": "SCENE %02d" % (i + 1),
            "img": (subs[i] if i < len(subs) and subs[i] else
                    FILLER_SUBJECTS[i % len(FILLER_SUBJECTS)]),
        })
    return beats


def images_for(duration, beats, policy=None):
    """配图计划：几张、各自在什么时间点、每张拍什么。"""
    policy = policy or {}
    spi = policy.get("secondsPerImage", 7.5)
    lo = policy.get("minImages", 3)
    hi = policy.get("maxImages", 12)

    n = int(_clamp(round(duration / spi), lo, hi))
    plan = []
    for k in range(n):
        # 时间点在片头稍后开始，均匀铺满全片
        t = round(duration * (k + 0.7) / (n + 0.4), 2)
        bi = 0
        for i, b in enumerate(beats):
            if t >= b["t0"]:
                bi = i
        b = beats[bi]
        plan.append({
            "idx": k + 1,
            "t": t,
            "beat": bi + 1,
            "title": b["title"],
            "subject": b.get("img") or b["title"],
        })
    return plan, {
        "imagesPerStyle": n,
        "secondsPerImage": spi,
        "rule": "%.1fs ÷ %.1fs/张 ≈ %d 张，夹在 [%d, %d] → %d 张"
                % (duration, spi, round(duration / spi), lo, hi, n),
        "table": [
            {"range": "≤ 30s", "images": "3 - 5"},
            {"range": "≈ 60s", "images": "6 - 10"},
            {"range": "≈ 3min", "images": "12 - 20"},
        ],
    }


if __name__ == "__main__":
    import json
    import sys

    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 20.067
    sc = sys.argv[2] if len(sys.argv) > 2 else None
    bs = make_beats(dur, sc)
    imgs, meta = images_for(dur, bs)
    print("时长 %.2fs → %d 拍，每套配图 %d 张（%s）"
          % (dur, len(bs), meta["imagesPerStyle"], meta["rule"]))
    for b in bs:
        print("  [%5.2f-%5.2f] %s | %s | %s" % (b["t0"], b["t1"], b["tag"], b["title"], b["sub"]))
    print(json.dumps(imgs, ensure_ascii=False, indent=1))
