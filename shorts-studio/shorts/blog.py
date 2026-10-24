"""매일 자동 쇼츠와 같은 내용으로 경제주막 네이버 블로그 글(blog.json)과 사진을 만든다.

카드뉴스 봇의 blog.json과 같은 형식이라, 네이버 발행은 카드뉴스와 같은 방식(Claude 루틴 + Uplika)으로 한다.
  - blog.json: titles, main_keyword, sub_keywords, meta_description, intro,
               sections[{heading, body, image, experience_hint}], faq[{question, answer}],
               conclusion, sources, tags, video_id, cover, images
  - cover.jpg: 영상 첫 장면(제목·자막이 보이는 화면)
  - scene_01.jpg …: 장면마다 나노바나나 그림 (없으면 그 장면의 영상 화면)
사진은 블로그에서 보기 좋게 4:5로 가운데를 잘라 쓴다.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

from PIL import Image

from .ffmpeg import FFMPEG
from .writer import _call_json

log = logging.getLogger(__name__)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
BLOG_WIDTH = 1080

BLOG_SCHEMA = {
    "type": "object",
    "properties": {
        "titles": {"type": "array", "items": {"type": "string"}},
        "main_keyword": {"type": "string"},
        "sub_keywords": {"type": "array", "items": {"type": "string"}},
        "meta_description": {"type": "string"},
        "intro": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "body": {"type": "string"},
                    "image": {"type": "string"},
                    "experience_hint": {"type": "string"},
                },
                "required": ["heading", "body", "image", "experience_hint"],
                "additionalProperties": False,
            },
        },
        "faq": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"question": {"type": "string"}, "answer": {"type": "string"}},
                "required": ["question", "answer"],
                "additionalProperties": False,
            },
        },
        "conclusion": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["titles", "main_keyword", "sub_keywords", "meta_description", "intro", "sections", "faq",
                 "conclusion", "sources", "tags"],
    "additionalProperties": False,
}

BLOG_SYSTEM = """당신은 경제 정보 블로그 '경제주막'(네이버 블로그)의 에디터입니다.
같은 주제로 이미 만든 30초 쇼츠 영상 대본과 자료 조사(팩트 시트)를 바탕으로, 검색하는 사람의 궁금증을 끝까지 풀어 주는 블로그 글을 씁니다.

