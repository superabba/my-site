"""릴스를 '스타일이 있는 영상'으로 만든다 (lemo-opuscar의 렌더 도구를 쓴다).

  카드뉴스 원고 ─► 스타일 고르기 + 내레이션 문장 쓰기 (LLM, 실패하면 규칙으로)
                ─► 한국어 여성 아나운서 목소리(ko-KR-SunHiNeural) 줄마다 합성 → 길이 측정
                ─► 장면·자막 타임라인(content.json) → 스타일 페이지를 1080×1920으로 프레임마다 렌더링
                ─► 목소리 + 배경음악(분위기별) + 효과음을 스타일별 믹싱 → −14 LUFS로 맞춰 합치기

제목 상자와 자막은 앱 버튼 영역(위 250, 아래 360, 오른쪽 130)을 피한 안쪽에 둔다 (styles/common.js의 SAFE).
스타일 페이지는 reelstudio/styles/<이름>/index.html, 렌더 도구는 reelstudio/core/ (MIT, lemomo-ai/lemo-opuscar).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from pydantic import BaseModel

from . import bgm
from .config import ROOT
from .llm import CardNews, TopicIdea

log = logging.getLogger(__name__)

STUDIO = ROOT / "reelstudio"
SR = 48000
W, H = 1080, 1920
DEFAULT_VOICE = "ko-KR-SunHiNeural"


# ---------- 스타일 목록 ----------


@dataclass(frozen=True)
class Style:
    name: str
    label: str
    when: str  # LLM에게 알려 줄 '어떤 주제에 어울리는지'
    keywords: tuple[str, ...]  # LLM이 없을 때 규칙으로 고르는 단어
    music: str  # 키워드로 분위기가 안 잡힐 때 쓰는 기본 배경음악 분위기 (bgm.py)
    music_db: float  # 목소리가 없을 때 배경음악 크기 (목소리 대비 dB)
    duck_db: float  # 목소리가 나올 때 배경음악을 얼마나 낮추는지
    room: float  # 목소리에 입히는 공간감 (0이면 없음)
    sfx: str  # 효과음 종류 (SFX 표)
    fonts_dir: str = ""


STYLES: dict[str, Style] = {s.name: s for s in [
    Style("caption-news", "뉴스 캡션",
          "새로 나왔거나 바뀐 제도·정책·이슈, 속보성 소식, 한 줄 결론이 강한 주제. 큰 자막이 한 줄씩 박히고 핵심 어구가 노란 블록으로 강조된다",
          ("발표", "개정", "시행", "변경", "신설", "인상", "인하", "확대", "폐지", "출시", "단독", "논란", "속보", "오늘", "이번 주", "올해"),
          music="bright", music_db=-19, duck_db=-9, room=0.0, sfx="slam"),
    Style("chalkboard", "칠판 수업",
          "방법·절차·신청 요령, 오해와 진실, 개념 설명처럼 선생님이 칠판에 써 가며 알려 주는 주제. 차분하고 믿음이 가는 분위기",
          ("방법", "신청", "절차", "오해", "진실", "꿀팁", "체크", "총정리", "알아보", "공제", "절세", "환급", "준비", "순서", "가이드"),
          music="calm", music_db=-21, duck_db=-8, room=0.10, sfx="chalk"),
    Style("swiss-motion", "스위스 타이포",
          "숫자·금리·세율·가격 비교처럼 데이터와 대비가 핵심인 주제. 격자와 큰 숫자, 한 가지 포인트 색으로 정확하고 세련된 느낌",
          ("%", "금리", "세율", "원 ", "비교", "순위", "평균", "차이", "얼마", "수수료", "이자", "배", "만 원", "억"),
          music="upbeat", music_db=-20, duck_db=-10, room=0.0, sfx="tick"),
]}
DEFAULT_STYLE = "caption-news"


def heuristic_style(topic: TopicIdea, card: CardNews) -> str:
    """LLM 없이 제목·본문의 단어로 고른다. 같으면 앞쪽 스타일."""
    text = " ".join([topic.title, topic.angle, topic.hook] + [f"{s.heading} {s.body}" for s in card.slides])
    scores = {n: sum(text.count(w) for w in st.keywords) for n, st in STYLES.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] else DEFAULT_STYLE


# ---------- 내레이션 쓰기 ----------


class NarrationLine(BaseModel):
    slide: int  # 슬라이드 번호 (1부터)
    text: str  # 아나운서가 읽을 문장


class ReelPlan(BaseModel):
    style: str  # STYLES 의 이름
    reason: str  # 이 스타일을 고른 이유 (한 줄)
    lines: list[NarrationLine]


PLAN_SYSTEM = """당신은 경제 정보 숏폼 영상의 연출가이자 작가입니다. 이미 만든 카드뉴스 슬라이드를 세로 영상(릴스·쇼츠)으로 옮기려고 합니다.
해야 할 일:
1) 이 주제에 가장 어울리는 영상 스타일을 하나 고릅니다. 아래 목록의 이름만 쓰세요.
2) 슬라이드마다 한국어 여성 아나운서가 읽을 내레이션을 씁니다.

