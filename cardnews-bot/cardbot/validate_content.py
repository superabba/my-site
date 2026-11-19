"""루틴이 올리기 전에 원고 폴더의 형식을 확인한다. pydantic·requests 만 있으면 돌아간다 (LLM·numpy 불필요).

사용: python -m cardbot.validate_content content/<날짜-주제>
틀린 곳을 한국어로 모두 출력하고 종료 코드 1을 돌려준다.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .llm import BlogPost, CardNews, TopicIdea

STYLES = ("caption-news", "chalkboard", "swiss-motion", "news-package", "whiteboard", "blueprint")  # 비트 스타일은 컷 구성이 달라 루틴 방식에서는 쓰지 않는다
IMG_RE = re.compile(r"^slide_(\d{2})\.jpg$")


def _limits() -> tuple[int, int]:
    return int(os.environ.get("SLIDES_MIN") or 5), min(10, int(os.environ.get("SLIDES_MAX") or 8))


def _blog_chars(b: BlogPost) -> int:
    n = len(b.intro) + len(b.conclusion)
    n += sum(len(s.body) for s in b.sections)
    n += sum(len(f.question) + len(f.answer) for f in b.faq)
    return n


def check(folder: Path) -> list[str]:
    errs: list[str] = []
    for name in ("topic.json", "card.json", "reel.json", "blog.json"):
        if not (folder / name).exists():
            errs.append(f"{name} 파일이 없습니다")
    if errs:
        return errs
    try:
        topic = TopicIdea.model_validate_json((folder / "topic.json").read_text(encoding="utf-8"))
        card = CardNews.model_validate_json((folder / "card.json").read_text(encoding="utf-8"))
    except Exception as e:  # pydantic 오류 메시지를 그대로 보여 준다
        return [f"topic.json/card.json 형식 오류: {e}"]
    lo, hi = _limits()
    n = len(card.slides)
    if not lo <= n <= hi:
        errs.append(f"슬라이드는 {lo}~{hi}장이어야 합니다 (지금 {n}장)")
    if card.slides[0].kind != "cover":
        errs.append("첫 슬라이드는 kind가 cover여야 합니다")
    if card.slides[-1].kind != "cta":
        errs.append("마지막 슬라이드는 kind가 cta여야 합니다")
    for i, s in enumerate(card.slides, 1):
        if not s.heading.strip():
            errs.append(f"슬라이드 {i}: heading이 비어 있습니다")
        if len(s.heading) > 30:
            errs.append(f"슬라이드 {i}: heading이 너무 깁니다 ({len(s.heading)}자, 30자 이하, 표지는 16자 안팎)")
        if len(s.body) > 110:
            errs.append(f"슬라이드 {i}: body가 너무 깁니다 ({len(s.body)}자, 110자 이하, 90자 안팎)")
        if len(s.highlight) > 8:
            errs.append(f"슬라이드 {i}: highlight가 너무 깁니다 ({len(s.highlight)}자, 8자 이하). 큰 글씨라 줄바꿈되면 한 글자만 남습니다")
    if len(card.threads_text) > 500:
        errs.append(f"threads_text가 500자를 넘습니다 ({len(card.threads_text)}자)")
    if not 5 <= len([t for t in card.hashtags if t.strip()]) <= 30:
        errs.append("hashtags는 5~30개여야 합니다 (8~15개 권장, '#' 없이)")
    if len([s for s in card.sources if s.strip()]) < 2:
        errs.append("card.json sources(출처)가 2개 이상 필요합니다")

    try:
        reel = json.loads((folder / "reel.json").read_text(encoding="utf-8"))
    except Exception as e:
        return errs + [f"reel.json이 JSON이 아닙니다: {e}"]
    style = reel.get("style")
    if style not in STYLES:
        errs.append(f"reel.json style은 {' | '.join(STYLES)} 중 하나여야 합니다 (지금 {style!r}). 직전 영상과 같으면 자동으로 다른 스타일로 바뀝니다")
    lines = reel.get("lines")
    if not isinstance(lines, list) or not lines:
        errs.append("reel.json lines가 비어 있습니다")
    else:
        slides = []
        for k, ln in enumerate(lines, 1):
            try:
                slides.append(int(ln["slide"]))
                text = str(ln["text"]).strip()
            except Exception:
                errs.append(f"reel.json lines[{k}]에는 slide(숫자)와 text가 있어야 합니다")
                continue
            if not 8 <= len(text.replace("{", "").replace("}", "")) <= 70:
                errs.append(f"내레이션 {k}번째 줄은 8~70자여야 합니다 (지금 {len(text)}자)")
        if sorted(slides) != list(range(1, n + 1)):
            errs.append(f"내레이션 줄은 슬라이드 {n}장에 하나씩(slide 1~{n}) 있어야 합니다")

    try:
        blog = BlogPost.model_validate_json((folder / "blog.json").read_text(encoding="utf-8"))
    except Exception as e:
        return errs + [f"blog.json 형식 오류: {e}"]
    titles = [t for t in blog.titles if t.strip()]
    if not 1 <= len(titles) <= 5:
        errs.append("blog.json titles는 1~5개여야 합니다 (첫 번째가 추천 제목)")
    if any(len(t) > 50 for t in titles):
        errs.append("blog.json 제목 후보는 50자 이하여야 합니다 (30자 안팎 권장)")
    if not blog.main_keyword.strip():
        errs.append("blog.json main_keyword가 비어 있습니다 (검색창에 입력할 2~4단어 구문)")
    if not 3 <= len(blog.sections) <= 7:
        errs.append(f"blog.json sections는 3~7개여야 합니다 (지금 {len(blog.sections)}개, 4~6개 권장)")
    for sec in blog.sections:
        if sec.image and (not IMG_RE.match(sec.image) or int(IMG_RE.match(sec.image).group(1)) > n):
            errs.append(f"blog.json section.image 는 slide_01.jpg ~ slide_{n:02d}.jpg 중 하나이거나 빈 문자열이어야 합니다: {sec.image!r}")
    if not 3 <= len(blog.faq) <= 6:
        errs.append(f"blog.json faq는 3~6개여야 합니다 (지금 {len(blog.faq)}개)")
    if not 8 <= len([t for t in blog.tags if t.strip()]) <= 30:
        errs.append("blog.json tags는 8~30개여야 합니다 ('#' 없이)")
    chars = _blog_chars(blog)
    if not 1500 <= chars <= 4800:
        errs.append(f"블로그 본문(도입~결론·FAQ)은 1,500~4,800자여야 합니다 (지금 {chars}자, 2,500~4,000자 권장)")
    if len([u for u in blog.sources if u.strip()]) < 2:
        errs.append("blog.json sources(참고 자료)가 2개 이상 필요합니다")
    if not topic.keyword.strip():
        errs.append("topic.json keyword가 비어 있습니다")
    return errs


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    errs = check(Path(argv[0]))
    for e in errs:
        print("✗", e)
    print("통과" if not errs else f"{len(errs)}개 문제가 있습니다")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
