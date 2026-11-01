"""분위기별 배경음악: 곡 라이브러리(music/)에서 고르고, 그 분위기 곡이 없으면 합성한다.

곡 라이브러리: music/library.json + music/<분위기>/*.m4a
  Free Music Archive의 CC0·CC BY 연주곡을 tools/fma.py로 골라 70초 발췌·−16 LUFS로 맞춰 둔 것.
  CC BY 곡은 출처 표기가 필요해서 credit()이 만든 한 줄을 게시할 때 캡션 끝에 붙인다.
  곡을 직접 지정하려면 pick_track(..., track="bright/xxx.m4a"), 합성만 쓰려면 track="synth".

분위기 (REELS_AUDIO=auto일 때 슬라이드 글자의 단어로 자동 선택. 걸리는 단어가 없으면 calm):
  calm    잔잔 — 패드 + 피아노풍 아르페지오. 카드뉴스봇에서 복사한 music.py 그대로
  warm    따뜻 — 느긋한 아르페지오 + 셰이커 (지원금·환급·생활 정보)
  bright  경쾌 — 통통 튀는 플럭 + 가벼운 드럼 (특가·할인·이벤트·혜택)
  upbeat  신나는 — 네 박 킥 + 엇박 화음 (선착순·마감 임박·역대급)
  tense   주의 — 단조 + 일정하게 뛰는 베이스 (사기·피싱·함정·손해)

게시물마다(id로 seed) 키·코드 진행·빠르기가 조금씩 바뀐다. 결과는 44.1kHz 스테레오 16비트 WAV이고
리버브·페이드·음량은 인코딩할 때 audio_filter()의 ffmpeg 필터로 입힌다.
"""

from __future__ import annotations

import json
import random
import wave
from pathlib import Path

import numpy as np

from . import music
from .music import SR, _envelope, _hz, _seed

LIBRARY = Path(__file__).resolve().parents[2] / "music"  # 저장소 맨 위 music/ (쇼츠 스튜디오와 같이 씀)
SYNTH = "synth"

# 화음 (C 기준 MIDI 음 번호)
C = [48, 55, 60, 64]
G = [43, 50, 55, 59]
AM = [45, 52, 57, 60]
F = [41, 48, 53, 57]
EM = [40, 47, 52, 55]
DM = [38, 45, 50, 53]
E = [40, 47, 52, 56]  # 단조 진행 끝에서 긴장을 만드는 장3화음

MAJOR = [[C, G, AM, F], [AM, F, C, G], [C, AM, F, G], [F, G, EM, AM]]
MINOR = [[AM, F, DM, E], [AM, G, F, E], [AM, DM, F, E]]

# 패턴은 한 마디 = 16칸(16분음표), '.'은 쉼.
#   arp   화음 구성음 번호 (4·5 = 2·3번째 음의 한 옥타브 위). 실제로는 한 옥타브 올려 친다
#   bass  R 근음, F 5도, O 옥타브 위
#   stab·kick·clap·shaker  x 침 / hat  x 닫힌 하이햇, o 열린 하이햇
# level은 잔잔(calm)과 체감 음량(LUFS)이 비슷해지게 맞춘 dB 값
REST = "." * 16
DEFAULTS = dict(stab=REST, kick=REST, clap=REST, hat=REST, shaker=REST, pump=False,
                stab_lv=0.0, kick_lv=0.0, clap_lv=0.0, hat_lv=0.0, shaker_lv=0.0)