내레이션 원칙:
- 슬라이드 순서와 개수를 그대로 따르고, 슬라이드마다 정확히 한 줄(1~2문장)
- 한 줄은 공백 포함 50자 안팎(최대 70자). 짧고 또렷한 문장. 합니다체와 해요체 중 하나로 통일 (뉴스 앵커처럼 차분하고 신뢰감 있게)
- 슬라이드 글을 그대로 읽지 말고, 화면에 보이는 숫자·핵심은 말로 한 번 더 짚어 주세요. 화면에 없는 사실을 새로 만들지 마세요
- 첫 장은 시청자가 멈추게 하는 한 문장, 마지막 장은 저장·공유·팔로우를 자연스럽게 권하는 한 문장
- 이모지, 괄호, 해시태그, 영어 약어 풀이는 쓰지 마세요. 숫자는 읽기 쉽게 '15만 원', '3.5퍼센트'처럼 쓰세요"""


def plan_reel(writer, topic: TopicIdea, card: CardNews, forced_style: str = "") -> ReelPlan:
    """스타일과 내레이션을 정한다. LLM이 실패하면 규칙으로 대신한다."""
    catalog = "\n".join(f"- {st.name} ({st.label}): {st.when}" for st in STYLES.values())
    slides = "\n".join(
        f"{i}. [{sl.kind}] {sl.heading.replace(chr(10), ' ')} / {sl.body.replace(chr(10), ' ')} / 강조: {sl.highlight}"
        for i, sl in enumerate(card.slides, 1)
    )
    user = f"""## 고를 수 있는 스타일
{catalog}

## 주제
제목: {topic.title}
관점: {topic.angle}
후킹: {topic.hook}

