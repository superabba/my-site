"""릴스용 잔잔한 배경음악을 직접 합성한다 (저작권 걱정 없음).

- 부드러운 패드 화음 + 피아노풍 아르페지오 + 낮은 베이스
- 게시물마다(seed) 키·코드 진행·빠르기를 조금씩 바꿔 매번 같은 곡이 되지 않게 한다
- 결과는 44.1kHz 스테레오 16비트 WAV. 리버브·페이드는 인코딩할 때 ffmpeg 필터로 입힌다
"""

from __future__ import annotations

import hashlib
import random
import wave
from pathlib import Path

import numpy as np

SR = 44100

# 잔잔한 느낌의 코드 진행 (C 기준 MIDI 음 번호, 7th/add9 위주)
PROGRESSIONS = [
    [[48, 55, 59, 64], [45, 52, 55, 60], [41, 48, 52, 57], [43, 50, 55, 59]],  # Cmaj7-Am7-Fmaj7-G6
    [[41, 48, 52, 57], [43, 50, 55, 59], [45, 52, 55, 60], [45, 52, 55, 60]],  # Fmaj7-G6-Am7-Am7
    [[48, 55, 62, 64], [43, 50, 57, 59], [45, 52, 59, 60], [41, 48, 55, 57]],  # Cadd9-Gadd9-Am(add9)-Fadd9
    [[45, 52, 55, 60], [41, 48, 52, 57], [48, 55, 59, 64], [43, 50, 55, 62]],  # Am7-Fmaj7-Cmaj7-G(add9)
]
ARP_PATTERN = [0, 1, 2, 3, 2, 1, 3, 2]  # 화음 구성음 순서 (8분음표)


def _hz(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def _seed(text: str) -> int:
    return int(hashlib.sha1(text.encode("utf-8")).hexdigest()[:8], 16)


def _envelope(n: int, attack: float, release: float) -> np.ndarray:
    env = np.ones(n)
    a, r = min(n, int(attack * SR)), min(n, int(release * SR))
    if a:
        env[:a] = np.linspace(0, 1, a) ** 2
    if r:
        env[-r:] *= np.linspace(1, 0, r) ** 2
    return env


def compose(seconds: float, seed: str) -> np.ndarray:
    """(샘플 수, 2) float 배열을 만든다."""
    rng = random.Random(_seed(seed))
    prog = rng.choice(PROGRESSIONS)
    key = rng.choice([-3, -2, 0, 2, 3, 5])  # 조옮김
    bpm = rng.uniform(66, 78)
    beat = 60 / bpm
    chord_len = beat * 4  # 한 화음 = 한 마디

    total = int((seconds + 0.5) * SR)
    t_all = np.arange(total) / SR
    left = np.zeros(total)
    right = np.zeros(total)

    n_chords = int(np.ceil(seconds / chord_len)) + 1
    for ci in range(n_chords):
        chord = [m + key for m in prog[ci % len(prog)]]
        start = int(ci * chord_len * SR)
        if start >= total:
            break

        # 패드: 살짝 어긋나게 맞춘 사인파 두 개로 코러스 느낌, 앞뒤를 겹쳐 부드럽게 연결
        pad_n = min(total - start, int((chord_len + 0.8) * SR))
        t = np.arange(pad_n) / SR
        env = _envelope(pad_n, 0.9, 0.9)
        for i, m in enumerate(chord):
            f = _hz(m)
            tone = (np.sin(2 * np.pi * f * 0.998 * t) + np.sin(2 * np.pi * f * 1.002 * t)
                    + 0.25 * np.sin(2 * np.pi * 2 * f * t))
            pan = 0.35 + 0.3 * (i / (len(chord) - 1))
            sig = 0.045 * env * tone
            left[start:start + pad_n] += sig * (1 - pan)
            right[start:start + pad_n] += sig * pan

        # 베이스: 근음 한 옥타브 아래
        f = _hz(chord[0] - 12)
        bass = 0.09 * env * np.sin(2 * np.pi * f * t)
        left[start:start + pad_n] += bass
        right[start:start + pad_n] += bass

        # 아르페지오: 한 옥타브 위, 피아노처럼 치고 빠르게 줄어드는 소리
        for k, idx in enumerate(ARP_PATTERN):
            s = start + int(k * beat / 2 * SR)
            if s >= total:
                break
            n = min(total - s, int(1.6 * SR))
            tt = np.arange(n) / SR
            f = _hz(chord[idx] + 12)
            decay = np.exp(-tt * 3.2) * _envelope(n, 0.004, 0.05)
            note = (np.sin(2 * np.pi * f * tt) + 0.35 * np.sin(2 * np.pi * 2 * f * tt)
                    + 0.1 * np.sin(2 * np.pi * 3 * f * tt))
            vel = 0.05 * (0.8 + 0.4 * rng.random())
            pan = 0.3 if k % 2 == 0 else 0.7
            left[s:s + n] += vel * decay * note * (1 - pan)
            right[s:s + n] += vel * decay * note * pan

    # 아주 느린 음량 흔들림으로 기계적인 느낌 줄이기
    swell = 1 + 0.06 * np.sin(2 * np.pi * 0.08 * t_all)
    mix = np.stack([left * swell, right * swell], axis=1)
    peak = np.max(np.abs(mix)) or 1.0
    return mix / peak * 0.7


def write_track(path: Path, seconds: float, seed: str) -> Path:
    data = (compose(seconds, seed) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())
    return path


def pick_user_track(source: Path, seed: str) -> Path | None:
    """REELS_AUDIO가 파일이면 그 파일, 폴더면 그 안의 음원 중 하나를 게시물마다 고른다."""
    if source.is_file():
        return source
    if source.is_dir():
        tracks = sorted(p for p in source.iterdir() if p.suffix.lower() in {".mp3", ".m4a", ".wav", ".aac", ".ogg"})
        if tracks:
            return tracks[_seed(seed) % len(tracks)]
    return None
