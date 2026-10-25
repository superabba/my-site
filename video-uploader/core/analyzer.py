"""Claude로 영상 프레임(+음성 자막)을 보고 SNS 게시 문구와 네이버 블로그 글 초안을 만든다.

- analyze_video: 유튜브·인스타·스레드 문구와 해시태그
- research_topic: 영상 주제와 관련된 정보를 웹 검색으로 모은 자료 (카드뉴스 봇의 자료 조사와 같은 방식)
- write_blog: 영상 내용 + SNS 문구·해시태그 + 자료로 블로그 글을 쓰고, 글에 넣을 장면(초)을 고른다
"""

import base64
import json
import re
from pathlib import Path

import anthropic

# 서버 측 대체 모델(fallbacks)을 지원하는 모델. Haiku는 지원하지 않는다.
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}

SYSTEM_PROMPT = """너는 유튜브 쇼츠·인스타그램 릴스·스레드 게시 문구를 쓰는 한국어 SNS 에디터다.
영상에서 일정 간격으로 뽑은 프레임 이미지와 (있다면) 음성 자막을 보고, 영상에 실제로 나오는 내용만 근거로 문구를 쓴다.
보이지 않는 사실(장소 이름, 제품명, 인물 이름 등)은 지어내지 말고, 사용자가 메모로 알려준 정보는 적극 활용한다.

작성 기준:
- title: 유튜브 쇼츠 제목. 호기심을 끄는 한 줄, 공백 포함 40자 안팎(최대 80자). 해시태그와 이모지는 넣지 않는다.
- youtube_description: 2~4문장으로 영상 내용을 설명. 해시태그는 넣지 않는다(따로 붙인다).
- instagram_caption: 인스타그램 말투로 2~5줄. 이모지를 1~3개 정도 자연스럽게 사용. 해시태그는 넣지 않는다.
- threads_text: 스레드용 글. 친구에게 말하듯 편한 대화체로 1~3문장, 300자 이내. 해시태그는 넣지 않는다.
- hashtags: 영상과 관련된 해시태그 6~12개. '#' 기호 없이 단어만. 한국어 위주로 하되 널리 쓰이는 영어 태그를 섞어도 된다. 'shorts', 'reels' 같은 플랫폼 태그는 넣지 않는다.
- summary: 사용자가 확인할 수 있도록 영상에 무엇이 나오는지 1~2문장 요약."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "영상 내용 요약 1~2문장"},
        "title": {"type": "string", "description": "유튜브 쇼츠 제목"},
        "youtube_description": {"type": "string", "description": "유튜브 설명(해시태그 제외)"},
        "instagram_caption": {"type": "string", "description": "인스타그램 캡션(해시태그 제외)"},
        "threads_text": {"type": "string", "description": "스레드 글(해시태그 제외)"},
        "hashtags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "'#' 없는 해시태그 단어 목록",
        },
    },
    "required": ["summary", "title", "youtube_description", "instagram_caption", "threads_text", "hashtags"],
    "additionalProperties": False,
}

RESEARCH_SYSTEM = """너는 네이버 블로그 글을 쓰기 전에 자료를 모으는 한국어 조사원이다.
사용자가 준 영상 정보(제목·설명·해시태그·메모·영상 요약)에서 글의 주제를 정하고,
웹 검색으로 그 주제와 관련해 독자에게 쓸모 있는 정보를 모은다.
예: 장소라면 위치·운영 시간·요금·주차·가 볼 만한 곳, 행사라면 일정·프로그램, 활동이라면 방법·준비물·주의할 점, 음식이라면 메뉴·특징.
검색 결과에서 확인한 사실만 쓰고, 날짜·가격·시간처럼 바뀌는 정보에는 기준 시점을 붙인다.
결과는 한국어 글머리표로 짧게: 맨 위에 '주제: …' 한 줄, 그 아래 사실 6~15개(각 줄 끝에 출처 URL), 마지막에 '확인 못 한 것: …'."""

BLOG_SYSTEM = """너는 네이버 블로그 '슈퍼아빠'의 글을 쓰는 아빠 블로거다. 가족과 다녀온 곳·해 본 것을 기록하면서,
같은 걸 찾는 사람에게 도움이 되는 정보를 함께 담는 블로그다.

