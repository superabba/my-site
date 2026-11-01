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
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from pydantic import BaseModel

from . import bgm
from .config import ROOT
from .llm import CardNews, Slide, TopicIdea

log = logging.getLogger(__name__)

STUDIO = ROOT / "reelstudio"
SR = 48000
W, H = 1080, 1920
DEFAULT_VOICE = "ko-KR-SunHiNeural"
MAX_SECONDS = 58.0  # 영상 길이 상한 (목소리가 이보다 길어지면 말하는 속도를 올려 맞춘다)
RATE_STEPS = ("+15%", "+30%")  # 길이를 못 맞출 때 차례로 올리는 말하는 속도


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
    sfx: str  # 효과음 종류 (make_cues)
    page: str = ""  # 쓰는 페이지 폴더 (비우면 name)
    fps: int = 0  # 0이면 설정의 기본 fps
    bpm: int = 0  # 0보다 크면 컷을 이 박자 격자에 맞추고, 합성 음악도 이 빠르기로 만든다
    beats: bool = False  # 첫 컷 중간과 마지막 컷 직전에 정적 + 반전 카드를 넣는다 (6컷 숏폼 구성)


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
    Style("caption-beat", "뉴스 캡션 · 120BPM 비트",
          "훅과 반전이 있는 숏폼 팁 영상. 첫 컷에서 시선을 잡고 → 데이터 비교 → 핵심 팁 3가지 → 요약·저장 유도의 6컷 구성. "
          "120BPM 박자에 맞춰 컷이 바뀌고, 첫 컷 중간과 마지막 컷 직전에 짧은 정적과 반전 카드가 들어간다. 핵심 어구에 노란 마커가 쓱 지나간다",
          ("반전", "몰랐", "진짜", "알고 보니", "이유", "비밀", "함정", "3가지", "세 가지", "실수", "손해", "놓치"),
          music="upbeat", music_db=-17, duck_db=-12, room=0.0, sfx="slam",
          page="caption-news", fps=24, bpm=120, beats=True),
]}
DEFAULT_STYLE = "caption-news"
BEAT = 60.0 / 120  # 120BPM의 한 박 = 0.5초


def heuristic_style(topic: TopicIdea, card: CardNews) -> str:
    """LLM 없이 제목·본문의 단어로 고른다. 같으면 앞쪽 스타일."""
    text = " ".join([topic.title, topic.angle, topic.hook] + [f"{s.heading} {s.body}" for s in card.slides])
    scores = {n: sum(text.count(w) for w in st.keywords) for n, st in STYLES.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] else DEFAULT_STYLE


# ---------- 컷 구성 ----------


def shots_for(style: Style, card: CardNews) -> list[Slide]:
    """영상의 컷 목록. 보통은 슬라이드 그대로, 비트 스타일은 6컷
    (훅 → 데이터 비교 → 팁 3개 → 요약·저장 유도)으로 줄이거나 합친다."""
    if not style.beats:
        return list(card.slides)
    slides = card.slides
    cover = slides[0]
    last = slides[-1]
    summary = next((s for s in slides if s.kind == "summary"), None)
    middle = [s for s in slides[1:] if s.kind == "content"][:4]
    closing = Slide(kind="cta", heading=last.heading, body=(summary.body if summary else last.body),
                    highlight=(summary.highlight if summary else last.highlight))
    return [cover, *middle, closing]


# ---------- 내레이션 쓰기 ----------


class NarrationLine(BaseModel):
    slide: int  # 컷(슬라이드) 번호 (1부터)
    text: str  # 아나운서가 읽을 문장. 강조할 핵심 어구는 {중괄호}로 감싼다


class ReelPlan(BaseModel):
    style: str  # STYLES 의 이름
    reason: str  # 이 스타일을 고른 이유 (한 줄)
    lines: list[NarrationLine]
    card_mid: str = ""  # (비트 스타일) 첫 컷 중간 정적에 나오는 반전 카드 글 (12자 안팎)
    card_last: str = ""  # (비트 스타일) 마지막 컷 직전 정적에 나오는 카드 글 (12자 안팎)