## 슬라이드 ({len(card.slides)}장)
{slides}"""
    try:
        plan = writer._parse(PLAN_SYSTEM, user, ReelPlan)
        plan = _clean_plan(plan, card)
    except Exception as e:  # LLM 오류가 영상 제작을 막지 않게 한다
        log.warning("릴스 내레이션 작성 실패, 규칙으로 대신합니다: %s", e)
        plan = fallback_plan(topic, card)
    if forced_style in STYLES:
        plan = plan.model_copy(update={"style": forced_style})
    return plan


def _clean_plan(plan: ReelPlan, card: CardNews) -> ReelPlan:
    by_slide = {ln.slide: ln.text.strip() for ln in plan.lines if ln.text.strip()}
    lines = []
    for i, sl in enumerate(card.slides, 1):
        text = by_slide.get(i) or _fallback_line(sl)
        lines.append(NarrationLine(slide=i, text=_tidy(text)))
    style = plan.style.strip() if plan.style.strip() in STYLES else ""
    return ReelPlan(style=style or DEFAULT_STYLE, reason=plan.reason, lines=lines)


def _tidy(text: str) -> str:
    text = re.sub(r"[\U00010000-\U0010ffff☀-➿]", "", text)  # 이모지
    text = re.sub(r"[#*_`]", "", text).replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def _fallback_line(sl) -> str:
    parts = [sl.heading.replace("\n", " ").strip(), sl.body.replace("\n", " ").strip()]
    return _tidy(". ".join(p for p in parts if p))[:90]


def fallback_plan(topic: TopicIdea, card: CardNews) -> ReelPlan:
    lines = [NarrationLine(slide=i, text=_fallback_line(sl)) for i, sl in enumerate(card.slides, 1)]
    return ReelPlan(style=heuristic_style(topic, card), reason="규칙으로 선택", lines=lines)


# ---------- 목소리 ----------


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def decode_audio(path: Path, channels: int = 1) -> np.ndarray:
    """어떤 음원이든 48kHz float32로 읽는다. 모노면 (n,), 스테레오면 (n, 2)."""
    out = subprocess.run(
        [ffmpeg_exe(), "-v", "error", "-i", str(path), "-f", "f32le", "-ac", str(channels), "-ar", str(SR), "-"],
        capture_output=True, check=True,
    ).stdout
    a = np.frombuffer(out, dtype=np.float32).copy()
    return a if channels == 1 else a.reshape(-1, channels)


def trim_silence(x: np.ndarray, pad: float = 0.06, thr: float = 0.012) -> np.ndarray:
    idx = np.nonzero(np.abs(x) > thr)[0]
    if len(idx) == 0:
        return x
    a, b = max(0, idx[0] - int(pad * SR)), min(len(x), idx[-1] + int(pad * 1.5 * SR))
    return x[a:b]


async def _tts_one(text: str, voice: str, rate: str, pitch: str, out: Path, sem: asyncio.Semaphore) -> None:
    import edge_tts

    async with sem:
        for attempt in range(4):
            try:
                await edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).save(str(out))
                if out.exists() and out.stat().st_size > 800:
                    return
            except Exception as e:
                log.warning("TTS 재시도 %d/4: %s", attempt + 1, e)
            await asyncio.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"TTS 합성 실패: {text[:30]}")


def _fake_voice(text: str) -> np.ndarray:
    """개발용: 인터넷이 막힌 곳에서 길이만 맞춘 웅얼거림을 만든다 (환경변수 REELS_FAKE_TTS=1일 때만, 실제 발행에는 쓰지 않는다)."""
    n = int(len(text.replace(" ", "")) * 0.2 * SR)
    t = np.arange(n) / SR
    rng = np.random.default_rng(len(text))
    syl = 0.5 + 0.5 * np.sin(2 * np.pi * 4.2 * t + rng.random() * 6)
    tone = np.sin(2 * np.pi * (190 + 25 * np.sin(2 * np.pi * 0.9 * t)) * t) + 0.4 * np.sin(2 * np.pi * 380 * t)
    return (0.25 * syl * tone).astype(np.float32)


def synthesize(lines: list[str], workdir: Path, voice: str = DEFAULT_VOICE, rate: str = "+0%", pitch: str = "+0Hz") -> list[np.ndarray]:
    """줄마다 mp3로 받아 앞뒤 무음을 잘라 낸 48kHz 모노 배열로 돌려준다."""
    if os.environ.get("REELS_FAKE_TTS") == "1":
        log.warning("REELS_FAKE_TTS=1: 실제 목소리 대신 길이만 맞춘 소리를 씁니다 (개발용)")
        return [_fake_voice(t) for t in lines]
    workdir.mkdir(parents=True, exist_ok=True)

    async def run():
        sem = asyncio.Semaphore(3)
        await asyncio.gather(*[
            _tts_one(t, voice, rate, pitch, workdir / f"line_{i:02d}.mp3", sem) for i, t in enumerate(lines)
        ])

    asyncio.run(run())
    return [trim_silence(decode_audio(workdir / f"line_{i:02d}.mp3")) for i in range(len(lines))]


# ---------- 타임라인 ----------


def split_subtitle(text: str, t0: float, t1: float, max_chars: int = 15) -> list[dict]:
    """내레이션을 한 번에 한 줄로 읽히는 토막으로 나눠, 글자 수에 비례해 시간을 배분한다."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?。])\s+|(?<=[,，])\s+", text) if s.strip()]
    chunks: list[str] = []
    for sent in sentences or [text]:
        cur = ""
        for word in sent.split(" "):
            if cur and len(cur) + 1 + len(word) > max_chars:
                chunks.append(cur)
                cur = word
            else:
                cur = f"{cur} {word}".strip()
        if cur:
            chunks.append(cur)
    # 너무 짧은 토막은 앞(없으면 뒤) 토막에 붙인다
    merged: list[str] = []
    for c in chunks:
        if merged and (len(c) <= 6 or len(merged[-1]) <= 6) and len(merged[-1]) + 1 + len(c) <= max_chars + 5:
            merged[-1] += " " + c
        else:
            merged.append(c)
    weights = [max(1, len(c.replace(" ", ""))) for c in merged]
    total = sum(weights)
    out, t = [], t0
    for c, w in zip(merged, weights):
        d = (t1 - t0) * w / total
        out.append({"t0": round(t, 3), "t1": round(t + d, 3), "text": c})
        t += d
    return out


