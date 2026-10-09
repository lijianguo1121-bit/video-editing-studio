#!/usr/bin/env python3
"""配乐生成器 · 20 套模板各一条专属 BGM（Per-template music bed generator）。

每一套剪辑模板都配一条从零合成的配乐 + 音效轨，时长跟着任务视频走。
口播永远是主轨，配乐由 FFmpeg 侧链压制（ducking）垫在下面。

用法：
    # 单套
    python3 music_gen.py --style=crt --duration=20.067 --outdir=/tmp/audio
    # 全部 20 套
    python3 music_gen.py --style=all --duration=31.5 --outdir=/tmp/audio
    # 指定音效触发点（通常是每个节拍的 t0）
    python3 music_gen.py --style=neon --duration=60 --events=0.3,8,16,24,32,40,48,55
"""

from pathlib import Path
import argparse
import sys
import wave

import numpy as np

RATE = 48_000

# ------------------------------------------------------------------ 音色
# pad/bass/pluck/kick/hat 是各层的增益；div 是琶音细分；tail 是空间尾音
FLAVOR = {
    "warm":   dict(pad=.030, bass=.055, pluck=.026, kick=.062, base=72, hat=.0034, tail=1.00, div=2, arp=.20),
    "bright": dict(pad=.025, bass=.075, pluck=.032, kick=.100, base=74, hat=.0034, tail=1.00, div=2, arp=.20),
    "neon":   dict(pad=.036, bass=.088, pluck=.040, kick=.118, base=73, hat=.0052, tail=1.30, div=4, arp=.42),
    "soft":   dict(pad=.026, bass=.038, pluck=.018, kick=.026, base=67, hat=.0018, tail=1.45, div=2, arp=.12),
    "drive":  dict(pad=.028, bass=.082, pluck=.036, kick=.106, base=76, hat=.0046, tail=1.05, div=4, arp=.30),
}

SCALES = {
    "minor":  [0, 2, 3, 5, 7, 8, 10],
    "major":  [0, 2, 4, 5, 7, 9, 11],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
}

# ------------------------------------------------------------------ 20 套风格映射
# num 与 templates.json 的 styles.num 一一对应
STYLES = {
    # 复古终端向
    "crt":       dict(num=1,  flavor="warm",   bpm=104, root=45, scale="minor",  prog=[0, 5, 3, 4]),
    "retro":     dict(num=2,  flavor="soft",   bpm=92,  root=48, scale="dorian", prog=[0, 3, 5, 4]),
    # 数据严谨向
    "dash":      dict(num=3,  flavor="warm",   bpm=116, root=43, scale="minor",  prog=[0, 4, 5, 3]),
    "report":    dict(num=4,  flavor="soft",   bpm=96,  root=46, scale="major",  prog=[0, 3, 4, 0]),
    # 生活亲和向
    "journal":   dict(num=5,  flavor="soft",   bpm=86,  root=50, scale="major",  prog=[0, 5, 3, 4]),
    "chalk":     dict(num=6,  flavor="soft",   bpm=94,  root=48, scale="major",  prog=[0, 4, 5, 3]),
    # 商业热度向
    "promo":     dict(num=7,  flavor="drive",  bpm=132, root=44, scale="minor",  prog=[0, 5, 3, 4]),
    "danmu":     dict(num=8,  flavor="drive",  bpm=126, root=45, scale="minor",  prog=[0, 3, 4, 5]),
    # 出版编辑向
    "magazine":  dict(num=9,  flavor="soft",   bpm=98,  root=47, scale="dorian", prog=[0, 3, 5, 4]),
    "minimal":   dict(num=10, flavor="soft",   bpm=82,  root=48, scale="minor",  prog=[0, 6, 5, 4]),
    # 时事权威向
    "news":      dict(num=11, flavor="warm",   bpm=118, root=41, scale="minor",  prog=[0, 5, 6, 4]),
    # 科技发布向
    "product":   dict(num=12, flavor="neon",   bpm=124, root=45, scale="minor",  prog=[0, 5, 3, 4]),
    # 现代 UI 向
    "glass":     dict(num=13, flavor="warm",   bpm=110, root=48, scale="dorian", prog=[0, 4, 3, 5]),
    # 叙事结构向
    "timeline":  dict(num=14, flavor="warm",   bpm=100, root=46, scale="minor",  prog=[0, 3, 4, 5]),
    # 观点表达向
    "quote":     dict(num=15, flavor="soft",   bpm=84,  root=50, scale="major",  prog=[0, 5, 3, 4]),
    # 音频播客向
    "podcast":   dict(num=16, flavor="warm",   bpm=94,  root=44, scale="dorian", prog=[0, 3, 5, 4]),
    # 生活方式向
    "travel":    dict(num=17, flavor="drive",  bpm=120, root=49, scale="major",  prog=[0, 4, 5, 3]),
    # 专业技术向
    "blueprint": dict(num=18, flavor="warm",   bpm=90,  root=43, scale="minor",  prog=[0, 5, 4, 3]),
    # 品牌视觉向
    "gradient":  dict(num=19, flavor="neon",   bpm=118, root=47, scale="minor",  prog=[0, 3, 5, 4]),
    # 信息流向
    "card":      dict(num=20, flavor="warm",   bpm=112, root=48, scale="dorian", prog=[0, 4, 3, 5]),
}