PLAN_SYSTEM = """당신은 경제 정보 숏폼 영상의 연출가이자 작가입니다. 이미 만든 카드뉴스 슬라이드를 세로 영상(릴스·쇼츠)으로 옮기려고 합니다.
해야 할 일:
1) 이 주제에 가장 어울리는 영상 스타일을 하나 고릅니다. 아래 목록의 이름만 쓰세요.
2) 슬라이드마다 한국어 여성 아나운서가 읽을 내레이션을 씁니다.

내레이션 원칙:
- 슬라이드 순서와 개수를 그대로 따르고, 슬라이드마다 정확히 한 줄(1~2문장)
- 한 줄은 공백 포함 50자 안팎(최대 70자). 짧고 또렷한 문장. 합니다체와 해요체 중 하나로 통일 (뉴스 앵커처럼 차분하고 신뢰감 있게)
- 슬라이드 글을 그대로 읽지 말고, 화면에 보이는 숫자·핵심은 말로 한 번 더 짚어 주세요. 화면에 없는 사실을 새로 만들지 마세요
- 첫 장은 시청자가 멈추게 하는 한 문장, 마지막 장은 저장·공유·팔로우를 자연스럽게 권하는 한 문장
- 줄마다 가장 중요한 핵심 어구 하나(숫자나 키워드, 2~8자)를 {중괄호}로 감싸세요. 예: 보험료를 {15만 원}까지 돌려받아요
- 이모지, 괄호, 해시태그, 영어 약어 풀이는 쓰지 마세요. 숫자는 읽기 쉽게 '15만 원', '3.5퍼센트'처럼 쓰세요"""

BEAT_SYSTEM = """당신은 경제 정보 숏폼 영상의 연출가이자 작가입니다. 카드뉴스를 120BPM 박자에 맞춘 6컷 숏폼(약 30~40초)으로 옮깁니다.
컷 구성: 1 훅과 반전 → 2 배경 데이터 비교 → 3·4·5 핵심 팁 하나씩 → 6 핵심 요약 + 저장·팔로우 유도.
컷 수가 6개보다 적으면 있는 컷만 같은 순서로 씁니다.

원칙:
- 컷마다 정확히 한 줄(1~2문장, 공백 포함 45자 안팎, 최대 60자). 짧고 또렷하게. 합니다체와 해요체 중 하나로 통일, 뉴스 앵커처럼 차분하고 신뢰감 있게
- 1컷은 "그런 줄 알았는데 사실은"처럼 시선을 잡고 반전을 건드리는 두 문장으로, 첫 문장과 둘째 문장 사이에서 잠깐 멈춥니다
- 줄마다 가장 중요한 핵심 어구 하나(숫자나 키워드, 2~8자)를 {중괄호}로 감싸세요. 노란 마커가 그 말을 할 때 지나갑니다
- 카드뉴스에 없는 사실을 새로 만들지 마세요. 이모지, 괄호, 해시태그는 쓰지 마세요. 숫자는 '15만 원', '3.5퍼센트'처럼 쓰세요
- card_mid: 1컷 중간 정적 때 화면 전체에 크게 뜨는 반전 카드 글 (12자 이내, 예: 그런데 말이죠)
- card_last: 마지막 컷 직전 정적 때 뜨는 카드 글 (12자 이내, 예: 그래서 결론은?)"""


def _describe(shots: list[Slide]) -> str:
    return "\n".join(
        f"{i}. [{sl.kind}] {sl.heading.replace(chr(10), ' ')} / {sl.body.replace(chr(10), ' ')} / 강조: {sl.highlight}"
        for i, sl in enumerate(shots, 1)
    )


