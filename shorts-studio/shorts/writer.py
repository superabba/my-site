"""Claude API로 주제를 추천하고 쇼츠 대본을 씁니다. AI 없이 글을 장면으로 나누는 기능도 있습니다."""

from __future__ import annotations

import datetime as _dt
import json
import re
import uuid

import anthropic

# 한국어 TTS가 1초에 읽는 글자 수(공백 제외, 장면 사이 쉼 포함, 말 빠르기 +10% 기준). 대본 길이를 맞추는 데 씁니다.
# 실측: 147자 → 24.1초, 239자 → 40.8초 (약 6자/초)
CHARS_PER_SEC = 6.0

CATEGORIES = {
    "일기": "1인칭 일상 기록. 솔직하고 따뜻한 감정, 구체적인 장면 묘사, 마지막엔 작은 깨달음이나 여운.",
    "정보": "첫 장면에 강한 후킹(질문·의외의 사실). 이어서 핵심 정보 3~5개를 짧고 명확하게. 마지막은 요약과 저장·팔로우 유도.",
    "동기부여": "공감되는 고민으로 시작해 관점을 바꿔주는 메시지. 짧고 힘 있는 문장, 마지막에 행동을 권하는 한 마디.",
    "이야기": "짧은 썰·스토리텔링. 상황 설정 → 전개 → 반전이나 여운. 듣는 사람이 다음 장면을 궁금해하게.",
    "리뷰·추천": "제품·장소·책·앱 등 추천. 누구에게 좋은지, 핵심 장점, 솔직한 아쉬운 점 하나, 한 줄 총평.",
}

TONES = {
    "존댓말 (~해요)": "존댓말(~해요, ~이에요)",
    "반말 (~했다)": "반말 독백체(~했다, ~이다)",
    "친구 말투 (~했어)": "친근한 반말(~했어, ~거든)",
}

MOODS = {
    "calm": "잔잔함",
    "emotional": "감성",
    "upbeat": "경쾌함",
    "energetic": "신남",
    "mysterious": "신비로움",
}


class WriterError(Exception):
    """사용자에게 보여줄 한국어 메시지를 담은 오류."""


def _client(api_key: str) -> anthropic.Anthropic:
    if not api_key:
        raise WriterError("Anthropic API 키가 없어요. 왼쪽 설정에서 키를 입력하고 저장해 주세요.")
    return anthropic.Anthropic(api_key=api_key)


def _call_json(api_key: str, model: str, system: str, user: str, schema: dict, effort: str) -> dict:
    client = _client(api_key)
    kwargs = {}
    # 안전 분류기가 무해한 요청을 잘못 거절할 때 다른 모델로 자동 재시도합니다.
    if model in ("claude-opus-5-5", "claude-sonnet-5-5"):
        kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    try:
        resp = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            **kwargs,
        )
    except anthropic.AuthenticationError:
        raise WriterError("API 키가 올바르지 않아요. 설정에서 키를 다시 확인해 주세요.")
    except anthropic.PermissionDeniedError:
        raise WriterError("이 API 키로는 해당 모델을 쓸 권한이 없어요.")
    except anthropic.RateLimitError:
        raise WriterError("요청이 너무 많아요. 잠시 후 다시 시도해 주세요.")
    except anthropic.BadRequestError as e:
        raise WriterError(f"요청 형식 오류: {e.message}")
    except anthropic.APIStatusError as e:
        if e.status_code >= 500:
            raise WriterError("Claude 서버에 일시적인 문제가 있어요. 잠시 후 다시 시도해 주세요.")
        raise WriterError(f"API 오류 ({e.status_code}): {e.message}")
    except anthropic.APIConnectionError:
        raise WriterError("인터넷 연결을 확인해 주세요. Claude 서버에 연결하지 못했어요.")

    if resp.stop_reason == "refusal":
        raise WriterError("AI가 이 주제로는 글을 쓰지 않았어요. 주제나 표현을 바꿔서 다시 시도해 주세요.")
    if resp.stop_reason == "max_tokens":
        raise WriterError("대본이 너무 길어 중간에 끊겼어요. 영상 길이를 줄여서 다시 시도해 주세요.")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise WriterError("AI 응답을 읽지 못했어요. 다시 시도해 주세요.")


# ---------------------------------------------------------------- 주제 추천

TOPIC_SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "hook": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["title", "hook", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["topics"],
    "additionalProperties": False,
}


