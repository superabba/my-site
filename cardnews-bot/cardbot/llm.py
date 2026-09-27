"""LLM(Claude 또는 Gemini)으로 주제 추천 / 자료 조사 / 카드뉴스 원고 작성."""

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


class BlogSection(BaseModel):
    heading: str  # 소제목
    body: str  # 문단은 빈 줄(\n\n)로 구분
    image: str  # 넣을 카드뉴스 이미지 파일명 (예: slide_02.jpg), 없으면 빈 문자열
    experience_hint: str  # 글쓴이가 직접 덧붙이면 좋을 경험/의견 안내 (없으면 빈 문자열)


class BlogFAQ(BaseModel):
    question: str
    answer: str


class BlogPost(BaseModel):
    titles: list[str]  # 제목 후보 (첫 번째가 추천)
    main_keyword: str
    sub_keywords: list[str]
    meta_description: str  # 검색 결과 요약문 (티스토리 요약/설명용)
    intro: str
    sections: list[BlogSection]
    faq: list[BlogFAQ]
    conclusion: str
    tags: list[str]  # '#' 없이
    sources: list[str]


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


BLOG_SYSTEM = """당신은 네이버 블로그·티스토리에서 검색 유입이 꾸준한 정보성 글을 쓰는 블로그 에디터입니다.
같은 주제로 이미 만든 카드뉴스와 자료 조사 결과를 바탕으로, 검색하는 사람의 궁금증을 끝까지 해결해 주는 블로그 글 초안을 씁니다.

검색 노출을 위한 원칙:
- 핵심 키워드는 사람들이 검색창에 실제로 입력할 2~4단어 구문으로 정하세요 (예: '잔액'처럼 한 단어로 된 너무 넓은 말 대신 '연휴 카드값 출금일'처럼 구체적인 롱테일). 제목 앞쪽과 도입부 첫 문단, 소제목 1~2곳에 자연스럽게 넣고, 억지로 반복하지 마세요
- 제목 후보 3~5개: 30자 안팎, 구체적 숫자나 대상이 드러나게. 낚시성·과장 표현 금지
- 본문(도입~결론, FAQ 포함)은 공백 포함 2,500~4,000자를 넘지 않게, 소제목 4~6개. 한 문단 2~4문장으로 짧게, 모바일에서 읽기 쉽게. 같은 내용을 다른 말로 되풀이하지 마세요
- 카드뉴스를 그대로 옮기지 말고, 왜 그런지·어떻게 하면 되는지·주의할 점을 더 깊게 풀어 쓰세요
- 자료에 있는 사실만 구체적 숫자로 쓰고, 불확실한 내용은 단정하지 마세요. 날짜가 중요한 정보는 기준 시점을 밝히세요
- 각 소제목마다 어울리는 카드뉴스 이미지 파일명을 image에 지정하세요 (제공된 파일명만 사용, 없으면 빈 문자열)
- experience_hint: 검색엔진과 독자는 직접 겪은 경험을 높게 평가합니다. 글쓴이가 자기 경험·사진·의견을 덧붙이면 좋을 소제목에 무엇을 쓰면 되는지 한 문장으로 안내하세요 (본문에 가짜 경험을 지어내지 마세요)
- faq: 검색에서 자주 나올 질문 3~5개와 짧은 답
- 결론: 핵심 요약 + 행동 제안. 과도한 광고 문구나 '구독/공감 부탁' 반복은 피하세요
- tags: 검색 수요가 있는 한국어 태그 10~20개 ('#' 제외)
- 의료·법률·투자처럼 전문 판단이 필요한 내용은 전문가 상담을 권하는 문장을 포함하세요"""


class BaseWriter:
    """주제 추천 → 자료 조사 → 원고 작성. 모델 호출(_parse, _search)만 제공자별로 다르다."""

    provider = "base"

    def __init__(self, settings):
        self.s = settings
        flag = os.environ.get("WEB_SEARCH") or os.environ.get("CLAUDE_WEB_SEARCH") or "true"
        self.use_web_search = flag.lower() != "false"

    def _parse(self, system: str, user: str, schema: type[T]) -> T:
        """스키마에 맞는 구조화 출력을 받아 검증된 객체로 반환."""
        raise NotImplementedError

    def _search(self, system: str, user: str) -> str | None:
        """웹 검색을 곁들여 자유 형식 텍스트를 반환 (실패하면 None)."""
        raise NotImplementedError

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

        notes = self._search(RESEARCH_SYSTEM, base + "\n\n이 주제의 팩트 시트를 만들어 주세요.")
        if not notes:
            log.warning("자료 조사가 거절/실패하여 헤드라인만 사용합니다")
            return base
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

    # ---------- 4) 블로그 글 ----------

    def write_blog(self, topic: TopicIdea, research_notes: str, card: CardNews, images: list[str]) -> BlogPost:
        slides = "\n".join(
            f"- {img}: [{sl.kind}] {sl.heading.replace(chr(10), ' ')} — {sl.body.replace(chr(10), ' ')}"
            for img, sl in zip(images, card.slides)
        )
        user = f"""계정 니치: {self.s.niche}
말투: {self.s.tone}

## 주제
{json.dumps(topic.model_dump(), ensure_ascii=False, indent=2)}

## 이미 만든 카드뉴스 (이미지 파일명: 내용)
{slides}

## 자료
{research_notes}

위 주제로 블로그 글 초안을 작성하세요."""
        post = self._parse(BLOG_SYSTEM, user, BlogPost)
        return normalize_blog(post, images)


