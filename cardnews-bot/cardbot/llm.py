"""Claude로 주제 추천 / 자료 조사 / 카드뉴스 원고 작성."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Literal, TypeVar

import anthropic
from pydantic import BaseModel

from .trends import TrendSignal

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

MAX_TOKENS = 16000
FALLBACK_BETA = "server-side-fallback-2026-07-01"


# ---------- 출력 스키마 ----------


class TopicIdea(BaseModel):
    title: str  # 카드뉴스 작업 제목
    keyword: str  # 근거가 된 트렌드 키워드
    angle: str  # 어떤 관점/형식으로 풀지 (예: 체크리스트, 비교, 오해와 진실)
    hook: str  # 첫 장에 들어갈 후킹 문구 초안
    why_now: str  # 지금 조회수가 나올 이유
    score: int  # 0~100, 예상 반응 점수
    risk: str  # 사실확인·민감성 리스크 (없으면 빈 문자열)


class TopicList(BaseModel):
    topics: list[TopicIdea]


class Slide(BaseModel):
    kind: Literal["cover", "content", "summary", "cta"]
    heading: str
    body: str
    highlight: str  # 크게 강조할 짧은 문구나 숫자 (없으면 빈 문자열)


class CardNews(BaseModel):
    slides: list[Slide]
    caption: str  # 인스타그램 캡션 (해시태그 제외)
    hashtags: list[str]  # '#' 없이
    threads_text: str  # 스레드 본문 (500자 이내)
    sources: list[str]  # 참고한 출처 URL/매체명


# ---------- 프롬프트 ----------

TOPIC_SYSTEM = """당신은 인스타그램·스레드 카드뉴스 계정의 편집장입니다.
실시간 트렌드 신호와 이 계정의 과거 성과를 보고, 지금 올리면 조회수·저장·공유가 가장 많이 나올 카드뉴스 주제를 고릅니다.

좋은 주제의 조건:
- 지금 사람들이 검색·대화하는 이슈와 연결되지만, 단순 속보 재전달이 아니라 "알아두면 쓸모 있는" 정리·해설·체크리스트로 풀 수 있다
- 첫 장만 보고도 넘겨보고 싶은 궁금증(숫자, 반전, 손해/이득, 오해 바로잡기)을 만들 수 있다
- 계정의 니치와 맞고, 과거에 잘 된 게시물의 패턴과 닮았다
- 저장하거나 친구에게 공유할 이유가 있다

피해야 할 주제: 사건·사고 피해자나 참사를 소비하는 내용, 특정 개인의 사생활·루머, 확인되지 않은 의혹, 정치적 편가르기, 근거 없는 의료·투자 조언.
과거에 이미 다룬 주제와 겹치면 제외하세요. score는 서로 비교 가능하도록 분산을 두어 매기세요."""

RESEARCH_SYSTEM = """당신은 카드뉴스 제작을 위한 자료 조사 담당입니다.
웹 검색으로 주제에 관한 최신 사실을 확인하고, 카드뉴스 원고에 쓸 수 있는 팩트 시트를 한국어로 작성하세요.
각 사실 옆에 출처(매체명과 URL)를 붙이고, 확인되지 않거나 엇갈리는 정보는 그렇다고 명시하세요. 불필요한 서론 없이 팩트 시트만 출력하세요."""

WRITER_SYSTEM = """당신은 조회수와 저장 수가 높은 카드뉴스를 만드는 카피라이터입니다.
인스타그램 캐러셀(1080x1350) 한 세트와, 같은 내용을 스레드용 글로 씁니다.

원칙:
- 1장(cover): 스크롤을 멈추게 하는 한 줄. heading 16자 안팎, highlight에 핵심 숫자/키워드
- 중간 장(content): 한 장에 메시지 하나. heading 18자 이내, body 90자 이내, 짧은 문장
- 마지막 전 장(summary): 핵심 요약 또는 체크리스트
- 마지막 장(cta): 저장·공유·팔로우를 자연스럽게 유도
- 팩트 시트나 제공된 자료에 있는 사실만 구체적 숫자로 쓰고, 불확실하면 단정하지 마세요
- 이모지는 절제해서 사용. 줄바꿈이 필요하면 \\n 사용
- caption: 첫 줄에 후킹, 본문 요약, 저장/공유 유도. 해시태그는 hashtags 필드에만
- hashtags: 검색 수요가 있는 한국어 해시태그 8~15개 ('#' 제외)
- threads_text: 스레드는 텍스트가 먼저 읽히므로 첫 문장에 궁금증을 만들고, 450자 이내로
- sources: 참고한 출처"""


class Writer:
    def __init__(self, settings, client: anthropic.Anthropic | None = None):
        self.s = settings
        self.client = client or anthropic.Anthropic()
        self.use_fallbacks = os.environ.get("CLAUDE_FALLBACKS", "default") != "off"
        self.use_web_search = os.environ.get("CLAUDE_WEB_SEARCH", "true").lower() != "false"

    # 공통 인자: 적응형 사고 + effort + (Claude API일 때) 거절 시 서버측 폴백
    def _common(self) -> dict:
        kw: dict = {
            "model": self.s.anthropic_model,
            "max_tokens": MAX_TOKENS,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.s.effort},
        }
        if self.use_fallbacks:
            kw["betas"] = [FALLBACK_BETA]
            kw["fallbacks"] = "default"
        return kw

    def _parse(self, system: str, user: str, schema: type[T]) -> T:
        resp = self.client.beta.messages.parse(
            **self._common(),
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"Claude가 요청을 거절했습니다: {resp.stop_details}")
        if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
            raise RuntimeError(f"구조화 출력 파싱 실패 (stop_reason={resp.stop_reason})")
        return resp.parsed_output

    # ---------- 1) 주제 추천 ----------

    def recommend(
        self,
        signals: list[TrendSignal],
        top_posts: list[dict],
        recent_topics: list[str],
        n: int = 5,
    ) -> list[TopicIdea]:
        signal_lines = "\n".join(s.to_prompt_line() for s in signals[:120]) or "(수집된 신호 없음)"
        perf = "\n".join(
            f"- {p['topic']} | 조회 {p.get('views', 0):,} · 좋아요 {p.get('likes', 0):,} · "
            f"저장 {p.get('saves', 0):,} · 공유 {p.get('shares', 0):,} ({p['platform']})"
            for p in top_posts
        ) or "(아직 데이터 없음)"
        recent = "\n".join(f"- {t}" for t in recent_topics) or "(없음)"
        user = f"""현재 시각: {datetime.now():%Y-%m-%d %H:%M} (지역: {self.s.geo})