def plan_reel(writer, topic: TopicIdea, card: CardNews, forced_style: str = "") -> ReelPlan:
    """스타일과 내레이션을 정한다. LLM이 실패하면 규칙으로 대신한다."""
    catalog = "\n".join(f"- {st.name} ({st.label}): {st.when}" for st in STYLES.values())
    head = f"""## 주제
제목: {topic.title}
관점: {topic.angle}
후킹: {topic.hook}
"""
    plan: ReelPlan
    try:
        if forced_style in STYLES:
            plan = ReelPlan(style=forced_style, reason="지정한 스타일", lines=[])
        else:
            user = f"## 고를 수 있는 스타일\n{catalog}\n\n{head}\n## 슬라이드 ({len(card.slides)}장)\n{_describe(card.slides)}"
            plan = _clean_plan(writer._parse(PLAN_SYSTEM, user, ReelPlan), card.slides)
        style = STYLES[plan.style]
        shots = shots_for(style, card)
        # 컷이 슬라이드와 다르면(비트 스타일) 그 컷 기준으로 내레이션을 새로 쓴다. 지정한 스타일이면 처음 쓰는 것이다
        if style.beats or forced_style in STYLES:
            system = BEAT_SYSTEM if style.beats else PLAN_SYSTEM
            user = f"## 스타일\n{style.name} ({style.label}): {style.when}\n\n{head}\n## 컷 ({len(shots)}개)\n{_describe(shots)}"
            again = writer._parse(system, user, ReelPlan)
            plan = _clean_plan(again.model_copy(update={"style": style.name}), shots)
        else:
            plan = _clean_plan(plan, shots)
    except Exception as e:  # LLM 오류가 영상 제작을 막지 않게 한다
        log.warning("릴스 내레이션 작성 실패, 규칙으로 대신합니다: %s", e)
        plan = fallback_plan(topic, card, forced_style)
    if forced_style in STYLES and plan.style != forced_style:
        plan = fallback_plan(topic, card, forced_style)
    return plan


def _clean_plan(plan: ReelPlan, shots: list[Slide]) -> ReelPlan:
    by_slide = {ln.slide: ln.text.strip() for ln in plan.lines if ln.text.strip()}
    lines = []
    for i, sl in enumerate(shots, 1):
        text = by_slide.get(i) or _fallback_line(sl)
        lines.append(NarrationLine(slide=i, text=_tidy(text)))
    style = plan.style.strip() if plan.style.strip() in STYLES else ""
    return ReelPlan(style=style or DEFAULT_STYLE, reason=plan.reason, lines=lines,
                    card_mid=_tidy(plan.card_mid)[:16], card_last=_tidy(plan.card_last)[:16])


def _tidy(text: str) -> str:
    text = re.sub(r"[\U00010000-\U0010ffff☀-➿]", "", text)  # 이모지
    text = re.sub(r"[#*_`]", "", text).replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def _fallback_line(sl) -> str:
    parts = [sl.heading.replace("\n", " ").strip(), sl.body.replace("\n", " ").strip()]
    return _tidy(". ".join(p for p in parts if p))[:90]


def fallback_plan(topic: TopicIdea, card: CardNews, forced_style: str = "") -> ReelPlan:
    style = forced_style if forced_style in STYLES else heuristic_style(topic, card)
    shots = shots_for(STYLES[style], card)
    lines = [NarrationLine(slide=i, text=_fallback_line(sl)) for i, sl in enumerate(shots, 1)]
    return ReelPlan(style=style, reason="규칙으로 선택", lines=lines, card_mid="그런데 말이죠", card_last="그래서 결론은?")


MARK_RE = re.compile(r"\{([^{}]{1,16})\}")


def parse_marks(text: str) -> tuple[str, list[tuple[str, int]]]:
    """'보험료를 {15만 원}까지' → ('보험료를 15만 원까지', [('15만 원', 4)]). 괄호는 읽지 않고 자막에도 쓰지 않는다."""
    marks: list[tuple[str, int]] = []
    out, last = [], 0
    for m in MARK_RE.finditer(text):
        out.append(text[last:m.start()])
        pos = len("".join(out))
        out.append(m.group(1))
        marks.append((m.group(1), pos))
        last = m.end()
    out.append(text[last:])
    clean = "".join(out).replace("{", "").replace("}", "")
    return clean, marks


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


def fake_duration(text: str) -> float:
    """개발용 길이 추정: 글자당 0.19초 (공백 제외), 쉼표마다 +0.25초."""
    chars = len(re.sub(r"\s", "", text))
    return chars * 0.19 + 0.25 * len(re.findall(r"[,，]", text))


