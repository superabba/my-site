"""Claude로 영상 프레임(+음성 자막)을 보고 제목·설명·캡션·해시태그와 네이버 블로그 글을 만든다."""

import base64
import json
from pathlib import Path

import anthropic

# 서버 측 대체 모델(fallbacks)을 지원하는 모델. Haiku는 지원하지 않는다.
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}

SYSTEM_PROMPT = """너는 유튜브 쇼츠·인스타그램 릴스·스레드 게시 문구와 네이버 블로그 글을 쓰는 한국어 SNS 에디터다.
영상에서 일정 간격으로 뽑은 프레임 이미지와 (있다면) 음성 자막을 보고, 영상에 실제로 나오는 내용만 근거로 문구를 쓴다.
보이지 않는 사실(장소 이름, 제품명, 인물 이름 등)은 지어내지 말고, 사용자가 메모로 알려준 정보는 적극 활용한다.

작성 기준:
- title: 유튜브 쇼츠 제목. 호기심을 끄는 한 줄, 공백 포함 40자 안팎(최대 80자). 해시태그와 이모지는 넣지 않는다.
- youtube_description: 2~4문장으로 영상 내용을 설명. 해시태그는 넣지 않는다(따로 붙인다).
- instagram_caption: 인스타그램 말투로 2~5줄. 이모지를 1~3개 정도 자연스럽게 사용. 해시태그는 넣지 않는다.
- threads_text: 스레드용 글. 친구에게 말하듯 편한 대화체로 1~3문장, 300자 이내. 해시태그는 넣지 않는다.
- hashtags: 영상과 관련된 해시태그 6~12개. '#' 기호 없이 단어만. 한국어 위주로 하되 널리 쓰이는 영어 태그를 섞어도 된다. 'shorts', 'reels' 같은 플랫폼 태그는 넣지 않는다.
- summary: 사용자가 확인할 수 있도록 영상에 무엇이 나오는지 1~2문장 요약.
- blog_title: 네이버 블로그 '슈퍼아빠'(아빠가 쓰는 가족·일상 기록 블로그) 글 제목. 검색에 걸릴 핵심 단어(장소·활동)를 앞쪽에, 25~40자. 이모지 없이.
- blog_body: 그 블로그 본문. 아빠 시점의 다정한 존댓말(~했어요)로 600~1200자.
  소제목은 '## '로 시작하는 줄 2~4개. 해시태그는 넣지 않는다.
  사진 자리는 사용자가 알려 준 [사진 1]~[사진 N]을 각각 정확히 한 번, 그 줄에 그것만 쓴다. 사진 N이 찍힌 장면을 설명하는 문단 가까이에 둔다.
  영상을 넣으라고 하면 [영상]을 한 줄로 한 번 넣는다(보통 도입부 다음).
- blog_tags: 블로그 태그 5~10개. '#' 없이, 띄어쓰기 없이."""

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
        "blog_title": {"type": "string", "description": "네이버 블로그 제목"},
        "blog_body": {"type": "string", "description": "네이버 블로그 본문 ([사진 N], [영상] 자리 표시 포함)"},
        "blog_tags": {"type": "array", "items": {"type": "string"}, "description": "'#' 없는 블로그 태그"},
    },
    "required": ["summary", "title", "youtube_description", "instagram_caption", "threads_text", "hashtags",
                 "blog_title", "blog_body", "blog_tags"],
    "additionalProperties": False,
}


class AnalysisError(Exception):
    pass


def _image_block(path: Path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def analyze_video(
    api_key: str,
    model: str,
    frames: list[tuple[float, Path]],
    duration: float,
    transcript: str | None = None,
    hint: str | None = None,
    blog_photo_times: list[float] | None = None,
    blog_video: bool = True,
) -> dict:
    if not api_key:
        raise AnalysisError("설정 탭에서 Claude API 키를 먼저 입력해 주세요.")

    content: list[dict] = [{
        "type": "text",
        "text": f"영상 길이: {duration:.1f}초. 아래는 영상에서 시간 순서대로 뽑은 프레임 {len(frames)}장이다.",
    }]
    for t, path in frames:
        content.append({"type": "text", "text": f"[{t:.1f}초 지점]"})
        content.append(_image_block(path))
    if transcript:
        content.append({"type": "text", "text": f"영상 음성 자막:\n<transcript>\n{transcript}\n</transcript>"})
    else:
        content.append({"type": "text", "text": "(음성 자막 없음 — 화면만 보고 작성)"})
    if hint:
        content.append({"type": "text", "text": f"사용자 메모(영상 관련 추가 정보·원하는 분위기):\n{hint}"})
    times = blog_photo_times or []
    if times:
        spots = ", ".join(f"[사진 {i}] = {t:.1f}초 지점 장면" for i, t in enumerate(times, start=1))
        blog_note = f"블로그 사진 자리: {spots}."
    else:
        blog_note = "블로그 본문에는 사진 자리를 넣지 않는다."
    blog_note += " 블로그 본문에 [영상]을 넣어 줘." if blog_video else " 블로그 본문에 [영상]은 넣지 않는다."
    content.append({"type": "text", "text": blog_note})
    content.append({"type": "text", "text": "위 내용을 바탕으로 게시 문구를 JSON으로 작성해 줘."})

    request = dict(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        messages=[{"role": "user", "content": content}],
    )
    if model in _FALLBACK_MODELS:
        # 안전 분류기가 요청을 거절하면 서버가 다른 모델로 자동 재시도한다.
        request["betas"] = ["server-side-fallback-2026-07-01"]
        request["fallbacks"] = "default"

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.beta.messages.create(**request)
    except anthropic.AuthenticationError:
        raise AnalysisError("Claude API 키가 올바르지 않습니다. 설정 탭에서 다시 확인해 주세요.")
    except anthropic.PermissionDeniedError:
        raise AnalysisError("이 API 키로는 선택한 모델을 사용할 수 없습니다.")
    except anthropic.RateLimitError:
        raise AnalysisError("요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.")
    except anthropic.BadRequestError as e:
        raise AnalysisError(f"요청 오류: {e.message}")
    except anthropic.APIStatusError as e:
        raise AnalysisError(f"Claude 서버 오류({e.status_code}). 잠시 후 다시 시도해 주세요.")
    except anthropic.APIConnectionError:
        raise AnalysisError("인터넷 연결을 확인해 주세요.")

    if response.stop_reason == "refusal":
        raise AnalysisError("Claude가 이 영상의 문구 작성을 거절했습니다. 내용을 직접 입력해 주세요.")
    if response.stop_reason == "max_tokens":
        raise AnalysisError("응답이 너무 길어 잘렸습니다. 다시 시도해 주세요.")

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        raise AnalysisError("Claude 응답을 해석하지 못했습니다. 다시 시도해 주세요.")
    result["hashtags"] = [h.strip().lstrip("#").replace(" ", "") for h in result.get("hashtags", []) if h.strip()]
    result["blog_tags"] = [h.strip().lstrip("#").replace(" ", "") for h in result.get("blog_tags", []) if h.strip()]
    return result
