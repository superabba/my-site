"""블로그 글 초안을 네이버 블로그 / 티스토리에 붙여넣기 좋은 파일로 저장.

두 플랫폼 모두 공식 글쓰기 API가 없으므로(티스토리 오픈 API는 2024년 종료) 자동 게시는 하지 않고,
사람이 경험을 덧붙여 직접 올리도록 복사용 파일을 만든다.

- blog_naver.txt    네이버 스마트에디터에 그대로 붙여넣는 텍스트
- blog_tistory.html 티스토리 에디터의 HTML 모드에 붙여넣는 본문 (게시 후에는 사진 포함)
- blog_naver.html   브라우저로 열어 전체 복사 → 네이버 에디터에 붙여넣는 사진 포함 본문
- blog_guide.md     제목 후보·키워드·태그·게시 체크리스트
- blog.json         구조화된 원본
"""

from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path

from .llm import BlogPost

HINT_MARK = "✍️ [직접 덧붙이기 · 게시 전 이 줄 삭제]"
IMAGE_MARK = "📷 [이미지 넣기: {name} · 게시 전 이 줄 삭제]"
BLOG_FILES = ("blog.json", "blog_naver.txt", "blog_naver.html", "blog_tistory.html", "blog_guide.md")


BODY_DIVIDER = "━━━━━━━━━━ 아래부터 본문 ━━━━━━━━━━"


def caption_hashtags(caption: str) -> list[str]:
    """인스타 캡션(caption.txt)에서 해시태그를 순서대로, 중복 없이 뽑는다."""
    tags: list[str] = []
    for t in re.findall(r"#[^\s#]+", caption):
        if t not in tags:
            tags.append(t)
    return tags


def naver_text(post: BlogPost, hashtags: list[str] | None = None) -> str:
    # 네이버 에디터는 제목칸이 따로 있으므로 제목을 맨 위에 두고 본문과 구분한다
    out = [
        f"[제목] {post.titles[0].strip()}",
        "(위 제목은 제목칸에 붙여넣으세요 · 다른 제목 후보는 blog_guide.md)",
        "",
        BODY_DIVIDER,
        "",
        post.intro.strip(),
        "",
    ]
    for sec in post.sections:
        out += [f"■ {sec.heading.strip()}", "", sec.body.strip(), ""]
        if sec.image:
            out += [IMAGE_MARK.format(name=sec.image), ""]
        if sec.experience_hint:
            out += [f"{HINT_MARK} {sec.experience_hint.strip()}", ""]
    if post.faq:
        out += ["■ 자주 묻는 질문", ""]
        for f in post.faq:
            out += [f"Q. {f.question.strip()}", f"A. {f.answer.strip()}", ""]
    out += ["■ 마무리", "", post.conclusion.strip(), ""]
    if post.sources:
        out += ["참고 자료"] + [f"- {s}" for s in post.sources] + [""]
    if hashtags:  # 네이버는 본문의 #해시태그를 태그로 인식한다
        out += [" ".join(hashtags), ""]
    return "\n".join(out).strip() + "\n"


def _paragraphs(text: str) -> str:
    paras = [p.strip() for p in text.replace("\\n", "\n").split("\n\n") if p.strip()]
    return "\n".join(f"<p>{escape(p).replace(chr(10), '<br>')}</p>" for p in paras)