def suggest_topics(api_key: str, model: str, category: str, hint: str = "", count: int = 6) -> list[dict]:
    today = _dt.date.today()
    system = (
        "당신은 한국 유튜브 쇼츠·인스타그램 릴스 기획자입니다. "
        "30~60초 세로 영상으로 만들기 좋은, 구체적이고 반응이 좋을 주제를 제안합니다."
    )
    user = (
        f"오늘 날짜: {today.isoformat()} (계절·시기를 고려해도 좋아요)\n"
        f"카테고리: {category} — {CATEGORIES.get(category, '')}\n"
        f"사용자 관심사/힌트: {hint or '(없음)'}\n\n"
        f"쇼츠 주제를 {count}개 제안해 주세요. 서로 겹치지 않게 다양하게.\n"
        "- title: 영상 주제 (25자 이내, 구체적으로)\n"
        "- hook: 영상 첫 문장 예시 (시선을 잡는 한 문장)\n"
        "- why: 이 주제가 좋은 이유 한 줄"
    )
    data = _call_json(api_key, model, system, user, TOPIC_SCHEMA, effort="low")
    return data.get("topics", [])[:count]


# ---------------------------------------------------------------- 대본 작성

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "mood": {"type": "string", "enum": list(MOODS)},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "visual_style": {"type": "string"},
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "narration": {"type": "string"},
                    "headline": {"type": "string"},
                    "keywords": {"type": "string"},
                    "image_prompt": {"type": "string"},
                },
                "required": ["narration", "headline", "keywords", "image_prompt"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "mood", "caption", "hashtags", "visual_style", "scenes"],
    "additionalProperties": False,
}

SCRIPT_SYSTEM = """당신은 한국어 유튜브 쇼츠·인스타그램 릴스 대본 작가입니다. 9:16 세로 영상용 대본을 장면(scene) 단위로 씁니다.

각 장면의 필드:
- narration: 성우(TTS)가 소리 내어 읽고, 화면 자막으로도 나오는 문장. 한 장면에 1~2문장, 자연스러운 구어체.
  이모지·해시태그·괄호·특수기호는 넣지 마세요(TTS가 그대로 읽습니다).
- headline: 화면 위쪽에 크게 뜨는 핵심 문구. 4~14자. 장면의 요점을 한눈에. 필요 없으면 빈 문자열.
- keywords: 배경 영상·사진을 찾기 위한 영어 검색어 2~4단어 (예: "morning coffee window").
  사람 얼굴 클로즈업보다 분위기·사물·풍경 위주로, 장면 내용과 어울리게.
- image_prompt: 이 장면 배경 그림을 AI 이미지 모델(나노바나나)에 그려 달라고 할 영어 설명 1~2문장.
  narration 내용을 한눈에 떠올리게 하는 구체적인 장면(사물·공간·상황·은유)으로. 글자·숫자·로고·실존 인물은 넣지 말 것.

전체 규칙:
- 첫 장면은 1~2초 안에 시선을 잡는 후킹으로 시작합니다.
- 내용이 사실 정보라면 정확한 것만 쓰고, 불확실한 수치는 쓰지 않습니다.
- title: 영상 제목(30자 이내). mood: 배경음악 분위기.
- caption: 게시물 설명 1~2문장. hashtags: 5~8개, '#' 없이 단어만.
- visual_style: 모든 장면 그림에 공통으로 쓸 영어 화풍 설명 한 줄 (영상 전체가 한 톤으로 보이게).
- 돈·투자 이야기는 특정 종목·상품을 사라고 권하지 말고, 수익을 장담하지 않습니다.
- <조사 자료>가 주어지면 숫자·날짜·제도 같은 사실은 그 안에 있는 것만 씁니다."""


def target_chars(length_sec: int) -> int:
    return int(length_sec * CHARS_PER_SEC)


def narration_chars(script: dict) -> int:
    """대본 전체 내레이션 글자 수 (공백 제외)."""
    return sum(len(sc.get("narration", "").replace(" ", "")) for sc in script.get("scenes", []))


def _length_spec(length_sec: int) -> str:
    chars = target_chars(length_sec)
    scenes = max(3, round(length_sec / 5))
    return (f"영상 길이 약 {length_sec}초 → narration 전체 글자 수(공백 제외) 약 {chars}자, "
            f"최대 {int(chars * 1.1)}자를 넘기지 마세요. 장면 {scenes}개 내외.")


def write_script(
    api_key: str,
    model: str,
    topic: str,
    category: str,
    tone: str,
    length_sec: int,
    notes: str = "",
    facts: str = "",
) -> dict:
    user = (
        (f"<조사 자료>\n{facts}\n</조사 자료>\n\n" if facts else "")
        + f"주제: {topic}\n"
        f"카테고리: {category} — {CATEGORIES.get(category, '')}\n"
        f"말투: {TONES.get(tone, tone)}\n"
        f"{_length_spec(length_sec)}\n"
        f"추가 요청: {notes or '(없음)'}"
    )
    data = _call_json(api_key, model, SCRIPT_SYSTEM, user, SCRIPT_SCHEMA, effort="medium")
    return normalize_script(data)