def _fake_voice(text: str) -> np.ndarray:
    """개발용: 인터넷이 막힌 곳에서 길이만 맞춘 말소리 비슷한 소리를 만든다 (REELS_FAKE_TTS=1일 때만).
    완전한 무음이면 더킹·무음 자르기가 틀어지므로, 말소리 대역(약 190·380Hz)을 음절 박자로 끊어 진폭 0.15~0.2로 낸다."""
    n = int(fake_duration(text) * SR)
    t = np.arange(n) / SR
    rng = np.random.default_rng(len(text))
    syl = 0.55 + 0.45 * np.sin(2 * np.pi * 4.2 * t + rng.random() * 6)  # 음절 박자의 엔벨로프 (0.1~1.0)
    tone = np.sin(2 * np.pi * (190 + 25 * np.sin(2 * np.pi * 0.9 * t)) * t) + 0.4 * np.sin(2 * np.pi * 380 * t)
    env = np.minimum(1, t / 0.03) * np.minimum(1, (n / SR - t) / 0.05)
    x = 0.2 * syl * tone / 1.4 * env
    return (x * (0.17 / max(float(np.abs(x).max()), 1e-6))).astype(np.float32)


def synthesize(lines: list[str], workdir: Path, voice: str = DEFAULT_VOICE, rate: str = "+0%", pitch: str = "+0Hz") -> list[np.ndarray]:
    """줄마다 mp3로 받아 앞뒤 무음을 잘라 낸 48kHz 모노 배열로 돌려준다."""
    workdir.mkdir(parents=True, exist_ok=True)
    if os.environ.get("REELS_FAKE_TTS") == "1":
        log.warning("REELS_FAKE_TTS=1: 실제 목소리 대신 길이만 맞춘 소리를 씁니다 (개발용)")
        out = [_fake_voice(t) for t in lines]
    else:
        async def run():
            sem = asyncio.Semaphore(3)
            await asyncio.gather(*[
                _tts_one(t, voice, rate, pitch, workdir / f"line_{i:02d}.mp3", sem) for i, t in enumerate(lines)
            ])

        asyncio.run(run())
        out = [trim_silence(decode_audio(workdir / f"line_{i:02d}.mp3")) for i in range(len(lines))]
    (workdir / "dur.json").write_text(json.dumps([round(len(v) / SR, 3) for v in out]), encoding="utf-8")
    return out


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
class Item:
    """영상의 한 토막: 컷(슬라이드 + 내레이션) 또는 정적 카드."""
    slide: Slide | None = None  # None이면 카드
    text: str = ""  # 내레이션 ({키워드} 표시 포함)
    voice: np.ndarray | None = None
    card: str = ""  # 카드 글
    cont: bool = False  # 앞 컷의 화면을 그대로 잇는다 (첫 컷 중간 정적 뒤)
    shot: int = 0  # 컷 번호 (카드는 앞 컷 번호)


@dataclass
class Timeline:
    content: dict
    voice_at: list[tuple[float, np.ndarray]]  # (시작 시각, 음성)
    duration: float
    scene_starts: list[float] = field(default_factory=list)


def split_first(text: str) -> tuple[str, str] | None:
    """첫 컷 내레이션을 중간 정적 앞뒤로 나눈다: 문장 경계에서, 없으면 공백 기준 가운데에서."""
    sents = [s.strip() for s in re.split(r"(?<=[.!?。])\s+", text) if s.strip()]
    if len(sents) >= 2:
        k = max(1, round(len(sents) / 2))
        return " ".join(sents[:k]), " ".join(sents[k:])
    words = text.split(" ")
    if len(words) < 4:
        return None
    k = len(words) // 2
    return " ".join(words[:k]), " ".join(words[k:])


def make_items(style: Style, shots: list[Slide], plan: ReelPlan) -> list[Item]:
    """컷마다 한 토막, 비트 스타일이면 첫 컷을 둘로 나눠 사이에 카드, 마지막 컷 앞에도 카드를 끼운다."""
    items: list[Item] = []
    n = len(shots)
    for i, (sl, ln) in enumerate(zip(shots, plan.lines), 1):
        if style.beats and i == 1 and (halves := split_first(ln.text)):
            items += [Item(sl, halves[0], shot=1), Item(None, card=plan.card_mid or "그런데 말이죠", shot=1),
                      Item(sl, halves[1], cont=True, shot=1)]
            continue
        if style.beats and i == n and n >= 3:
            items.append(Item(None, card=plan.card_last or "그래서 결론은?", shot=n - 1))
        items.append(Item(sl, ln.text, shot=i))
    return items