class ClaudeWriter(BaseWriter):
    provider = "claude"

    def __init__(self, settings, client: anthropic.Anthropic | None = None):
        super().__init__(settings)
        self.client = client or anthropic.Anthropic()
        self.use_fallbacks = (os.environ.get("CLAUDE_FALLBACKS") or "default") != "off"

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

    def _search(self, system: str, user: str) -> str | None:
        messages: list = [{"role": "user", "content": user}]
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 6}]
        resp = None
        for _ in range(4):  # 서버 도구가 길어지면 pause_turn으로 끊겨 오므로 이어서 요청
            resp = self.client.beta.messages.create(
                **self._common(), system=system, messages=messages, tools=tools
            )
            if resp.stop_reason != "pause_turn":
                break
            messages = [*messages, {"role": "assistant", "content": resp.content}]
        if resp is None or resp.stop_reason == "refusal":
            return None
        return "".join(b.text for b in resp.content if b.type == "text").strip() or None


class GeminiWriter(BaseWriter):
    """Google Gemini API (google-genai). 웹 검색은 Google 검색 그라운딩을 사용."""

    provider = "gemini"

    def __init__(self, settings, client=None):
        super().__init__(settings)
        if client is None:
            from google import genai  # 선택 의존성: LLM_PROVIDER=gemini일 때만 필요

            client = genai.Client()  # GEMINI_API_KEY 환경변수 사용
        self.client = client

    def _generate(self, system: str, user: str, **config):
        from google.genai import types

        resp = self.client.models.generate_content(
            model=self.s.gemini_model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                # 함수 도구를 쓰지 않으므로 자동 함수 호출은 끈다
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                **config,
            ),
        )
        cand = (resp.candidates or [None])[0]
        reason = str(getattr(cand, "finish_reason", "") or "")
        if cand is None or any(r in reason for r in ("SAFETY", "BLOCKLIST", "PROHIBITED", "RECITATION")):
            feedback = getattr(resp, "prompt_feedback", None)
            raise RuntimeError(f"Gemini가 응답을 차단했습니다: {reason or feedback}")
        return resp, cand, reason

    def _parse(self, system: str, user: str, schema: type[T]) -> T:
        resp, _, reason = self._generate(
            system, user, response_mime_type="application/json", response_schema=schema
        )
        parsed = resp.parsed
        if isinstance(parsed, schema):
            return parsed
        try:
            return schema.model_validate_json(resp.text or "")
        except ValueError as e:
            raise RuntimeError(f"구조화 출력 파싱 실패 (finish_reason={reason}): {e}") from e

    def _search(self, system: str, user: str) -> str | None:
        from google.genai import types

        try:
            resp, cand, _ = self._generate(
                system, user, tools=[types.Tool(google_search=types.GoogleSearch())]
            )
        except RuntimeError as e:
            log.warning("%s", e)
            return None
        text = (resp.text or "").strip()
        if not text:
            return None
        # 그라운딩에 쓰인 웹 출처를 덧붙인다
        chunks = getattr(getattr(cand, "grounding_metadata", None), "grounding_chunks", None) or []
        sources = []
        for ch in chunks:
            web = getattr(ch, "web", None)
            if web and web.uri:
                line = f"- {web.title or web.domain or ''} {web.uri}".strip()
                if line not in sources:
                    sources.append(line)
        if sources:
            text += "\n\n### 검색 출처\n" + "\n".join(sources)
        return text


# 기존 코드 호환용 별칭
Writer = ClaudeWriter


def make_writer(settings) -> BaseWriter:
    provider = settings.llm_provider
    if provider == "claude":
        return ClaudeWriter(settings)
    if provider == "gemini":
        return GeminiWriter(settings)
    raise ValueError(f"알 수 없는 LLM_PROVIDER: {provider} (claude | gemini)")


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


def normalize_blog(post: BlogPost, images: list[str]) -> BlogPost:
    """존재하지 않는 이미지 참조 제거, 태그 정리 (네이버 최대 30개)."""
    allowed = set(images)
    sections = [
        sec.model_copy(update={"image": sec.image if sec.image in allowed else ""})
        for sec in post.sections
    ]
    tags = []
    for t in post.tags:
        t = t.strip().lstrip("#").replace(" ", "")
        if t and t not in tags:
            tags.append(t)
    if not post.titles or not sections:
        raise ValueError("블로그 글에 제목이나 본문이 없습니다")
    return post.model_copy(update={"sections": sections, "tags": tags[:30]})


def instagram_caption(card: CardNews) -> str:
    tags = " ".join(f"#{t}" for t in card.hashtags)
    cap = f"{card.caption.strip()}\n\n{tags}".strip()
    return cap[:2200]