def image_urls(folder: Path) -> dict[str, str]:
    """게시할 때 올린 슬라이드의 공개 URL (published.json), 파일 이름 → URL."""
    try:
        meta = json.loads((folder / "published.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {u.rsplit("/", 1)[-1]: u for u in meta.get("image_urls") or []}


def tistory_html(post: BlogPost, hashtags: list[str] | None = None,
                 images: dict[str, str] | None = None) -> str:
    images = images or {}
    # 제목은 브라우저로 열어도 보이게 맨 위에 표시한다 (이미지 표시처럼 게시 전 삭제)
    title = escape(post.titles[0].strip())
    out = [
        f"<p><b>[제목] {title}</b></p>",
        "<p><b>📌 [위 제목은 제목칸에 입력하고, 이 두 줄과 아래 구분선은 게시 전 삭제]</b></p>",
        "<hr>",
        _paragraphs(post.intro),
    ]
    for sec in post.sections:
        out.append(f"<h2>{escape(sec.heading.strip())}</h2>")
        out.append(_paragraphs(sec.body))
        if sec.image and sec.image in images:  # 공개 URL이 있으면 사진을 바로 넣는다
            out.append(f'<p><img src="{escape(images[sec.image])}" alt="{escape(sec.heading.strip())}"'
                       ' style="max-width:100%"></p>')
        elif sec.image:
            out.append(f"<p><b>{escape(IMAGE_MARK.format(name=sec.image))}</b></p>")
        if sec.experience_hint:
            out.append(f"<blockquote>{escape(HINT_MARK)} {escape(sec.experience_hint.strip())}</blockquote>")
    if post.faq:
        out.append("<h2>자주 묻는 질문</h2>")
        for f in post.faq:
            out.append(f"<h3>Q. {escape(f.question.strip())}</h3>")
            out.append(_paragraphs(f.answer))
    out.append("<h2>마무리</h2>")
    out.append(_paragraphs(post.conclusion))
    if post.sources:
        items = "".join(
            f'<li><a href="{escape(s)}">{escape(s)}</a></li>' if s.startswith("http") else f"<li>{escape(s)}</li>"
            for s in post.sources
        )
        out.append(f"<h3>참고 자료</h3><ul>{items}</ul>")
    if hashtags:
        out.append(f"<p>{escape(' '.join(hashtags))}</p>")
    return "\n".join(out) + "\n"


def naver_html(body: str, title: str) -> str:
    """브라우저로 열어 Ctrl+A → Ctrl+C 한 뒤 네이버 에디터 본문에 붙여넣는 페이지."""
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>body{{max-width:720px;margin:24px auto;padding:0 16px;line-height:1.8;font-family:sans-serif}}img{{max-width:100%}}</style>
</head><body>
{body}</body></html>
"""


def guide(post: BlogPost, folder_name: str) -> str:
    titles = "\n".join(f"{i}. {t}" + (" (추천)" if i == 1 else "") for i, t in enumerate(post.titles, 1))
    images = sorted({s.image for s in post.sections if s.image})
    return f"""# 블로그 게시 가이드 — {folder_name}

## 제목 후보
{titles}

## 키워드
- 핵심: **{post.main_keyword}**
- 보조: {", ".join(post.sub_keywords)}

## 요약문 (티스토리 '요약' / 검색 설명)
{post.meta_description}

## 태그
- 네이버 (최대 30개): {" ".join("#" + t for t in post.tags)}
- 티스토리 (10개 안팎 권장): {", ".join(post.tags[:10])}

## 게시 체크리스트
1. 네이버: `blog_naver.txt` 맨 위 **[제목]** 줄을 제목칸에, '아래부터 본문' 구분선 아래를 본문에 붙여넣기 → '■' 줄을 소제목으로 바꾸기
   네이버(사진 포함): `blog_naver.html`을 브라우저로 열어 **Ctrl+A → Ctrl+C** → 네이버 본문에 붙여넣기 (맨 위 [제목] 두 줄과 구분선은 지우기)
   티스토리: 맨 위 **[제목]**을 제목칸에 입력하고, 에디터 오른쪽 위 **기본모드 → HTML**로 바꾼 뒤 `blog_tistory.html` 붙여넣기 (게시 후 만든 초안은 사진 포함)
2. 사진이 안 들어간 📷 표시 자리에는 이미지 업로드 ({", ".join(images) or "없음"}) — 같은 폴더의 slide_*.jpg
3. ✍️ 표시 자리에 **직접 겪은 경험·사진·의견** 한두 문장 추가 (검색 노출과 신뢰도에 가장 중요)
4. 📷·✍️ 안내 줄은 모두 삭제
5. 숫자·날짜 등 사실관계를 `research.md`의 출처와 한 번 더 대조
6. 바로 발행하지 않으려면 **임시저장**
"""


def save_blog(folder: Path, post: BlogPost) -> None:
    caption = folder / "caption.txt"
    # 글 맨 아래에 인스타 캡션과 같은 해시태그를 붙인다 (사람이 고친 캡션도 반영)
    hashtags = caption_hashtags(caption.read_text(encoding="utf-8")) if caption.exists() else []
    (folder / "blog.json").write_text(post.model_dump_json(indent=2), encoding="utf-8")
    (folder / "blog_naver.txt").write_text(naver_text(post, hashtags), encoding="utf-8")
    body = tistory_html(post, hashtags, image_urls(folder))
    (folder / "blog_tistory.html").write_text(body, encoding="utf-8")
    (folder / "blog_naver.html").write_text(naver_html(body, post.titles[0].strip()), encoding="utf-8")
    (folder / "blog_guide.md").write_text(guide(post, folder.name), encoding="utf-8")


def has_blog(folder: Path) -> bool:
    return (folder / "blog.json").exists()


def refresh_blog(folder: Path) -> None:
    """게시 후 사진 URL이 생기면 저장된 글(blog.json)로 파일만 다시 만든다 (AI 호출 없음)."""
    save_blog(folder, BlogPost.model_validate_json((folder / "blog.json").read_text(encoding="utf-8")))
