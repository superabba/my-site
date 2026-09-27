"""GitHub Actions 실행 요약 작성.

python -m cardbot.gh_summary <실행 전 폴더 목록 파일> [추가 폴더 이름]

- 이번 실행에서 새로 생긴(또는 지정한) 초안 폴더를 찾아
- $GITHUB_STEP_SUMMARY에 슬라이드 미리보기·캡션·게시 결과를 쓰고
- $REVIEW_DIR로 복사해 아티팩트로 내려받을 수 있게 한다.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


LABELS = {"instagram": "인스타그램 카드뉴스", "reels": "인스타그램 릴스", "threads": "스레드"}


def main() -> None:
    out_dir = Path(os.environ["CARDBOT_OUTPUT_DIR"])
    before = set(Path(sys.argv[1]).read_text().split()) if Path(sys.argv[1]).exists() else set()
    extra = {Path(a).name for a in sys.argv[2:] if a}
    folders = sorted(
        d for d in out_dir.iterdir() if d.is_dir() and (d.name not in before or d.name in extra)
    ) if out_dir.exists() else []

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("STATE_BRANCH", "cardbot-data")
    review = Path(os.environ.get("REVIEW_DIR", "review"))
    md = ["# 카드뉴스 실행 결과\n"]
    if not folders:
        md.append("새로 만들어진 초안이 없습니다. 로그를 확인하세요.")

    for d in folders:
        topic = _json(d / "topic.json")
        meta = _json(d / "published.json")
        md.append(f"## {topic.get('title', d.name)}")
        md.append(f"폴더: `{d.name}` · [브랜치에서 보기](https://github.com/{repo}/tree/{branch}/output/{d.name})\n")
        slides = sorted(d.glob("slide_*.jpg"))
        md.append(" ".join(
            f'<img src="https://raw.githubusercontent.com/{repo}/{branch}/output/{d.name}/{s.name}" width="180">'
            for s in slides
        ))
        for name, info in meta.get("posts", {}).items():
            md.append(f"- ✅ **{LABELS.get(name, name)}** 게시: {info.get('permalink') or info.get('media_id')}")
        for name, err in meta.get("errors", {}).items():
            md.append(f"- ❌ **{LABELS.get(name, name)}** 실패: `{err[:300]}`")
        if not meta.get("posts") and not meta.get("errors"):
            md.append(
                f"- 📝 초안만 생성됨 → Actions에서 **publish-draft** 실행, folder에 `{d.name}` 입력"
            )
        tree = f"https://github.com/{repo}/blob/{branch}/output/{d.name}"
        if (d / "reel.mp4").exists():
            md.append(f"\n**🎬 릴스 영상**: [reel.mp4 보기]({tree}/reel.mp4)")
        elif (d / "reel_error.txt").exists():
            err = (d / "reel_error.txt").read_text(encoding="utf-8")[:300]
            md.append(f"- ⚠️ 릴스 영상 생성 실패: `{err}`")
        blog = _json(d / "blog.json")
        if blog:
            md.append(f"\n**📝 블로그 글 초안** — {blog.get('titles', [''])[0]}")
            md.append(
                f"[네이버용 텍스트]({tree}/blog_naver.txt) · [티스토리용 HTML]({tree}/blog_tistory.html)"
                f" · [게시 가이드(제목·태그)]({tree}/blog_guide.md) · 핵심 키워드: `{blog.get('main_keyword', '')}`"
            )
        elif (d / "blog_error.txt").exists():
            err = (d / "blog_error.txt").read_text(encoding="utf-8")[:300]
            md.append(f"- ⚠️ 블로그 글 생성 실패: `{err}` → Run workflow에서 action `blog`로 다시 만들 수 있어요")
        cap = d / "caption.txt"
        if cap.exists():
            md.append("\n<details><summary>인스타 캡션</summary>\n\n```\n" + cap.read_text(encoding="utf-8") + "\n```\n</details>")
        th = d / "threads.txt"
        if th.exists():
            md.append("<details><summary>스레드 본문</summary>\n\n```\n" + th.read_text(encoding="utf-8") + "\n```\n</details>\n")
        shutil.copytree(d, review / d.name, dirs_exist_ok=True)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    text = "\n".join(md) + "\n"
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)


def _json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


if __name__ == "__main__":
    main()