MOODS = {
    "warm": dict(
        level=-3.1,
        progs=music.PROGRESSIONS, keys=(-3, -2, 0, 2, 3, 5), bpm=(82, 90), pad=0.05,
        arp="0.1.2.3.2.1.3.2.", arp_lv=0.07, arp_decay=3.0, arp_tone=0.35,
        bass="R.......F.......", bass_lv=0.12, bass_decay=1.2,
        shaker="..x...x...x...x.", shaker_lv=0.05,
    ),
    "bright": dict(
        level=-1.6,
        progs=MAJOR, keys=(-2, 0, 2, 3, 5), bpm=(104, 112), pad=0.025,
        arp="0.2.3.24.2.35.2.", arp_lv=0.07, arp_decay=6.0, arp_tone=0.5,
        bass="R..R..R.R..R..O.", bass_lv=0.13, bass_decay=5.0,
        kick="x.....x.x.......", kick_lv=0.45, clap="....x.......x...", clap_lv=0.16,
        hat="x.x.x.x.x.x.x.x.", hat_lv=0.05,
    ),
    "upbeat": dict(
        level=-1.6,
        progs=MAJOR, keys=(-2, 0, 2, 3, 5), bpm=(120, 126), pad=0.035, pump=True,
        arp="4..3..2.4..3..2.", arp_lv=0.045, arp_decay=7.0, arp_tone=0.5,
        stab="..x...x...x...x.", stab_lv=0.035,
        bass="..R...R...R...O.", bass_lv=0.14, bass_decay=7.0,
        kick="x...x...x...x...", kick_lv=0.5, clap="....x.......x...", clap_lv=0.16,
        hat="x.o.x.o.x.o.x.o.", hat_lv=0.05,
    ),
    "tense": dict(
        level=-2.4,
        progs=MINOR, keys=(-3, -2, 0, 2), bpm=(88, 96), pad=0.045,
        arp="0.2.0.2.0.3.0.2.", arp_lv=0.05, arp_decay=4.0, arp_tone=0.2,
        bass="R.R.R.R.R.R.R.R.", bass_lv=0.12, bass_decay=9.0,
        kick="x.......x.......", kick_lv=0.4,
        hat="xxxxxxxxxxxxxxxx", hat_lv=0.025,
    ),
}
TARGET_RMS = 0.16

# auto: 제목·장면 글자에서 이 단어들이 몇 번 나오는지 세어 가장 많은 분위기 (같으면 앞쪽)
KEYWORDS = {
    "bright": ("특가", "할인", "이벤트", "혜택", "쿠폰", "무료", "공짜", "적립", "캐시백", "증정", "경품", "한정", "최저가", "세일"),
    "upbeat": ("선착순", "오늘만", "마감", "역대급", "대박", "깜짝", "타임딜", "오픈런"),
    "warm": ("지원금", "환급", "복지", "수당", "연금", "장려금", "바우처", "신청"),
    "tense": ("사기", "피싱", "스미싱", "경고", "위험", "피해", "조심", "주의", "손해", "함정", "과태료", "연체"),
}


def pick_mood(text: str) -> str:
    """제목·슬라이드 글자에서 분위기 단어가 가장 많은 쪽을 고른다 (같으면 앞쪽). 없으면 calm."""
    scores = {mood: sum(text.count(w) for w in words) for mood, words in KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] else "calm"


def library(folder: Path | None = None) -> list[dict]:
    path = (folder or LIBRARY) / "library.json"
    return json.loads(path.read_text(encoding="utf-8"))["tracks"] if path.exists() else []


def pick_track(seed: str, mood: str, folder: Path | None = None, track: str = "") -> dict | None:
    """라이브러리에서 그 분위기 곡을 게시물마다(seed로) 하나 고른다. None이면 합성한다."""
    if track == SYNTH:
        return None
    tracks = library(folder)
    if track:
        for t in tracks:
            if t["file"] == track:
                return t
        raise ValueError(f"곡 '{track}'이 music/library.json에 없습니다")
    same = [t for t in tracks if t["mood"] == mood]
    return same[_seed(seed) % len(same)] if same else None


def credit(track: dict | None) -> str:
    """CC BY 곡의 출처 표기 한 줄 (CC0·퍼블릭 도메인이면 빈 문자열). 자르고 음량을 맞췄으니 '편집'을 밝힌다."""
    if not track or not track.get("attribution"):
        return ""
    lic = track["license"].replace("CC-BY", "CC BY")
    return f"🎵 {track['title']} - {track['artist']} ({lic}, 편집) {track['url']}"


def with_credit(text: str, folder: Path, limit: int) -> str:
    """릴스를 만들 때 남긴 music.json에 출처 표기(CC BY 곡)가 있으면 글 끝에 붙인다. 넘치면 본문을 줄인다."""
    path = folder / "music.json"
    try:
        line = json.loads(path.read_text(encoding="utf-8")).get("credit", "") if path.exists() else ""
    except (OSError, ValueError):
        line = ""
    if not line:
        return text[:limit]
    room = limit - len(line) - 2
    body = text if len(text) <= room else text[:room - 1].rstrip() + "…"
    return f"{body}\n\n{line}"


