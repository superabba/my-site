"""배경음악: 분위기별로 저작권 걱정 없는 음악을 직접 합성하거나, 사용자의 음악 파일을 길이에 맞춰 씁니다."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .config import MUSIC_DIR
from .ffmpeg import SR, decode_audio

MUSIC_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}

CHORDS = {
    "maj": [0, 4, 7], "min": [0, 3, 7], "maj7": [0, 4, 7, 11], "min7": [0, 3, 7, 10],
    "add9": [0, 4, 7, 14], "sus2": [0, 2, 7], "min9": [0, 3, 7, 14],
}

PRESETS = {
    "calm": dict(bpm=70, key=60, prog=[(5, "maj7"), (4, "min7"), (2, "min7"), (0, "maj7")],
                 arp="quarter", drums=None, bass="long", cutoff=1600, arp_gain=0.20),
    "emotional": dict(bpm=66, key=57, prog=[(0, "min"), (8, "maj7"), (3, "add9"), (10, "sus2")],
                      arp="eighth", drums=None, bass="long", cutoff=1900, arp_gain=0.24),
    "upbeat": dict(bpm=108, key=62, prog=[(0, "add9"), (9, "min7"), (5, "maj7"), (7, "sus2")],
                   arp="eighth", drums="pop", bass="beats", cutoff=2600, arp_gain=0.20),
    "energetic": dict(bpm=124, key=57, prog=[(0, "min"), (8, "maj"), (3, "maj"), (10, "maj")],
                      arp="sixteenth", drums="four", bass="offbeat", cutoff=3200, arp_gain=0.16),
    "mysterious": dict(bpm=76, key=50, prog=[(0, "min9"), (1, "maj7"), (0, "min"), (10, "sus2")],
                       arp="sparse", drums=None, bass="long", cutoff=1300, arp_gain=0.22),
}

_TABLE_N = 4096
_PHASES = np.arange(_TABLE_N) / _TABLE_N
# 부드러운 톱니파(배음 8개) 한 주기
_PAD_TABLE = sum(np.sin(2 * np.pi * k * _PHASES) / k ** 1.4 for k in range(1, 9)).astype(np.float32)
_PAD_TABLE /= np.abs(_PAD_TABLE).max()


def _hz(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def _voicing(key: int, root: int, kind: str) -> list[int]:
    notes = []
    for iv in CHORDS[kind]:
        n = key + root + iv
        while n > key + 10:
            n -= 12
        notes.append(n)
    return sorted(notes)


def _wavetable(freq: float, n: int, phase0: float) -> np.ndarray:
    phase = (phase0 + freq * np.arange(n) / SR) % 1.0
    return _PAD_TABLE[(phase * _TABLE_N).astype(np.int32)]


def _pluck(freq: float, dur: float, decay: float) -> np.ndarray:
    t = np.arange(int(dur * SR)) / SR
    attack = 1 - np.exp(-t * 400)
    sig = (np.sin(2 * np.pi * freq * t) * np.exp(-t * decay)
           + 0.45 * np.sin(2 * np.pi * 2 * freq * t) * np.exp(-t * decay * 1.8)
           + 0.2 * np.sin(2 * np.pi * 3 * freq * t) * np.exp(-t * decay * 2.6))
    return (sig * attack).astype(np.float32)


def _add(buf: np.ndarray, start: int, sig: np.ndarray, gain_l: float = 1.0, gain_r: float = 1.0) -> None:
    if start >= len(buf):
        return
    end = min(len(buf), start + len(sig))
    seg = sig[: end - start]
    buf[start:end, 0] += seg * gain_l
    buf[start:end, 1] += seg * gain_r


def _lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    spec = np.fft.rfft(x, axis=0)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    spec *= (1 / (1 + (f / cutoff) ** 4))[:, None]
    return np.fft.irfft(spec, n=len(x), axis=0).astype(np.float32)


def _reverb(x: np.ndarray, rng: np.random.Generator, seconds: float = 2.2, wet: float = 0.3) -> np.ndarray:
    ir_n = int(seconds * SR)
    t = np.arange(ir_n) / SR
    env = np.exp(-t * 3.2)
    out = np.empty_like(x)
    size = 1 << int(np.ceil(np.log2(len(x) + ir_n)))
    for ch in range(2):
        ir = rng.standard_normal(ir_n) * env
        ir /= np.sqrt(np.sum(ir ** 2))
        y = np.fft.irfft(np.fft.rfft(x[:, ch], size) * np.fft.rfft(ir, size), size)[: len(x)]
        out[:, ch] = y
    return (x * (1 - wet) + out * wet).astype(np.float32)


def _drum_hits(rng: np.random.Generator) -> dict[str, np.ndarray]:
    t = np.arange(int(0.45 * SR)) / SR
    kfreq = 45 + 95 * np.exp(-t * 28)
    kick = np.sin(2 * np.pi * np.cumsum(kfreq) / SR) * np.exp(-t * 7.5)
    ts = np.arange(int(0.25 * SR)) / SR
    noise = rng.standard_normal(len(ts))
    hp = np.diff(noise, prepend=0)
    snare = 0.55 * hp * np.exp(-ts * 22) + 0.4 * np.sin(2 * np.pi * 185 * ts) * np.exp(-ts * 30)
    th = np.arange(int(0.07 * SR)) / SR
    hat = np.diff(rng.standard_normal(len(th)), prepend=0) * np.exp(-th * 85)
    to = np.arange(int(0.28 * SR)) / SR
    ohat = np.diff(rng.standard_normal(len(to)), prepend=0) * np.exp(-to * 16)
    return {k: v.astype(np.float32) for k, v in
            {"kick": kick, "snare": snare, "hat": hat * 0.35, "ohat": ohat * 0.25}.items()}


def generate_bgm(mood: str, duration: float, seed: int = 0) -> np.ndarray:
    """분위기(mood)에 맞는 배경음악을 duration초 길이로 합성합니다. seed를 바꾸면 다른 곡이 됩니다."""
    p = PRESETS.get(mood, PRESETS["calm"])
    rng = np.random.default_rng(seed)
    key = p["key"] + int(rng.integers(-2, 3))
    prog = list(p["prog"])
    if seed:
        shift = int(rng.integers(0, len(prog)))
        prog = prog[shift:] + prog[:shift]

    beat = 60.0 / p["bpm"]
    chord_len = beat * 4
    n = int((duration + 1.0) * SR)
    n_chords = int(np.ceil((duration + 1.0) / chord_len)) + 1

    pad = np.zeros((n, 2), np.float32)
    arp = np.zeros((n, 2), np.float32)
    bass = np.zeros((n, 2), np.float32)
    drums = np.zeros((n, 2), np.float32)
    xfade = int(0.5 * SR)

    for ci in range(n_chords):
        root, kind = prog[ci % len(prog)]
        notes = _voicing(key, root, kind)
        start = int(ci * chord_len * SR)
        seg_n = int(chord_len * SR) + xfade
        if start >= n:
            break
        # 패드: 좌우로 살짝 다르게 조율해 넓게 들리게
        env = np.ones(seg_n, np.float32)
        ramp = np.sin(np.linspace(0, np.pi / 2, xfade)) ** 2
        env[:xfade] = ramp
        env[-xfade:] = ramp[::-1]
        for note in notes:
            f = _hz(note)
            left = _wavetable(f * 2 ** (-6 / 1200), seg_n, rng.random()) + _wavetable(f, seg_n, rng.random())
            right = _wavetable(f * 2 ** (6 / 1200), seg_n, rng.random()) + _wavetable(f, seg_n, rng.random())
            _add(pad, start, left * env * 0.5, 1.0, 0.0)
            _add(pad, start, right * env * 0.5, 0.0, 1.0)

        # 베이스
        bf = _hz(key + root - 24 if key + root - 24 > 33 else key + root - 12)
        if p["bass"] == "long":
            tb = np.arange(seg_n) / SR
            b = (np.sin(2 * np.pi * bf * tb) + 0.25 * np.sin(4 * np.pi * bf * tb)) * env
            _add(bass, start, b.astype(np.float32) * 0.7)
        else:
            step = beat if p["bass"] == "beats" else beat / 2
            offs = 0.0 if p["bass"] == "beats" else beat / 2
            k = 0
            while offs + k * step < chord_len - 1e-6:
                if p["bass"] == "offbeat" and k % 2 == 1:
                    k += 1
                    continue
                tb = np.arange(int(step * 0.95 * SR)) / SR
                b = np.tanh(1.8 * np.sin(2 * np.pi * bf * tb)) * np.exp(-tb * 4) * (1 - np.exp(-tb * 300))
                _add(bass, start + int((offs + k * step) * SR), b.astype(np.float32) * 0.6)
                k += 1

        # 아르페지오(피아노 같은 소리)
        arp_notes = [x + 12 for x in notes] + [notes[0] + 24]
        pattern = p["arp"]
        if pattern == "quarter":
            steps, step = 4, beat
        elif pattern == "eighth":
            steps, step = 8, beat / 2
        elif pattern == "sixteenth":
            steps, step = 16, beat / 4
        else:  # sparse
            steps, step = 4, beat
        order = list(range(len(arp_notes))) + list(range(len(arp_notes) - 2, 0, -1))
        for s in range(steps):
            if pattern == "sparse" and rng.random() < 0.45:
                continue
            note = arp_notes[order[(s + ci) % len(order)]]
            vel = 0.6 + 0.4 * rng.random()
            sig = _pluck(_hz(note), dur=min(2.0, step * 3), decay=3.0 if pattern != "sixteenth" else 7.0)
            pan = 0.5 + 0.3 * np.sin(s * 1.7)
            _add(arp, start + int(s * step * SR), sig * vel, 1 - pan * 0.6, 0.4 + pan * 0.6)

        # 드럼
        if p["drums"]:
            hits = _drum_hits(rng)
            for b_i in range(4):
                bt = start + int(b_i * beat * SR)
                if p["drums"] == "four" or b_i in (0, 2):
                    _add(drums, bt, hits["kick"] * 0.9)
                if b_i in (1, 3):
                    _add(drums, bt, hits["snare"] * 0.55)
                for h in range(2):
                    ht = bt + int(h * beat / 2 * SR)
                    if p["drums"] == "four" and h == 1:
                        _add(drums, ht, hits["ohat"], 0.8, 1.0)
                    else:
                        _add(drums, ht, hits["hat"] * (0.7 + 0.3 * rng.random()), 1.0, 0.8)

    pad = _lowpass(pad, p["cutoff"])
    music = pad * 0.22 + arp * p["arp_gain"]
    music = _reverb(music, rng, wet=0.35 if p["drums"] is None else 0.22)
    music += bass * 0.3 + drums * 0.45
    music = music[: int(duration * SR)]
    return _finish(music)


def _finish(music: np.ndarray, fade_in: float = 0.8, fade_out: float = 1.5) -> np.ndarray:
    # 곡마다 체감 음량이 비슷하도록 평균 음량(RMS)을 맞추고, 튀는 부분은 부드럽게 눌러줍니다.
    rms = float(np.sqrt(np.mean(music ** 2)))
    if rms > 0:
        music = music / rms * 0.2
    music = np.tanh(music * 1.2) / 1.2
    n = len(music)
    fi, fo = min(n, int(fade_in * SR)), min(n, int(fade_out * SR))
    if fi:
        music[:fi] *= np.linspace(0, 1, fi)[:, None]
    if fo:
        music[n - fo:] *= np.linspace(1, 0, fo)[:, None]
    return music.astype(np.float32)


def load_music_file(path: str | Path, duration: float) -> np.ndarray:
    """음악 파일을 읽어 duration초에 맞춥니다. 짧으면 이어 붙여 반복합니다."""
    src = decode_audio(path)
    need = int(duration * SR)
    if len(src) == 0:
        return np.zeros((need, 2), np.float32)
    out = src
    xf = int(1.0 * SR)
    while len(out) < need:
        if len(out) > xf and len(src) > xf:
            ramp = np.linspace(0, 1, xf)[:, None]
            joined = out[-xf:] * (1 - ramp) + src[:xf] * ramp
            out = np.concatenate([out[:-xf], joined, src[xf:]])
        else:
            out = np.concatenate([out, src])
    return _finish(out[:need].copy(), fade_in=0.3, fade_out=1.5)


def list_music_files() -> list[Path]:
    return sorted(p for p in MUSIC_DIR.iterdir() if p.suffix.lower() in MUSIC_EXTS)


def _moving_avg(x: np.ndarray, win: int) -> np.ndarray:
    if win <= 1:
        return x
    c = np.cumsum(np.concatenate([[0.0], x]))
    out = (c[win:] - c[:-win]) / win
    pad = win // 2
    return np.concatenate([np.full(pad, out[0]), out, np.full(len(x) - len(out) - pad, out[-1])])


def mix(voice: np.ndarray, bgm: np.ndarray | None, voice_gain: float, bgm_gain: float, duck: float = 0.35) -> np.ndarray:
    """목소리와 배경음악을 섞습니다. 목소리가 나올 때는 음악을 duck 비율만큼 줄입니다."""
    n = len(voice)
    out = voice * voice_gain
    if bgm is not None and bgm_gain > 0:
        b = np.zeros((n, 2), np.float32)
        b[: min(n, len(bgm))] = bgm[:n]
        level = _moving_avg(np.abs(voice).mean(axis=1), int(0.05 * SR))
        active = (level > 0.01).astype(np.float64)
        env = np.clip(_moving_avg(active, int(0.4 * SR)) * 1.6, 0, 1)
        gain = 1 - (1 - duck) * env
        out = out + b * bgm_gain * gain[:, None].astype(np.float32)
    peak = np.abs(out).max()
    if peak > 0.98:
        out = out / peak * 0.98
    return out.astype(np.float32)