글의 주제는 영상 내용과 유튜브 제목·설명·인스타 캡션·해시태그에서 정한다. 영상 장면만 나열하지 말고, 그 주제에 관한 글을 쓴다.
구성:
1. 도입: 우리 가족이 영상 속에서 한 일을 짧게 (영상·사진에 실제로 보이는 것과 메모에 있는 것만).
2. 본문: '## '로 시작하는 소제목 2~5개. 우리 경험과 함께, 주어진 자료에 있는 주제 관련 정보(장소·행사·방법·팁 등)를 풀어 쓴다.
3. 마무리: 느낀 점과 추천 한두 문장.
4. 자료를 썼다면 맨 끝에 '참고한 자료' 소제목과 출처 링크 목록.
말투는 다정한 존댓말(~했어요), 길이는 공백 포함 1,200~2,500자. 본문에 해시태그와 이모지 남용은 하지 않는다.

사실 규칙: 가족·장소·사람에 대한 구체적인 사실은 영상·자막·메모에 있는 것만 쓴다.
일반 정보(운영 시간·요금·방법 등)는 주어진 자료에 있는 것만 쓰고, 자료가 없으면 단정하지 말고 일반적인 이야기로만 쓴다.

사진: 영상의 장면 목록에서 글에 넣을 장면을 고른다. photos에 고른 장면의 초(time)와 짧은 사진 설명(caption)을 글에 나오는 순서대로 넣고,
본문에는 그 사진 자리를 [사진 1], [사진 2] … 로 photos 순서와 같게, 각각 한 번씩, 그 줄에 그것만 쓴다.
사진은 그 장면을 이야기하는 문단 바로 근처에 둔다. 사진 수는 사용자가 알려 준 범위를 지킨다.
영상을 넣으라고 하면 [영상]을 도입부 다음에 한 줄로 한 번 쓴다.
tags: 블로그 태그 5~10개, '#' 없이 띄어쓰기 없이 (주제·장소·활동 위주)."""

BLOG_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "블로그 제목, 검색어를 앞쪽에, 25~40자"},
        "body": {"type": "string", "description": "본문 ([사진 N], [영상] 자리 표시 포함)"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "'#' 없는 태그"},
        "photos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "time": {"type": "number", "description": "사진으로 쓸 장면의 초"},
                    "caption": {"type": "string", "description": "사진 설명 한 줄"},
                },
                "required": ["time", "caption"],
                "additionalProperties": False,
            },
            "description": "본문에 [사진 1]부터 차례로 들어갈 장면",
        },
    },
    "required": ["title", "body", "tags", "photos"],
    "additionalProperties": False,
}

PHOTO_LINE = re.compile(r"^\s*\[사진\s*(\d+)\]\s*$")
VIDEO_LINE = re.compile(r"^\s*\[영상\]\s*$")


class AnalysisError(Exception):
    """fatal=True: 키·크레딧처럼 다시 해도 안 되는 문제 (웹 자료 단계에서도 숨기지 않고 바로 알린다)."""

    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


BILLING_URL = "https://console.anthropic.com/settings/billing"


def _org(e: anthropic.APIStatusError) -> str:
    try:
        return e.response.headers.get("anthropic-organization-id") or ""
    except AttributeError:
        return ""


def _image_block(path: Path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def _client(api_key: str) -> anthropic.Anthropic:
    if not api_key:
        raise AnalysisError("설정 탭에서 Claude API 키를 먼저 입력해 주세요.")
    return anthropic.Anthropic(api_key=api_key)


def _request(model: str, **kw) -> dict:
    request = dict(model=model, max_tokens=16000, **kw)
    if model in _FALLBACK_MODELS:
        # 안전 분류기가 요청을 거절하면 서버가 다른 모델로 자동 재시도한다.
        request["betas"] = ["server-side-fallback-2026-07-01"]
        request["fallbacks"] = "default"
    return request


def _send(client: anthropic.Anthropic, request: dict):
    try:
        response = client.beta.messages.create(**request)
    except anthropic.AuthenticationError:
        raise AnalysisError("Claude API 키가 올바르지 않습니다. 설정 탭에서 다시 확인해 주세요.", fatal=True)
    except anthropic.PermissionDeniedError:
        raise AnalysisError("이 API 키로는 선택한 모델을 사용할 수 없습니다.", fatal=True)
    except anthropic.RateLimitError:
        raise AnalysisError("요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.")
    except anthropic.BadRequestError as e:
        if "credit balance" in str(e.message).lower():
            org = _org(e)
            raise AnalysisError(
                "Claude API 크레딧이 부족합니다. Anthropic 콘솔의 Plans & Billing에서 충전해 주세요"
                + (f" — 이 키는 조직 {org} 에 속해 있으니, 충전도 이 조직에 해야 합니다" if org else "")
                + f". ({BILLING_URL})", fatal=True)
        raise AnalysisError(f"요청 오류: {e.message}")
    except anthropic.APIStatusError as e:
        raise AnalysisError(f"Claude 서버 오류({e.status_code}). 잠시 후 다시 시도해 주세요.")
    except anthropic.APIConnectionError:
        raise AnalysisError("인터넷 연결을 확인해 주세요.")
    if response.stop_reason == "refusal":
        raise AnalysisError("Claude가 이 영상의 글 작성을 거절했습니다. 내용을 직접 입력해 주세요.")
    if response.stop_reason == "max_tokens":
        raise AnalysisError("응답이 너무 길어 잘렸습니다. 다시 시도해 주세요.")
    return response


def _json(response) -> dict:
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise AnalysisError("Claude 응답을 해석하지 못했습니다. 다시 시도해 주세요.")


def _clean_tags(tags) -> list[str]:
    return [h.strip().lstrip("#").replace(" ", "") for h in tags or [] if h.strip().lstrip("#")]


def _frames_content(frames: list[tuple[float, Path]], duration: float, transcript: str | None) -> list[dict]:
    content: list[dict] = [{
        "type": "text",
        "text": f"영상 길이: {duration:.1f}초. 아래는 영상에서 시간 순서대로 뽑은 장면 {len(frames)}장이다.",
    }]
    for t, path in frames:
        content.append({"type": "text", "text": f"[{t:.1f}초 장면]"})
        content.append(_image_block(path))
    if transcript:
        content.append({"type": "text", "text": f"영상 음성 자막:\n<transcript>\n{transcript}\n</transcript>"})
    else:
        content.append({"type": "text", "text": "(음성 자막 없음 — 화면만 보고 작성)"})
    return content


def check_key(api_key: str) -> str:
    """아주 짧은 요청으로 키가 쓸 수 있는지 확인한다 (비용은 1원 미만). 반환: 키가 속한 조직 ID."""
    client = _client(api_key)
    try:
        raw = client.messages.with_raw_response.create(
            model="claude-haiku-5-5", max_tokens=5, messages=[{"role": "user", "content": "hi"}])
    except anthropic.APIStatusError as e:
        try:
            _send_error(e)
        except AnalysisError as err:
            org = _org(e)
            raise AnalysisError(f"{err}" + (f" (조직 {org})" if org and org not in str(err) else ""), fatal=err.fatal)
        raise
    except anthropic.APIConnectionError:
        raise AnalysisError("인터넷 연결을 확인해 주세요.")
    return raw.headers.get("anthropic-organization-id") or ""


def _send_error(e: anthropic.APIStatusError) -> None:
    """_send와 같은 규칙으로 API 오류를 AnalysisError로 바꾼다."""
    class _Once:
        class beta:
            class messages:
                @staticmethod
                def create(**_kw):
                    raise e
    _send(_Once, {})


# ── SNS 문구 ────────────────────────────────────────────────

def analyze_video(
    api_key: str,
    model: str,
    frames: list[tuple[float, Path]],
    duration: float,
    transcript: str | None = None,
    hint: str | None = None,
) -> dict:
    client = _client(api_key)
    content = _frames_content(frames, duration, transcript)
    if hint:
        content.append({"type": "text", "text": f"사용자 메모(영상 관련 추가 정보·원하는 분위기):\n{hint}"})
    content.append({"type": "text", "text": "위 내용을 바탕으로 게시 문구를 JSON으로 작성해 줘."})
    response = _send(client, _request(
        model,
        system=SYSTEM_PROMPT,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        messages=[{"role": "user", "content": content}],
    ))
    result = _json(response)
    result["hashtags"] = _clean_tags(result.get("hashtags"))
    return result


# ── 네이버 블로그 ───────────────────────────────────────────

def sns_brief(sns: dict, hint: str | None = None, summary: str | None = None) -> str:
    """블로그 주제를 정할 때 쓰는 영상 정보 (SNS 문구·해시태그·메모·요약)."""
    lines = [
        f"유튜브 제목: {sns.get('title') or '(없음)'}",
        f"유튜브 설명: {sns.get('youtube_description') or '(없음)'}",
        f"인스타 캡션: {sns.get('instagram_caption') or '(없음)'}",
        f"해시태그: {' '.join('#' + h for h in sns.get('hashtags') or []) or '(없음)'}",
    ]
    if summary:
        lines.append(f"영상 요약: {summary}")
    if hint:
        lines.append(f"사용자 메모: {hint}")
    return "\n".join(lines)


def web_search_tool(model: str) -> dict:
    """새 웹 검색(동적 필터링)은 Opus·Sonnet 계열만 지원한다. Haiku는 기본 웹 검색을 쓴다."""
    newer = model.startswith(("claude-opus", "claude-sonnet", "claude-fable", "claude-mythos"))
    return {"type": "web_search_20260209" if newer else "web_search_20250305", "name": "web_search", "max_uses": 5}


def research_topic(api_key: str, model: str, brief: str) -> str | None:
    """웹 검색으로 주제 관련 자료를 모은다. 찾지 못하거나 거절되면 None (글은 자료 없이 쓴다).
    키·크레딧 문제(fatal)는 AnalysisError로 그대로 올린다."""
    client = _client(api_key)
    messages: list = [{"role": "user", "content": f"{brief}\n\n이 영상으로 쓸 블로그 글의 자료를 모아 주세요."}]
    tools = [web_search_tool(model)]
    response = None
    for _ in range(4):  # 서버 도구가 길어지면 pause_turn으로 끊겨 오므로 이어서 요청
        try:
            response = _send(client, _request(model, system=RESEARCH_SYSTEM, tools=tools, messages=messages,
                                              output_config={"effort": "medium"}))
        except AnalysisError as e:
            if e.fatal:  # 키·크레딧 문제는 '자료 없음'으로 넘기지 않고 알린다
                raise
            return None
        if response.stop_reason != "pause_turn":
            break
        messages = [*messages, {"role": "assistant", "content": response.content}]
    if response is None:
        return None
    notes = "".join(b.text for b in response.content if b.type == "text").strip()
    return notes or None


def blog_photo_range(duration: float) -> tuple[int, int]:
    """영상 길이에 맞는 블로그 사진 수 (짧으면 2~4장, 길면 글 흐름에 맞게 더)."""
    if duration <= 30:
        return 2, 3
    if duration <= 60:
        return 2, 4
    if duration <= 180:
        return 4, 6
    return 5, 10


def tidy_blog(result: dict, duration: float, photo_max: int, include_video: bool) -> dict:
    """AI 결과 정리: 사진 시점을 영상 안으로, 사진 수 상한, 없는 사진·중복 자리는 본문에서 뺀다."""
    photos = []
    for p in result.get("photos") or []:
        try:
            t = float(p.get("time", 0))
        except (TypeError, ValueError):
            continue
        t = min(max(t, 0.0), max(duration - 0.2, 0.0))
        photos.append({"time": round(t, 1), "caption": str(p.get("caption", "")).strip()})
    photos = photos[:photo_max]

    lines, seen_photos, seen_video = [], set(), False
    for line in (result.get("body") or "").splitlines():
        if m := PHOTO_LINE.match(line):
            n = int(m.group(1))
            if not 1 <= n <= len(photos) or n in seen_photos:
                continue
            seen_photos.add(n)
        elif VIDEO_LINE.match(line):
            if not include_video or seen_video:
                continue
            seen_video = True
        lines.append(line)
    return {
        "title": (result.get("title") or "").strip()[:100],
        "body": "\n".join(lines).strip(),
        "tags": _clean_tags(result.get("tags"))[:30],
        "photos": photos,
    }


def write_blog(
    api_key: str,
    model: str,
    frames: list[tuple[float, Path]],
    duration: float,
    brief: str,
    transcript: str | None = None,
    research: str | None = None,
    include_video: bool = True,
) -> dict:
    """반환: {"title", "body", "tags", "photos": [{"time", "caption"}, …]} — 본문의 [사진 N]은 photos[N-1]."""
    client = _client(api_key)
    low, high = blog_photo_range(duration)
    content = _frames_content(frames, duration, transcript)
    content.append({"type": "text", "text": f"## 영상 정보\n{brief}"})
    content.append({"type": "text", "text": f"## 자료 (웹 검색)\n{research}" if research
                    else "## 자료\n(웹 검색 자료 없음 — 일반 정보는 단정하지 말 것)"})
    content.append({"type": "text", "text": (
        f"사진은 {low}~{high}장 골라 줘 (위 장면들의 초 중에서). "
        + ("본문에 [영상]을 넣어 줘." if include_video else "본문에 [영상]은 넣지 마.")
        + " 이 영상의 주제로 네이버 블로그 글 초안을 JSON으로 써 줘.")})
    response = _send(client, _request(
        model,
        system=BLOG_SYSTEM,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": BLOG_SCHEMA}},
        messages=[{"role": "user", "content": content}],
    ))
    return tidy_blog(_json(response), duration, high, include_video)