def track_filter(total: float) -> str:
    """라이브러리 곡용 ffmpeg 필터. 음량은 미리 −16 LUFS로 맞춰 두었으니 페이드만."""
    return f"afade=t=in:d=0.3,afade=t=out:st={max(0.0, total - 1.8):.2f}:d=1.8,alimiter=limit=0.9:level=false"


def _bass_root(m: int) -> int:
    """근음을 33~44(55~104Hz)로 옮긴다. 배음을 같이 내서 폰 스피커에서도 들린다."""
    while m >= 45:
        m -= 12
    while m < 33:
        m += 12
    return m


def _pluck(midi: float, seconds: float, decay: float, tone: float) -> np.ndarray:
    """치고 빠르게 줄어드는 소리. 높은 배음일수록 먼저 사라진다."""
    n = int(seconds * SR)
    t = np.arange(n) / SR
    f = _hz(midi)
    sig = np.zeros(n)
    for k, a in ((1, 1.0), (2, tone), (3, tone * 0.5), (4, tone * 0.25)):
        sig += a * np.exp(-t * decay * (1 + 0.6 * (k - 1))) * np.sin(2 * np.pi * f * k * t)
    return sig * _envelope(n, 0.003, 0.03)


def _bass(midi: float, seconds: float, decay: float) -> np.ndarray:
    n = int(seconds * SR)
    t = np.arange(n) / SR
    f = _hz(midi)
    sig = np.sin(2 * np.pi * f * t) + 0.45 * np.sin(2 * np.pi * 2 * f * t) + 0.2 * np.sin(2 * np.pi * 3 * f * t)
    return sig * np.exp(-t * decay) * _envelope(n, 0.005, 0.04)


def _hp(x: np.ndarray, k: int) -> np.ndarray:
    """이동 평균을 빼서 낮은 소리를 걷어 낸다 (k가 작을수록 높은 소리만 남음)."""
    return x - np.convolve(x, np.ones(k) / k, mode="same")


def _kick(rng: np.random.Generator) -> np.ndarray:
    n = int(0.32 * SR)
    t = np.arange(n) / SR
    f = 48 + 90 * np.exp(-t * 28)  # 높은 음에서 빠르게 떨어지는 '쿵'
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 8)
    return body + 0.3 * rng.standard_normal(n) * np.exp(-t * 300)


def _clap(rng: np.random.Generator) -> np.ndarray:
    n = int(0.25 * SR)
    t = np.arange(n) / SR
    env = np.exp(-t * 18)
    for d in (0.0, 0.011, 0.022):  # 손뼉 여러 번이 살짝 어긋나게 겹친 느낌
        s = int(d * SR)
        env[s:] += 0.8 * np.exp(-t[: n - s] * 180)
    return 0.5 * _hp(rng.standard_normal(n), 12) * env


def _noise_hit(rng: np.random.Generator, seconds: float, decay: float, hp: int, attack: float = 0.0) -> np.ndarray:
    n = int(seconds * SR)
    t = np.arange(n) / SR
    env = np.exp(-t * decay) * _envelope(n, attack, 0.01)
    return _hp(rng.standard_normal(n), hp) * env