def bullets_of(body: str) -> list[str]:
    # 줄머리 기호("·", "•", "-", "1.", "2)")만 떼고 숫자 자체("500km")는 남긴다
    parts = [re.sub(r"^\s*(?:[·•\-]|\d{1,2}[.)])\s*", "", p).strip() for p in re.split(r"[\n]+", body or "")]
    parts = [p for p in parts if p]
    return parts if len(parts) >= 2 else []


@dataclass
class Timeline:
    content: dict
    voice_at: list[tuple[float, np.ndarray]]  # (시작 시각, 음성)
    duration: float
    scene_starts: list[float] = field(default_factory=list)


def build_timeline(card: CardNews, plan: ReelPlan, voices: list[np.ndarray], topic: TopicIdea, handle: str,
                   lead: float = 0.30, gap: float = 0.40, tail: float = 0.9) -> Timeline:
    scenes, voice_at, t = [], [], 0.0
    n = len(card.slides)
    for i, (sl, line, v) in enumerate(zip(card.slides, plan.lines, voices)):
        dur = len(v) / SR
        t0 = t
        vs = t0 + (lead + 0.2 if i == 0 else lead)
        t1 = vs + dur + (tail if i == n - 1 else gap)
        scenes.append({
            "index": i + 1, "kind": sl.kind,
            "heading": sl.heading.strip(), "body": sl.body.strip(), "highlight": (sl.highlight or "").strip(),
            "bullets": bullets_of(sl.body) if sl.kind in ("summary", "cta") else [],
            "t0": round(t0, 3), "t1": round(t1, 3), "voice_t0": round(vs, 3), "voice_t1": round(vs + dur, 3),
            "narration": line.text, "subs": split_subtitle(line.text, vs, vs + dur),
        })
        voice_at.append((vs, v))
        t = t1
    content = {
        "style": plan.style, "title": (topic.title or card.slides[0].heading).replace("\n", " ").strip(),
        "handle": handle, "channel": "경제주막 · 오늘의 이슈", "duration": round(t, 3), "scenes": scenes,
    }
    return Timeline(content, voice_at, t, [s["t0"] for s in scenes])


# ---------- 소리 ----------