def snap_up(t: float, bpm: int) -> float:
    """시각을 박자 격자(한 박 = 60/bpm초)의 다음 칸으로 올린다."""
    beat = 60.0 / bpm
    return round(math.ceil(t / beat - 1e-9) * beat, 3)


def build_timeline(items: list[Item], plan: ReelPlan, style: Style, topic: TopicIdea, handle: str,
                   lead: float = 0.30, gap: float = 0.40, tail: float = 0.9, card_len: float = 1.0) -> Timeline:
    scenes, voice_at, t = [], [], 0.0
    shots_total = max(it.shot for it in items)
    group_t0 = 0.0
    last_voice = max(i for i, it in enumerate(items) if it.slide is not None)
    for k, it in enumerate(items):
        t0 = t
        if it.slide is None:  # 정적 카드: 목소리 없이 박자 두 칸
            t1 = snap_up(t0 + card_len, style.bpm) if style.bpm else t0 + card_len
            scenes.append({"index": it.shot, "kind": "card", "card": it.card, "heading": "", "body": "", "highlight": "",
                           "bullets": [], "t0": round(t0, 3), "t1": round(t1, 3), "group_t0": round(group_t0, 3),
                           "narration": "", "subs": [], "marks": []})
            t = t1
            continue
        if not it.cont:
            group_t0 = t0
        dur = len(it.voice) / SR
        clean, marks = parse_marks(it.text)
        vs = t0 + (lead + 0.2 if k == 0 else lead)
        t1 = vs + dur + (tail if k == last_voice else gap)
        if style.bpm:
            t1 = snap_up(t1, style.bpm)
        sl = it.slide
        scenes.append({
            "index": it.shot, "kind": sl.kind, "cont": it.cont,
            "heading": sl.heading.strip(), "body": sl.body.strip(), "highlight": (sl.highlight or "").strip(),
            "bullets": bullets_of(sl.body) if sl.kind in ("summary", "cta") else [],
            "t0": round(t0, 3), "t1": round(t1, 3), "group_t0": round(group_t0, 3),
            "voice_t0": round(vs, 3), "voice_t1": round(vs + dur, 3),
            "narration": clean, "subs": split_subtitle(clean, vs, vs + dur),
            # 핵심 어구를 말하는 시각 (글자 위치에 비례): 페이지가 이때 노란 마커를 긋는다
            "marks": [{"text": m, "t": round(vs + dur * pos / max(1, len(clean)), 3)} for m, pos in marks],
        })
        voice_at.append((vs, it.voice))
        t = t1
    # 컷 그룹이 끝나는 시각(진행 막대용)
    for s in scenes:
        s["group_t1"] = max(x["t1"] for x in scenes if x["group_t0"] == s["group_t0"])
    content = {
        "style": plan.style, "beat": style.beats, "bpm": style.bpm, "shots": shots_total,
        "title": (topic.title or items[0].slide.heading).replace("\n", " ").strip(),
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
        if s["kind"] == "card":  # 정적 카드: 낮게 '쿵' 하고 짧게 울린다
            cues += [{"t": t0 + 0.02, "type": "thump"}, {"t": t0 + 0.05, "type": "ding"}]
            continue
        if s.get("cont"):  # 같은 컷을 잇는 뒷부분: 새 컷이 아니므로 컷 소리는 없다
            pass
        elif style.sfx == "slam":
            cues += [{"t": t0 + 0.04, "type": "whoosh"}, {"t": t0 + 0.12, "type": "thump"}]
        elif style.sfx == "chalk":
            cues += [{"t": t0 + 0.05, "type": "pop"}, {"t": t0 + 0.2, "type": "chalk", "d": min(1.6, max(0.6, (s["voice_t1"] - t0) * 0.5))}]
        else:
            cues += [{"t": t0 + 0.0, "type": "tick"}]
        if style.sfx == "tick":
            cues += [{"t": u["t0"], "type": "tick"} for u in s["subs"][1:]]
        if style.beats:  # 노란 마커가 지나가는 순간 '틱'
            cues += [{"t": m["t"], "type": "pop"} for m in s["marks"]]
    return cues


def choose_music_mood(style: Style, text: str) -> str:
    if style.bpm:  # 박자가 정해진 스타일은 늘 그 스타일의 분위기(경쾌한 비트)로
        return style.music
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
        from scipy.signal import fftconvolve

        rng = np.random.default_rng(3)
        ir = rng.standard_normal(int(0.28 * SR)) * np.exp(-np.arange(int(0.28 * SR)) / (0.07 * SR))
        ir /= np.sqrt(np.sum(ir ** 2))
        wet = fftconvolve(x, ir.astype(np.float32))[:n].astype(np.float32)
        x = x * (1 - style.room) + wet * style.room
    return np.clip(x, -0.95, 0.95)


def _music_track(tl: Timeline, style: Style, mood: str, folder: Path) -> tuple[np.ndarray, dict]:
    """곡 라이브러리(music/)에 그 분위기 곡이 있으면 그 곡, 없으면 합성. (스테레오 배열, music.json 내용)
    박자가 정해진 스타일(bpm)은 컷이 박자에 맞아야 하므로 그 빠르기로 합성한 곡을 쓴다."""
    n = int((tl.duration + 0.5) * SR)
    picked = None if style.bpm else bgm.pick_track(folder.name, mood)
    with tempfile.TemporaryDirectory() as td:
        if picked:
            src = bgm.LIBRARY / picked["file"]
            info = {"mood": mood, "track": picked["file"], "credit": bgm.credit(picked)}
        else:
            src = bgm.write_track(Path(td) / "synth.wav", tl.duration + 1, folder.name, mood, bpm=style.bpm or None)
            info = {"mood": mood, "track": "synth", "credit": ""}
            if style.bpm:
                info["bpm"] = style.bpm
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
    music, music_info = _music_track(tl, style, mood, folder)
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
    # 정적 카드: 음악도 거의 꺼서 '정적'이 들리게 하고, 끝나면 박자에 맞춰 다시 들어온다
    for s in tl.content["scenes"]:
        if s["kind"] == "card":
            a, b = int(max(0, s["t0"] - 0.08) * SR), int(s["t1"] * SR)
            cut = np.ones(n)
            cut[a:b] = 0.04
            ramp = int(0.08 * SR)
            cut[max(0, a - ramp):a] = np.linspace(1, 0.04, a - max(0, a - ramp))
            r2 = min(n - b, int(0.05 * SR))
            if r2 > 0:  # 카드가 끝나면 박자에 맞춰 음악이 다시 들어온다 (뚝 끊기는 소리가 나지 않게 짧게 올린다)
                cut[b:b + r2] = np.linspace(0.04, 1, r2)
            gain = gain * cut[:len(gain)]
    mus_peak = float(np.sqrt(np.mean(music ** 2))) or 1.0
    music = music * (0.16 / mus_peak) * 10 ** (style.music_db / 20)
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
        elif ty == "ding":
            snd, g = cache.setdefault("ding", sfx.ding()[: int(0.5 * SR)] * np.linspace(1, 0, int(0.5 * SR))), 0.10
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


def prepare_job(job: Path, style: Style, content: dict, fonts_dir: Path) -> None:
    job.mkdir(parents=True, exist_ok=True)
    shutil.copy(STUDIO / "styles" / (style.page or style.name) / "index.html", job / "index.html")
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


def fit_rates(base: str) -> list[str]:
    """처음 속도 다음에 길이가 넘칠 때 올려 볼 속도들 (기본보다 느린 것은 건너뛴다)."""
    def num(r: str) -> int:
        try:
            return int(r.replace("%", "").replace("+", ""))
        except ValueError:
            return 0
    return [base] + [r for r in RATE_STEPS if num(r) > num(base)]


def fit_timeline(st: Style, shots: list[Slide], plan: ReelPlan, topic: TopicIdea, handle: str, rate: str,
                 max_seconds: float, synth) -> tuple[Timeline, str]:
    """목소리가 길어 상한을 넘으면 말하는 속도를 올려(+15%, +30%) 길이 안에 넣는다. synth(줄 목록, 속도) → 음성 목록."""
    tl, used = None, rate
    for used in fit_rates(rate):
        items = make_items(st, shots, plan)
        speak = [it for it in items if it.slide is not None]
        for it, v in zip(speak, synth([parse_marks(it.text)[0] for it in speak], used)):
            it.voice = v
        tl = build_timeline(items, plan, st, topic, handle)
        if tl.duration <= max_seconds:
            return tl, used
        log.warning("목소리가 %.1f초라 상한 %.0f초를 넘습니다 (속도 %s)", tl.duration, max_seconds, used)
    log.warning("속도를 올려도 상한을 넘습니다 (%.1f초). 그대로 만듭니다", tl.duration)
    return tl, used


def make_reel(card: CardNews, topic: TopicIdea, folder: Path, writer, fonts_dir: Path, handle: str = "@economypub",
              *, style: str = "", voice: str = DEFAULT_VOICE, rate: str = "+0%", fps: int = 30,
              workers: int | None = None, keep_job: bool = False, max_seconds: float = MAX_SECONDS) -> tuple[Path, Path, dict]:
    """(릴스 영상, 커버 이미지, 계획 정보)를 만든다. 실패하면 예외를 던진다 (부르는 쪽이 예전 방식으로 대신한다)."""
    plan = plan_reel(writer, topic, card, forced_style=style)
    st = STYLES[plan.style]
    fps = st.fps or fps
    shots = shots_for(st, card)
    log.info("릴스 스타일: %s (%s) — %s (%d컷, %dfps)", st.name, st.label, plan.reason, len(shots), fps)
    with tempfile.TemporaryDirectory(prefix="lemo_") as td_s:
        td = Path(td_s)
        tl, used_rate = fit_timeline(
            st, shots, plan, topic, handle, rate, max_seconds,
            lambda lines, r: synthesize(lines, td / f"voice_{r}", voice=voice, rate=r),
        )
        text = tl.content["title"] + " " + " ".join(f"{s['heading']} {s['body']}" for s in tl.content["scenes"])
        mix, music_info = mix_audio(tl, st, folder, text)
        wav = td / "mix.wav"
        write_wav(wav, mix)
        job = folder / "lemo_job" if keep_job else td / "job"
        prepare_job(job, st, tl.content, fonts_dir)
        video = render_frames(job, td, fps, workers or min(4, os.cpu_count() or 2))
        out = folder / "reel.mp4"
        mux(video, wav, out, fps)
        cover = folder / "reel_cover.jpg"
        first = next(s for s in tl.content["scenes"] if s["kind"] != "card")
        render_cover(job, td, min(1.2, first["t1"] - 0.1), cover)
    (folder / "music.json").write_text(json.dumps(music_info, ensure_ascii=False), encoding="utf-8")
    info = {"style": st.name, "label": st.label, "reason": plan.reason, "duration": tl.duration, "fps": fps, "rate": used_rate,
            "music": music_info, "narration": [s["narration"] for s in tl.content["scenes"] if s["narration"]],
            "cards": [s["card"] for s in tl.content["scenes"] if s["kind"] == "card"]}
    (folder / "reel_plan.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("릴스 영상 생성: %s (%.1f초, %s)", out.name, tl.duration, st.label)
    return out, cover, info


def check_voice() -> int:
    """실제 edge-tts로 한 문장을 합성해 본다 (Actions에서 목소리가 되는지 미리 확인). 성공 0, 실패 1."""
    import tempfile as _tf

    with _tf.TemporaryDirectory() as td:
        try:
            v = synthesize(["경제주막입니다. 목소리 점검 중이에요."], Path(td), voice=os.environ.get("REELS_VOICE", DEFAULT_VOICE))[0]
        except Exception as e:
            print(f"목소리 합성 실패: {e}", file=sys.stderr)
            return 1
    ok = len(v) / SR > 1.0
    print(f"목소리 합성 {'성공' if ok else '실패(너무 짧음)'}: {len(v) / SR:.1f}초")
    return 0 if ok else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(check_voice() if sys.argv[1:] == ["check-voice"] else 2)