검색 노출을 위한 원칙:
- 핵심 키워드는 사람들이 검색창에 실제로 입력할 2~4단어 구문으로 정하고, 제목 앞쪽과 도입부 첫 문단, 소제목 1~2곳에 자연스럽게 넣으세요. 억지로 반복하지 마세요
- 제목 후보 3~5개: 30자 안팎, 구체적 숫자나 대상이 드러나게. 낚시성·과장 표현 금지
- 본문(도입~결론, FAQ 포함)은 공백 포함 2,000~3,500자, 소제목 4~5개. 한 문단 2~4문장으로 짧게, 모바일에서 읽기 쉽게
- 영상 대본을 그대로 옮기지 말고, 왜 그런지·어떻게 하면 되는지·주의할 점을 더 깊게 풀어 쓰세요
- 숫자·날짜·제도는 팩트 시트에 있는 것만 쓰고, 기준 시점을 밝히세요. 불확실한 내용은 단정하지 마세요
- 각 소제목의 image에는 제공된 사진 파일명 중 내용이 가장 어울리는 것을 하나씩 지정하세요 (제공된 파일명만, 같은 사진 반복 금지, 없으면 빈 문자열)
- experience_hint: 글쓴이가 직접 경험·의견을 덧붙이면 좋을 소제목에 무엇을 쓰면 되는지 한 문장 (본문에 가짜 경험을 지어내지 마세요)
- faq: 검색에서 자주 나올 질문 3~4개와 짧은 답
- 결론: 핵심 요약 + 행동 제안. 마지막에 '정보 제공용이며 특정 상품의 투자 권유가 아니다'는 취지와, 개인 상황은 금융회사·전문가와 상담하라는 문장을 넣으세요
- sources: 팩트 시트에 나온 출처 이름과 날짜만 짧게 (예: '금융감독원 보도자료 (2026.10.21)'). 주소(URL)를 지어내지 마세요
- tags: 검색 수요가 있는 한국어 태그 10~15개 ('#' 없이, 띄어쓰기 없이)
- 존댓말(~해요/~합니다)로, 이모지·해시태그는 본문에 쓰지 마세요"""


# ---------------------------------------------------------------- 사진

def crop_for_blog(src: Path, dest: Path, center_y: float = 0.5) -> Path:
    """세로 사진을 4:5로 가운데를 잘라 블로그 폭(1080)에 맞춘 JPEG로 저장한다."""
    with Image.open(src) as im:
        im = im.convert("RGB")
        w, h = im.size
        target_h = min(h, int(w * 5 / 4))
        top = int(min(max(center_y * h - target_h / 2, 0), h - target_h))
        im = im.crop((0, top, w, top + target_h))
        if im.width != BLOG_WIDTH:
            im = im.resize((BLOG_WIDTH, int(im.height * BLOG_WIDTH / im.width)), Image.LANCZOS)
        dest.parent.mkdir(parents=True, exist_ok=True)
        im.save(dest, "JPEG", quality=86, optimize=True)
    return dest


def grab_frame(video: Path, t: float, dest: Path) -> Path:
    proc = subprocess.run(
        [FFMPEG, "-nostdin", "-y", "-loglevel", "error", "-ss", f"{max(t, 0):.2f}", "-i", str(video),
         "-frames:v", "1", str(dest)],
        stdin=subprocess.DEVNULL, capture_output=True, creationflags=_NO_WINDOW)
    if proc.returncode != 0 or not dest.exists():
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-300:] or "프레임 추출 실패")
    return dest


def save_blog_images(video: Path, scenes: list[dict], blog_dir: Path) -> list[str]:
    """cover.jpg + scene_XX.jpg를 만들고, 장면 사진 파일 이름 목록을 돌려준다.

    scenes: [{"media": 나노바나나 그림 경로 또는 None, "start": 초, "dur": 초}, ...]
    """
    blog_dir.mkdir(parents=True, exist_ok=True)
    tmp = blog_dir / "_frame.png"
    try:
        crop_for_blog(grab_frame(video, 0.5, tmp), blog_dir / "cover.jpg", center_y=0.43)
    except (RuntimeError, OSError) as e:
        log.warning("표지 사진을 만들지 못했습니다: %s", e)
    names = []
    for i, sc in enumerate(scenes, 1):
        name = f"scene_{i:02d}.jpg"
        try:
            media = sc.get("media")
            if media and Path(media).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                crop_for_blog(Path(media), blog_dir / name)
            else:  # 그림이 없는 장면은 그 장면 가운데 화면을 쓴다
                crop_for_blog(grab_frame(video, sc["start"] + sc["dur"] / 2, tmp), blog_dir / name, center_y=0.43)
            names.append(name)
        except (RuntimeError, OSError, KeyError) as e:
            log.warning("장면 %d 사진을 만들지 못했습니다: %s", i, e)
    tmp.unlink(missing_ok=True)
    return names


# ---------------------------------------------------------------- 글

def normalize_blog(data: dict, images: list[str]) -> dict:
    allowed, used = set(images), set()
    sections = []
    for s in data.get("sections", []):
        img = s.get("image", "").strip()
        if img not in allowed or img in used:
            img = next((n for n in images if n not in used), "")  # 빈 자리는 아직 안 쓴 사진으로
        if img:
            used.add(img)
        sections.append({"heading": s.get("heading", "").strip(), "body": s.get("body", "").strip(),
                         "image": img, "experience_hint": s.get("experience_hint", "").strip()})
    tags = []
    for t in data.get("tags", []):
        t = t.replace("#", "").replace(" ", "").strip()
        if t and t not in tags:
            tags.append(t)
    return {
        "titles": [t.strip() for t in data.get("titles", []) if t.strip()],
        "main_keyword": data.get("main_keyword", "").strip(),
        "sub_keywords": [k.strip() for k in data.get("sub_keywords", []) if k.strip()],
        "meta_description": data.get("meta_description", "").strip(),
        "intro": data.get("intro", "").strip(),
        "sections": [s for s in sections if s["heading"] and s["body"]],
        "faq": [{"question": f.get("question", "").strip(), "answer": f.get("answer", "").strip()}
                for f in data.get("faq", []) if f.get("question", "").strip()],
        "conclusion": data.get("conclusion", "").strip(),
        "sources": [s.strip() for s in data.get("sources", []) if s.strip()],
        "tags": tags[:30],
    }


def write_blog(api_key: str, model: str, script: dict, facts: str, topic: dict, theme: str,
               images: list[str]) -> dict:
    scenes = "\n".join(
        f"- {img}: {sc.get('narration', '')} (화면 문구: {sc.get('headline', '')})"
        for img, sc in zip(images, script.get("scenes", []))
    ) or "(사진 없음)"
    user = (
        f"오늘의 테마: {theme}\n"
        f"## 주제\n{json.dumps(topic, ensure_ascii=False)}\n\n"
        f"## 쇼츠 영상\n제목: {script.get('title', '')}\n설명: {script.get('caption', '')}\n\n"
        f"## 장면별 대본과 사진 파일명\n{scenes}\n\n"
        f"## 팩트 시트\n{facts or '(없음 — 대본에 있는 사실만 쓰세요)'}\n\n"
        "위 내용으로 네이버 블로그 글을 작성하세요."
    )
    data = _call_json(api_key, model, BLOG_SYSTEM, user, BLOG_SCHEMA, effort="medium")
    blog = normalize_blog(data, images)
    if not blog["titles"] or not blog["sections"]:
        raise ValueError("블로그 글이 비어 있습니다")
    return blog