def compose(seconds: float, seed: str, mood: str, bpm: float | None = None) -> np.ndarray:
    """(샘플 수, 2) float 배열을 만든다. bpm을 주면 그 빠르기로 고정한다 (calm은 제외)."""
    if mood == "calm":
        return music.compose(seconds, seed)
    p = {**DEFAULTS, **MOODS[mood]}
    rng = random.Random(_seed(seed))
    noise = np.random.default_rng(_seed(seed))
    prog = rng.choice(p["progs"])
    key = rng.choice(p["keys"])
    tempo = rng.uniform(*p["bpm"])
    step = 60 / (bpm or tempo) / 4  # 16분음표 길이
    bar = step * 16

    total = int((seconds + 0.5) * SR)
    mel = np.zeros((total, 2))  # 패드·아르페지오·화음 (upbeat에서는 킥에 맞춰 숨을 쉰다)
    rhy = np.zeros((total, 2))  # 드럼·베이스

    def put(bus: np.ndarray, sig: np.ndarray, at: float, lv: float, pan: float = 0.5) -> None:
        s = int(at * SR)
        if s >= total:
            return
        n = min(len(sig), total - s)
        bus[s:s + n, 0] += lv * sig[:n] * (1 - pan)
        bus[s:s + n, 1] += lv * sig[:n] * pan

    for bi in range(int(np.ceil(seconds / bar)) + 1):
        start = bi * bar
        if start * SR >= total:
            break
        chord = [m + key for m in prog[bi % len(prog)]]
        notes = chord + [chord[1] + 12, chord[2] + 12]
        root = _bass_root(chord[0])

        # 패드: music.py와 같은 소리, 앞뒤 마디와 겹쳐 부드럽게 이어진다
        n = int((bar + 0.8) * SR)
        t = np.arange(n) / SR
        env = _envelope(n, 0.6, 0.8)
        for i, m in enumerate(chord):
            f = _hz(m)
            tone = (np.sin(2 * np.pi * f * 0.998 * t) + np.sin(2 * np.pi * f * 1.002 * t)
                    + 0.25 * np.sin(2 * np.pi * 2 * f * t))
            put(mel, env * tone, start, p["pad"], 0.35 + 0.3 * i / (len(chord) - 1))

        fill = bi % 4 == 3 and "x" in p["clap"]  # 진행이 한 바퀴 돌 때 박수 두 번 더
        for k in range(16):
            at = start + k * step
            if p["arp"][k] != ".":
                put(mel, _pluck(notes[int(p["arp"][k])] + 12, 1.2, p["arp_decay"], p["arp_tone"]), at,
                    p["arp_lv"] * (0.8 + 0.4 * rng.random()), 0.3 if k % 4 < 2 else 0.7)
            if p["stab"][k] == "x":
                for i, m in enumerate(chord[1:]):
                    put(mel, _pluck(m + 12, 0.4, 12.0, 0.6), at, p["stab_lv"], 0.25 + 0.25 * i)
            b = p["bass"][k]
            if b != ".":
                gap = next((j for j, c in enumerate(p["bass"][k + 1:], 1) if c != "."), 16 - k)
                put(rhy, _bass(root + {"R": 0, "F": 7, "O": 12}[b], gap * step + 0.05, p["bass_decay"]), at, p["bass_lv"])
            if p["kick"][k] == "x":
                put(rhy, _kick(noise), at, p["kick_lv"])
            if p["clap"][k] == "x" or (fill and k >= 14):
                put(rhy, _clap(noise), at, p["clap_lv"] * (1.0 if p["clap"][k] == "x" else 0.6), 0.45)
            if p["hat"][k] != ".":
                hat = _noise_hit(noise, 0.3, 11, 3) if p["hat"][k] == "o" else _noise_hit(noise, 0.06, 70, 3)
                put(rhy, hat, at, p["hat_lv"] * (0.75 + 0.25 * rng.random()), 0.6)
            if p["shaker"][k] == "x":
                put(rhy, _noise_hit(noise, 0.12, 28, 4, attack=0.012), at, p["shaker_lv"], 0.4)

    if p["pump"]:  # 박마다 화음이 살짝 눌렸다 올라온다 (킥과 같이 숨 쉬는 느낌)
        beat = np.arange(total) / SR % (step * 4)
        mel *= (1 - 0.5 * np.exp(-beat * 10))[:, None]

    mix = mel + rhy
    mix *= TARGET_RMS * 10 ** (p["level"] / 20) / (np.sqrt(np.mean(mix ** 2)) or 1.0)
    return 0.75 * np.tanh(mix / 0.75)  # 드럼 순간 최댓값만 부드럽게 눌러 줌


def write_track(path: Path, seconds: float, seed: str, mood: str, bpm: float | None = None) -> Path:
    data = (compose(seconds, seed, mood, bpm) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())
    return path


def audio_filter(mood: str, total: float) -> str:
    """음원을 영상에 붙일 때 쓰는 ffmpeg 오디오 필터 (리버브·페이드·음량)."""
    fade_out = f"afade=t=out:st={max(0.0, total - 1.8):.2f}:d=1.8"
    if mood == "calm":  # 카드뉴스봇과 같은 처리
        return f"lowpass=f=6000,aecho=0.8:0.5:70|150:0.22|0.12,afade=t=in:d=1.0,{fade_out},volume=2.0"
    # 드럼이 있으니 하이햇이 살게 고역을 덜 깎고, 첫 박이 바로 들리게 짧게 페이드 인
    return (f"highpass=f=30,aecho=0.85:0.7:45|90:0.10|0.05,afade=t=in:d=0.25,{fade_out},"
            f"volume=2.0,alimiter=limit=0.9:level=false")