def polish_text(
    api_key: str,
    model: str,
    text: str,
    category: str,
    tone: str,
    length_sec: int,
    notes: str = "",
) -> dict:
    user = (
        "아래 <원문>은 사용자가 직접 쓴 글입니다. 원문의 내용과 감정, 고유한 표현을 최대한 살려서 "
        "쇼츠 대본으로 다듬어 주세요. 원문에 없는 사실을 지어내지 마세요.\n\n"
        f"<원문>\n{text}\n</원문>\n\n"
        f"카테고리: {category} — {CATEGORIES.get(category, '')}\n"
        f"말투: {TONES.get(tone, tone)}\n"
        f"{_length_spec(length_sec)}\n"
        f"추가 요청: {notes or '(없음)'}"
    )
    data = _call_json(api_key, model, SCRIPT_SYSTEM, user, SCRIPT_SCHEMA, effort="medium")
    return normalize_script(data)


def shorten_script(api_key: str, model: str, script: dict, length_sec: int, facts: str = "") -> dict:
    """너무 긴 대본을 내용·말투·사실은 그대로 두고 목표 길이로 줄인다."""
    have, limit = narration_chars(script), int(target_chars(length_sec) * 1.05)
    draft = json.dumps({k: script.get(k) for k in ("title", "mood", "caption", "hashtags", "visual_style")}
                       | {"scenes": [{k: sc.get(k, "") for k in ("narration", "headline", "keywords", "image_prompt")}
                                     for sc in script.get("scenes", [])]}, ensure_ascii=False)
    user = (
        (f"<조사 자료>\n{facts}\n</조사 자료>\n\n" if facts else "")
        + f"아래 대본은 narration이 공백 제외 {have}자라서 {length_sec}초 영상에 너무 깁니다.\n"
        f"가장 중요한 내용만 남겨 narration 전체를 공백 제외 {limit}자 이내로 줄여 주세요. "
        "말투·사실·첫 장면의 후킹·마지막 장면의 저장·팔로우 권유는 유지하고, 장면 수는 줄여도 됩니다.\n\n"
        f"<대본>\n{draft}\n</대본>"
    )
    data = _call_json(api_key, model, SCRIPT_SYSTEM, user, SCRIPT_SCHEMA, effort="medium")
    return normalize_script(data)


# ---------------------------------------------------------------- AI 없이 장면 나누기

_SENT_RE = re.compile(r"[^.!?。…\n]+[.!?。…]*")


def split_text(text: str, title: str = "", mood: str = "calm", target_chars: int = 40) -> dict:
    """글을 문장 단위로 잘라 장면 목록을 만듭니다. 짧은 문장은 합칩니다."""
    sentences = [s.strip() for s in _SENT_RE.findall(text) if s.strip()]
    scenes: list[str] = []
    buf = ""
    for s in sentences:
        if buf and len(buf) + len(s) > target_chars:
            scenes.append(buf)
            buf = s
        else:
            buf = f"{buf} {s}".strip()
    if buf:
        scenes.append(buf)
    data = {
        "title": title or (scenes[0][:20] if scenes else "제목 없음"),
        "mood": mood,
        "caption": "",
        "hashtags": [],
        "scenes": [
            {"narration": s, "headline": title if i == 0 else "", "keywords": "", "image_prompt": ""}
            for i, s in enumerate(scenes)
        ],
    }
    return normalize_script(data)


def new_scene(narration: str = "", headline: str = "", keywords: str = "", image_prompt: str = "") -> dict:
    return {
        "id": uuid.uuid4().hex[:8],
        "narration": narration,
        "headline": headline,
        "keywords": keywords,
        "image_prompt": image_prompt,
        "media": None,
    }


def normalize_script(data: dict) -> dict:
    mood = data.get("mood") if data.get("mood") in MOODS else "calm"
    return {
        "title": data.get("title", "").strip(),
        "mood": mood,
        "caption": data.get("caption", "").strip(),
        "hashtags": [h.lstrip("#").strip() for h in data.get("hashtags", []) if h.strip()],
        "visual_style": data.get("visual_style", "").strip(),
        "scenes": [
            new_scene(s.get("narration", "").strip(), s.get("headline", "").strip(), s.get("keywords", "").strip(),
                      s.get("image_prompt", "").strip())
            for s in data.get("scenes", [])
            if s.get("narration", "").strip() or s.get("headline", "").strip()
        ],
    }
