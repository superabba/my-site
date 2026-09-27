"""블로그 글 초안을 네이버 블로그 / 티스토리에 붙여넣기 좋은 파일로 저장.

두 플랫폼 모두 공식 글쓰기 API가 없으므로(티스토리 오픈 API는 2024년 종료) 자동 게시는 하지 않고,
사람이 경험을 덧붙여 직접 올리도록 복사용 파일을 만든다.

- blog_naver.txt    네이버 스마트에디터에 그대로 붙여넣는 텍스트
- blog_tistory.html 티스토리 에디터의 HTML 모드에 붙여넣는 본문
- blog_guide.md     제목 후보·키워드·태그·게시 체크리스트
- blog.json         구조화된 원본
"""

from __future__ import annotations

from html import escape
from pathlib import Path

from .llm import BlogPost

HINT_MARK = "✍️ [직접 덧붙이기 · 게시 전 이 줄 삭제]"
IMAGE_MARK = "📷 [이미지 넣기: {name} · 게시 전 이 줄 삭제]"
BLOG_FILES = ("blog.json", "blog_naver.txt", "blog_tistory.html", "blog_guide.md")


BODY_DIVIDER = "━━━━━━━━━━ 아래부터 본문 ━━━━━━━━━━"


def naver_text(post: BlogPost) -> str:
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
    return "\n".join(out).strip() + "\n"


def _paragraphs(text: str) -> str:
    paras = [p.strip() for p in text.replace("\\n", "\n").split("\n\n") if p.strip()]
    return "\n".join(f"<p>{escape(p).replace(chr(10), '<br>')}</p>" for p in paras)


def tistory_html(post: BlogPost) -> str:
    # 제목은 HTML 주석으로 넣어 두어 실수로 함께 붙여넣어도 글에는 보이지 않게 한다
    title = escape(post.titles[0].strip()).replace("--", "—")
    out = [
        f"<!-- [제목] {title} -->",
        "<!-- 위 제목은 제목칸에 입력하세요 · 아래부터 본문 -->",
        _paragraphs(post.intro),
    ]
    for sec in post.sections:
        out.append(f"<h2>{escape(sec.heading.strip())}</h2>")
        out.append(_paragraphs(sec.body))
        if sec.image:
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
    return "\n".join(out) + "\n"


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
   티스토리: 맨 위 주석의 **[제목]**을 제목칸에 입력하고, 에디터 오른쪽 위 **기본모드 → HTML**로 바꾼 뒤 `blog_tistory.html` 붙여넣기
2. 📷 표시 자리에 이미지 업로드 ({", ".join(images) or "없음"}) — 같은 폴더의 slide_*.jpg
3. ✍️ 표시 자리에 **직접 겪은 경험·사진·의견** 한두 문장 추가 (검색 노출과 신뢰도에 가장 중요)
4. 📷·✍️ 안내 줄은 모두 삭제
5. 숫자·날짜 등 사실관계를 `research.md`의 출처와 한 번 더 대조
6. 바로 발행하지 않으려면 **임시저장**
"""


def save_blog(folder: Path, post: BlogPost) -> None:
    (folder / "blog.json").write_text(post.model_dump_json(indent=2), encoding="utf-8")
    (folder / "blog_naver.txt").write_text(naver_text(post), encoding="utf-8")
    (folder / "blog_tistory.html").write_text(tistory_html(post), encoding="utf-8")
    (folder / "blog_guide.md").write_text(guide(post, folder.name), encoding="utf-8")


def has_blog(folder: Path) -> bool:
    return (folder / "blog.json").exists()