def _sfx():
    """효과음 도구(lemo-opuscar core/audio/sfx.py). scipy가 필요하다."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("reel_sfx", STUDIO / "core" / "audio" / "sfx.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def chalk_scratch(sfx, d: float, v: float = 1.0) -> np.ndarray:
    """분필이 칠판을 긁는 소리: 잡음을 중고음 대역으로 거르고 불규칙하게 끊어 준다."""
    n = sfx.noise(d)
    x = sfx.bp(n, 1800, 6500, 2)
    tt = sfx.t_(d)
    gate = (np.sin(2 * np.pi * 17 * tt) * 0.5 + 0.5) ** 1.5 * (np.sin(2 * np.pi * 5.3 * tt + 1) * 0.3 + 0.7)
    env = np.minimum(1, tt / 0.03) * np.minimum(1, (d - tt) / 0.06)
    return sfx.norm(x * gate * env) * v


def make_cues(style: Style, tl: Timeline) -> list[dict]:
    """페이지의 움직임과 같은 시각에 효과음을 놓기 위한 목록."""
    cues = []
    for s in tl.content["scenes"]:
        t0 = s["t0"]
        if style.sfx == "slam":
            cues += [{"t": t0 + 0.04, "type": "whoosh"}, {"t": t0 + 0.12, "type": "thump"}]
        elif style.sfx == "chalk":
            cues += [{"t": t0 + 0.05, "type": "pop"}, {"t": t0 + 0.2, "type": "chalk", "d": min(1.6, max(0.6, (s["voice_t1"] - t0) * 0.5))}]
        else:
            cues += [{"t": t0 + 0.0, "type": "tick"}]
        for u in s["subs"][1:]:
            if style.sfx == "tick":
                cues.append({"t": u["t0"], "type": "tick"})
    return cues


def choose_music_mood(style: Style, text: str) -> str:
    mood = bgm.pick_mood(text)
    return style.music if mood == "calm" else mood


def _voice_track(tl: Timeline, style: Style) -> np.ndarray:
    n = int((tl.duration + 0.5) * SR)
    x = np.zeros(n, np.float32)
    for t0, v in tl.voice_at:
        s = int(t0 * SR)
        v = v.astype(np.float32)
        fade = min(int(0.01 * SR), len(v) // 4)
        if fade:
            v[:fade] *= np.linspace(0, 1, fade)
            v[-fade:] *= np.linspace(1, 0, fade)
        x[s:s + len(v)] += v[: n - s]
    sfx = _sfx()
    x = sfx.compress(x.astype(np.float64), thr=0.22, ratio=3.0).astype(np.float32)
    rms = float(np.sqrt(np.mean(x[np.abs(x) > 0.01] ** 2))) if np.any(np.abs(x) > 0.01) else 1.0
    x *= 0.16 / max(rms, 1e-6)  # 목소리 평균 크기를 맞춘다
    if style.room > 0:  # 작은 교실 같은 공간감
        rng = np.random.default_rng(3)
        ir = rng.standard_normal(int(0.28 * SR)) * np.exp(-np.arange(int(0.28 * SR)) / (0.07 * SR))
        ir /= np.sqrt(np.sum(ir ** 2))
        wet = np.convolve(x, ir.astype(np.float32))[:n]
        x = x * (1 - style.room) + wet * style.room
    return np.clip(x, -0.95, 0.95)


def _music_track(tl: Timeline, mood: str, folder: Path) -> tuple[np.ndarray, dict]:
    """곡 라이브러리(music/)에 그 분위기 곡이 있으면 그 곡, 없으면 합성. (스테레오 배열, music.json 내용)"""
    n = int((tl.duration + 0.5) * SR)
    picked = bgm.pick_track(folder.name, mood)
    with tempfile.TemporaryDirectory() as td:
        if picked:
            src = bgm.LIBRARY / picked["file"]
            info = {"mood": mood, "track": picked["file"], "credit": bgm.credit(picked)}
        else:
            src = bgm.write_track(Path(td) / "synth.wav", tl.duration + 1, folder.name, mood)
            info = {"mood": mood, "track": "synth", "credit": ""}
        m = decode_audio(src, 2)
    if len(m) < n:  # 짧으면 이어 붙인다
        reps = int(np.ceil(n / len(m)))
        m = np.concatenate([m] * reps)
    return m[:n], info


def mix_audio(tl: Timeline, style: Style, folder: Path, text_for_mood: str) -> tuple[np.ndarray, dict]:
    """목소리 + 음악(목소리가 나올 때 낮춤) + 효과음 → 스테레오 float32."""
    n = int((tl.duration + 0.5) * SR)
    voice = _voice_track(tl, style)
    mood = choose_music_mood(style, text_for_mood)
    music, music_info = _music_track(tl, mood, folder)
    log.info("릴스 배경음: %s (%s)", music_info["track"], mood)

    # 목소리 크기에서 덕킹 곡선: 빨리 낮추고 천천히 올린다
    env = np.abs(voice)
    k = int(0.05 * SR)
    sm = np.convolve(env, np.ones(k) / k, mode="same")
    act = (sm > 0.02).astype(np.float32)
    rel = int(0.45 * SR)
    duck = np.convolve(act, np.ones(rel) / rel, mode="same")
    duck = np.clip(duck * 1.8, 0, 1)
    lo = 10 ** (style.duck_db / 20)
    gain = (1 - duck) + duck * lo
    mus_peak = float(np.sqrt(np.mean(music ** 2))) or 1.0
    music = music * (0.16 / mus_peak) * 10 ** (style.music_db / 20) * 1.0
    music = music * gain[:, None]
    # 시작·끝 페이드
    fi, fo = int(0.8 * SR), int(1.6 * SR)
    music[:fi] *= np.linspace(0, 1, fi)[:, None]
    music[-fo:] *= np.linspace(1, 0, fo)[:, None]

    sfx = _sfx()
    fx = np.zeros((n, 2), np.float64)
    cache = {}
    for c in make_cues(style, tl):
        ty = c["type"]
        if ty == "whoosh":
            snd, g = sfx.whoosh(0.28), 0.22
        elif ty == "thump":
            snd, g = sfx.thump(1.0, 80), 0.30
        elif ty == "pop":
            snd, g = sfx.pop(), 0.16
        elif ty == "tick":
            snd, g = cache.setdefault("tick", sfx.step()), 0.20
        elif ty == "chalk":
            snd, g = chalk_scratch(sfx, c["d"]), 0.17
        else:
            continue
        sfx.add(fx, snd, c["t"], g)
    mix = np.stack([voice, voice], axis=1) + music + fx.astype(np.float32)
    mix = np.tanh(mix / 0.9) * 0.9
    return mix.astype(np.float32), music_info


def write_wav(path: Path, x: np.ndarray) -> None:
    data = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(x.shape[1])
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())


# ---------- 렌더링 ----------


def _ffmpeg_shim(td: Path) -> dict:
    """렌더 도구(video.mjs)가 부르는 `ffmpeg`를 imageio가 가진 ffmpeg로 연결한다 (시스템 ffmpeg 버전에 상관없이)."""
    bindir = td / "bin"
    bindir.mkdir(exist_ok=True)
    link = bindir / "ffmpeg"
    if not link.exists():
        link.symlink_to(ffmpeg_exe())
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    return env


def prepare_job(job: Path, style: str, content: dict, fonts_dir: Path) -> None:
    job.mkdir(parents=True, exist_ok=True)
    shutil.copy(STUDIO / "styles" / style / "index.html", job / "index.html")
    (job / "content.json").write_text(json.dumps(content, ensure_ascii=False, indent=1), encoding="utf-8")
    (job / "fonts").mkdir(exist_ok=True)
    for f in fonts_dir.glob("Pretendard-*.otf"):
        shutil.copy(f, job / "fonts" / f.name)


def run_node(script: str, args: list[str], env: dict, timeout: int = 1500) -> None:
    cmd = ["node", str(STUDIO / "core" / "render" / script), *args]
    r = subprocess.run(cmd, cwd=STUDIO, env=env, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{script} 실패(코드 {r.returncode}): {(r.stderr or r.stdout)[-600:]}")


def render_frames(job: Path, td: Path, fps: int, workers: int) -> Path:
    env = _ffmpeg_shim(td)
    out = job / "out" / "video.mp4"
    run_node("video.mjs", [str(job), "--fps", str(fps), "--size", f"{W}x{H}", "--workers", str(workers), "--out", str(out)], env)
    return out


def render_cover(job: Path, td: Path, t: float, dest: Path) -> None:
    env = _ffmpeg_shim(td)
    stills = job / "cover"
    run_node("still.mjs", [str(job), f"{t:.2f}", "--size", f"{W}x{H}", "--out", str(stills), "--prefix", "c_"], env, timeout=300)
    src = next(stills.glob("c_*.jpg"))
    shutil.copy(src, dest)


def mux(video: Path, wav: Path, out: Path, fps: int) -> None:
    """−14 LUFS로 두 번에 걸쳐 맞춰 합친다 (lemo-opuscar mux.sh와 같은 방식)."""
    ff = ffmpeg_exe()
    target = "loudnorm=I=-14:TP=-1.2:LRA=11"
    m = subprocess.run([ff, "-hide_banner", "-nostats", "-i", str(wav), "-af", f"{target}:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    meas = None
    mm = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", m.stderr, re.S)
    if mm:
        try:
            meas = json.loads(mm.group(0))
        except ValueError:
            meas = None
    ln = target
    if meas and all(k in meas and "inf" not in str(meas[k]).lower() for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")):
        ln = (f"{target}:measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:measured_LRA={meas['input_lra']}"
              f":measured_thresh={meas['input_thresh']}:offset={meas['target_offset']}:linear=true")
    cmd = [ff, "-y", "-loglevel", "error", "-i", str(video), "-i", str(wav),
           "-filter_complex", f"[0:v]fps={fps},format=yuv420p[v];[1:a]{ln},aresample=48000[a]",
           "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-profile:v", "high",
           "-r", str(fps), "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"영상 합치기 실패: {r.stderr[-400:]}")


# ---------- 한 번에 ----------


def make_reel(card: CardNews, topic: TopicIdea, folder: Path, writer, fonts_dir: Path, handle: str = "@economypub",
              *, style: str = "", voice: str = DEFAULT_VOICE, rate: str = "+0%", fps: int = 30,
              workers: int | None = None, keep_job: bool = False) -> tuple[Path, Path, dict]:
    """(릴스 영상, 커버 이미지, 계획 정보)를 만든다. 실패하면 예외를 던진다 (부르는 쪽이 예전 방식으로 대신한다)."""
    plan = plan_reel(writer, topic, card, forced_style=style)
    st = STYLES[plan.style]
    log.info("릴스 스타일: %s (%s) — %s", st.name, st.label, plan.reason)
    with tempfile.TemporaryDirectory(prefix="lemo_") as td_s:
        td = Path(td_s)
        voices = synthesize([ln.text for ln in plan.lines], td / "voice", voice=voice, rate=rate)
        tl = build_timeline(card, plan, voices, topic, handle)
        text = tl.content["title"] + " " + " ".join(f"{s['heading']} {s['body']}" for s in tl.content["scenes"])
        mix, music_info = mix_audio(tl, st, folder, text)
        wav = td / "mix.wav"
        write_wav(wav, mix)
        job = folder / "lemo_job" if keep_job else td / "job"
        prepare_job(job, st.name, tl.content, fonts_dir)
        video = render_frames(job, td, fps, workers or min(4, os.cpu_count() or 2))
        out = folder / "reel.mp4"
        mux(video, wav, out, fps)
        cover = folder / "reel_cover.jpg"
        render_cover(job, td, min(1.2, tl.content["scenes"][0]["t1"] - 0.1), cover)
    (folder / "music.json").write_text(json.dumps(music_info, ensure_ascii=False), encoding="utf-8")
    info = {"style": st.name, "label": st.label, "reason": plan.reason, "duration": tl.duration, "music": music_info,
            "narration": [ln.text for ln in plan.lines]}
    (folder / "reel_plan.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("릴스 영상 생성: %s (%.1f초, %s)", out.name, tl.duration, st.label)
    return out, cover, info