계정 니치: {self.s.niche}

## 실시간 트렌드 신호
{signal_lines}

## 이 계정에서 반응이 좋았던 과거 게시물
{perf}

## 최근 이미 다룬 주제 (중복 금지)
{recent}

위 정보를 바탕으로 지금 만들 카드뉴스 주제 {n}개를 score 높은 순으로 추천하세요."""
        topics = self._parse(TOPIC_SYSTEM, user, TopicList).topics
        return sorted(topics, key=lambda t: t.score, reverse=True)[:n]

    # ---------- 2) 자료 조사 (웹 검색) ----------

    def research(self, topic: TopicIdea, signals: list[TrendSignal]) -> str:
        related = [s for s in signals if s.keyword == topic.keyword]
        ctx = "\n".join(f"- {c}" for s in related for c in s.context) or "(없음)"
        base = f"주제: {topic.title}\n키워드: {topic.keyword}\n관점: {topic.angle}\n관련 헤드라인:\n{ctx}"
        if not self.use_web_search:
            return base

        messages: list = [{"role": "user", "content": base + "\n\n이 주제의 팩트 시트를 만들어 주세요."}]
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 6}]
        resp = None
        for _ in range(4):  # 서버 도구가 길어지면 pause_turn으로 끊겨 오므로 이어서 요청
            resp = self.client.beta.messages.create(
                **self._common(), system=RESEARCH_SYSTEM, messages=messages, tools=tools
            )
            if resp.stop_reason != "pause_turn":
                break
            messages = [*messages, {"role": "assistant", "content": resp.content}]
        if resp is None or resp.stop_reason == "refusal":
            log.warning("자료 조사가 거절/실패하여 헤드라인만 사용합니다")
            return base
        notes = "".join(b.text for b in resp.content if b.type == "text").strip()
        return f"{base}\n\n## 팩트 시트\n{notes}"

    # ---------- 3) 원고 작성 ----------

    def write(self, topic: TopicIdea, research_notes: str) -> CardNews:
        user = f"""계정 니치: {self.s.niche}
말투: {self.s.tone}
계정 핸들: {self.s.brand_handle or '(없음)'}
슬라이드 수: {self.s.slides_min}~{self.s.slides_max}장 (cover 1장, cta 1장 포함)

## 주제
{json.dumps(topic.model_dump(), ensure_ascii=False, indent=2)}

## 자료
{research_notes}

위 주제로 카드뉴스 원고를 작성하세요."""
        card = self._parse(WRITER_SYSTEM, user, CardNews)
        return normalize(card, self.s.slides_min, self.s.slides_max)


def normalize(card: CardNews, lo: int, hi: int) -> CardNews:
    """플랫폼 제약에 맞게 정리 (캐러셀 2~10장, 스레드 500자, 해시태그 30개)."""
    slides = card.slides[:hi]
    if slides and slides[-1].kind != "cta" and any(s.kind == "cta" for s in card.slides):
        slides[-1] = next(s for s in reversed(card.slides) if s.kind == "cta")
    if len(slides) < max(2, lo):
        raise ValueError(f"슬라이드가 너무 적습니다: {len(slides)}장")
    tags = []
    for t in card.hashtags:
        t = t.strip().lstrip("#").replace(" ", "")
        if t and t not in tags:
            tags.append(t)
    threads = card.threads_text.strip()
    if len(threads) > 500:
        threads = threads[:499].rstrip() + "…"
    return card.model_copy(update={"slides": slides, "hashtags": tags[:30], "threads_text": threads})


def instagram_caption(card: CardNews) -> str:
    tags = " ".join(f"#{t}" for t in card.hashtags)
    cap = f"{card.caption.strip()}\n\n{tags}".strip()
    return cap[:2200]