def midi(n):
    return 440.0 * 2.0 ** ((n - 69) / 12.0)


def smooth(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def write_wav(path, signal):
    peak = float(np.max(np.abs(signal))) or 1.0
    signal = np.clip(signal * min(0.72 / peak, 1.0), -0.999, 0.999)
    pcm = (signal * 32767.0).astype(np.int16)
    if pcm.ndim == 1:
        pcm = np.column_stack((pcm, pcm))
    with wave.open(str(path), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm.astype("<i2").tobytes())


def voicings(root, scale, prog, bars_needed):
    """把级数进程展开成逐小节的和弦音列表，循环补足到需要的长度。"""
    st = SCALES[scale]
    base = []
    for d in prog:
        notes = []
        for step in (0, 2, 4, 6):
            i = d + step
            notes.append(root + st[i % 7] + 12 * (i // 7))
        base.append(notes)
    out = []
    while len(out) < bars_needed:
        out += [list(c) for c in base]
    return out


def synthesize(name, duration, bpm, chords, flavor, events, outdir):
    F = FLAVOR[flavor]
    n = int(RATE * duration)
    if n <= 0:
        raise ValueError("duration must be > 0")
    t = np.arange(n, dtype=np.float64) / RATE
    rng = np.random.default_rng(abs(hash(name)) % (2 ** 31))
    bed = np.zeros(n)
    beat = 60.0 / bpm
    bar = beat * 4

    # --- sustained chord pad, one voicing per bar, soft attack / release
    for bi, chord in enumerate(chords):
        start = bi * bar
        if start >= duration:
            break
        end = min(duration, start + bar + 0.16)
        a, b = int(start * RATE), min(n, int(end * RATE))
        tt = t[a:b] - start
        env = smooth(tt / 0.42) * smooth((end - t[a:b]) / 0.48)
        for note in chord:
            f = midi(note)
            voice = (np.sin(2 * np.pi * f * tt)
                     + 0.27 * np.sin(2 * np.pi * f * 2.006 * tt + 0.17)
                     + 0.09 * np.sin(2 * np.pi * f * 3.01 * tt + 0.31)
                     + 0.16 * np.sin(2 * np.pi * f * 1.003 * tt + 0.5))
            if flavor == "neon":
                voice += F["arp"] * np.sin(2 * np.pi * f * 1.007 * tt + 1.1)
            bed[a:b] += F["pad"] * voice * env

    # --- round bass on the downbeat and beat 3, leaving room for the voice
    for bi, chord in enumerate(chords):
        for k in (0, 2):
            start = bi * bar + k * beat
            if start >= duration:
                continue
            a = int(start * RATE)
            b = min(n, a + int(0.62 * RATE))
            if b <= a:
                continue
            tt = np.arange(b - a) / RATE
            env = np.exp(-tt * (5.8 if flavor in ("bright", "drive") else 4.2))
            if flavor == "neon":
                env = np.exp(-tt * 3.4) * (0.72 + 0.28 * np.cos(2 * np.pi * 4 * tt))
            bed[a:b] += F["bass"] * np.sin(2 * np.pi * midi(chord[0] - 12) * tt) * env

    # --- plucks / arpeggio on the off-beat grid
    scale = [0, 2, 4, 7, 9]
    step = beat / F["div"]
    for k, start in enumerate(np.arange(0.2, duration, step)):
        a = int(start * RATE)
        b = min(n, a + int(0.48 * RATE))
        if b <= a:
            continue
        tt = np.arange(b - a) / RATE
        f = midi(F["base"] + scale[(k + k // 5) % len(scale)])
        env = np.exp(-tt * (7.4 if flavor in ("bright", "drive") else 6.2))
        tone = np.sin(2 * np.pi * f * tt) + 0.2 * np.sin(2 * np.pi * f * 2.01 * tt + 0.2)
        accent = 1.35 if k % 4 == 0 else 0.72
        if flavor == "neon":
            accent = 1.5 if k % 8 == 0 else 0.66
        bed[a:b] += F["pluck"] * accent * tone * env

    # --- kick, clap and hats
    for k, start in enumerate(np.arange(0.0, duration, beat)):
        a = int(start * RATE)
        b = min(n, a + int(0.28 * RATE))
        if b <= a:
            continue
        tt = np.arange(b - a) / RATE
        f = 96.0 - 42.0 * np.clip(tt / 0.28, 0, 1)
        bed[a:b] += F["kick"] * np.sin(2 * np.pi * f * tt) * np.exp(-tt * 13.0)
        if k % 4 in (1, 3):
            b2 = min(n, a + int(0.12 * RATE))
            env2 = np.exp(-np.arange(b2 - a) / (RATE * 0.023))
            bed[a:b2] += (0.016 if flavor == "neon" else 0.011) * rng.normal(0, 1, b2 - a) * env2

    for start in np.arange(beat / 2, duration, beat / 2):
        a = int(start * RATE)
        b = min(n, a + int(0.045 * RATE))
        if b <= a:
            continue
        env = np.exp(-np.arange(b - a) / (RATE * 0.008))
        bed[a:b] += F["hat"] * rng.normal(0, 1, b - a) * env

    # --- small deterministic room tail, then fade in / out
    dry = bed.copy()
    for delay, amount in ((0.105, 0.12), (0.205, 0.07), (0.31, 0.035)):
        s = int(delay * RATE)
        bed[s:] += dry[:-s] * amount * F["tail"]
    bed *= smooth(t / 0.9) * smooth((duration - t) / 1.3)
    bed *= 0.48 / max(float(np.max(np.abs(bed))), 1e-9)

    # --- quiet UI chimes on the narrative beats
    sfx = np.zeros(n)
    for i, start in enumerate(events):
        if start >= duration:
            continue
        a = int(start * RATE)
        b = min(n, a + int(0.68 * RATE))
        if b <= a:
            continue
        tt = np.arange(b - a) / RATE
        f = (740, 988, 1175, 880, 1320)[i % 5]
        chime = np.sin(2 * np.pi * f * tt) + 0.34 * np.sin(2 * np.pi * f * 2.67 * tt + 0.4)
        sfx[a:b] += 0.095 * chime * np.exp(-tt * 7.3)
        c = min(n, a + int(0.045 * RATE))
        sfx[a:c] += 0.02 * rng.normal(0, 1, c - a) * np.exp(-np.arange(c - a) / (RATE * 0.006))
    sfx *= 0.20 / max(float(np.max(np.abs(sfx))), 1e-9)

    # --- a touch of stereo width from a decorrelated delay
    width = 0.009 if flavor != "neon" else 0.014
    s = int(width * RATE)
    right = np.zeros_like(bed)
    right[s:] = bed[:-s] * 0.96
    rs = np.zeros_like(sfx)
    rs[s:] = sfx[:-s] * 0.94

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    write_wav(outdir / f"music_{name}.wav", np.column_stack((bed, right)))
    write_wav(outdir / f"sfx_{name}.wav", np.column_stack((sfx, rs)))
    return outdir / f"music_{name}.wav"


def build(name, duration, events=None, outdir="."):
    """按风格 id 生成一条配乐，返回 (music_path, sfx_path)。"""
    if name not in STYLES:
        raise KeyError(f"unknown style: {name}")
    s = STYLES[name]
    bar = 60.0 / s["bpm"] * 4
    chords = voicings(s["root"], s["scale"], s["prog"], int(duration / bar) + 2)
    if events is None:
        # 没有节拍信息时，按 4 秒一拍打点
        events = list(np.arange(0.4, duration, 4.0))
    synthesize(name, duration, s["bpm"], chords, s["flavor"], list(events), outdir)
    return Path(outdir) / f"music_{name}.wav", Path(outdir) / f"sfx_{name}.wav"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--style", default="all", help="风格 id，或 all")
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--events", default="", help="逗号分隔的音效触发秒数")
    a = ap.parse_args()

    events = [float(x) for x in a.events.split(",") if x.strip()] or None
    names = list(STYLES) if a.style == "all" else [s.strip() for s in a.style.split(",")]
    for nm in names:
        if nm not in STYLES:
            print(f"skip unknown style: {nm}", file=sys.stderr)
            continue
        m, s = build(nm, a.duration, events, a.outdir)
        print(f"  {STYLES[nm]['num']:>2}. {nm:<10} {STYLES[nm]['flavor']:<6} "
              f"{STYLES[nm]['bpm']:>3}bpm  ->  {m.name} + {s.name}", flush=True)


if __name__ == "__main__":
    main()
