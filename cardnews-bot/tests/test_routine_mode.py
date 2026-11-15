"""CONTENT_SOURCE=routine: Claude 루틴이 올린 원고 → 초안 → 게시 (LLM 없음)."""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from cardbot import content, validate_content
from cardbot.config import Settings
from cardbot.llm import TopicIdea, CardNews
from cardbot.pipeline import Pipeline
from cardbot.publishers import PublishResult
from cardbot.store import Store

FONTS = Path(__file__).parent.parent / "fonts"


def make_content(root: Path, name: str, style: str = "caption-news", keyword: str = "고향사랑기부 세액공제", n: int = 6) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "topic.json").write_text(json.dumps({
        "title": "고향사랑기부 연말 전에 꼭 확인할 3가지", "keyword": keyword, "angle": "체크리스트", "hook": "연말 지나면 못 받아요",
        "why_now": "연말 세액공제 마감", "score": 70, "risk": ""}, ensure_ascii=False), encoding="utf-8")
    slides = [{"kind": "cover", "heading": "고향사랑기부, 연말 전 3가지", "body": "놓치면 세액공제가 날아가요", "highlight": "최대 16.5%"}]
    for i in range(n - 2):
        slides.append({"kind": "content", "heading": f"포인트 {i + 1}: 한도 확인", "body": "연간 기부 한도와 공제율을 먼저 확인하세요.", "highlight": ""})
    slides.append({"kind": "cta", "heading": "저장해 두고 12월에 꺼내 보세요", "body": "친구에게도 공유해 주세요", "highlight": ""})
    (d / "card.json").write_text(json.dumps({
        "slides": slides, "caption": "고향사랑기부, 연말 전에 확인하세요", "hashtags": [f"태그{i}" for i in range(8)],
        "threads_text": "고향사랑기부 연말 전에 확인할 3가지를 정리했어요.", "sources": ["행정안전부 고향사랑e음", "국세청 보도자료"]},
        ensure_ascii=False), encoding="utf-8")
    (d / "reel.json").write_text(json.dumps({
        "style": style, "reason": "숫자·한도 비교", "lines": [{"slide": i + 1, "text": f"{i + 1}번째 내레이션이에요. {i + 1}만 원을 확인해 보세요."} for i in range(n)]},
        ensure_ascii=False), encoding="utf-8")
    body = "고향사랑기부는 거주지가 아닌 지역에 기부하고 세액공제와 답례품을 받는 제도예요. 연간 한도와 공제율, 신청 방법을 차례로 확인해 보세요. " * 3
    (d / "blog.json").write_text(json.dumps({
        "titles": ["고향사랑기부 세액공제, 연말 전에 확인할 3가지"], "main_keyword": "고향사랑기부 세액공제", "sub_keywords": ["답례품", "한도"],
        "meta_description": "고향사랑기부 세액공제 한도와 신청 방법", "intro": body,
        "sections": [{"heading": f"{k}. 소제목", "body": body * 2, "image": f"slide_{k + 1:02d}.jpg", "experience_hint": ""} for k in range(4)],
        "faq": [{"question": f"질문 {k}?", "answer": "답변이에요."} for k in range(3)], "conclusion": body,
        "tags": [f"태그{i}" for i in range(10)], "sources": ["행정안전부", "국세청"]}, ensure_ascii=False), encoding="utf-8")
    return d


@pytest.fixture
def s(tmp_path):
    st = Settings()
    st.content_source = "routine"
    st.output_dir, st.data_dir, st.fonts_dir = tmp_path / "output", tmp_path / "data", FONTS
    st.platforms = ["instagram", "threads"]  # 릴스를 빼서 브라우저 렌더링 없이 시험
    st.output_dir.mkdir()
    st.data_dir.mkdir()
    return st


def test_valid_content_passes_and_bad_shapes_are_reported(tmp_path):
    d = make_content(tmp_path, "20261116-1357-a")
    assert validate_content.check(d) == []
    (d / "blog.json").unlink()
    assert any("blog.json" in e for e in validate_content.check(d))
    bad = make_content(tmp_path, "20261117-1357-b", style="caption-beat")
    assert any("style" in e for e in validate_content.check(bad))
    card = json.loads((bad / "card.json").read_text(encoding="utf-8"))
    card["slides"][0]["highlight"] = "최대 16.5퍼센트 공제"
    (bad / "card.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
    assert any("highlight" in e for e in validate_content.check(bad))
    blog = json.loads((bad / "blog.json").read_text(encoding="utf-8"))
    blog["sections"][0]["image"] = "slide_09.jpg"  # 슬라이드는 6장
    blog["intro"] = "짧음"
    (bad / "blog.json").write_text(json.dumps(blog, ensure_ascii=False), encoding="utf-8")
    errs = validate_content.check(bad)
    assert any("section.image" in e for e in errs)


def test_pipeline_in_routine_mode_needs_no_llm_and_refuses_to_write(s, tmp_path):
    p = Pipeline(s, store=Store(tmp_path / "data" / "cardbot.db"))
    assert p.writer is None
    with pytest.raises(RuntimeError, match="LLM이 없습니다"):
        p.recommend()
    assert p.run_once(publish=False) == []  # 올릴 원고가 없으면 조용히 끝난다


def test_config_rejects_unknown_content_source(monkeypatch):
    monkeypatch.setenv("CONTENT_SOURCE", "magic")
    with pytest.raises(ValueError, match="CONTENT_SOURCE"):
        Settings.load()


def test_ingest_builds_the_draft_folder_and_is_idempotent(s, tmp_path):
    d = make_content(tmp_path / "content", "20261116-1357-a")
    p = Pipeline(s, store=Store(tmp_path / "data" / "cardbot.db"))
    folder = p.ingest(d)
    names = {f.name for f in folder.iterdir()}
    for need in ("topic.json", "card.json", "caption.txt", "threads.txt", "blog.json", "blog_naver.txt", "blog_guide.md",
                 "reel.json", "source.txt", "slide_01.jpg", "slide_06.jpg"):
        assert need in names, need
    assert (folder / "source.txt").read_text(encoding="utf-8") == "20261116-1357-a"
    assert "#태그0" in (folder / "caption.txt").read_text(encoding="utf-8")
    assert p.ingest(d) == folder and len(list(s.output_dir.iterdir())) == 1  # 같은 원고는 두 번 만들지 않는다
    assert p.ingest_all(tmp_path / "content") == [folder]


def test_choose_style_avoids_recent_and_beat_styles():
    topic = TopicIdea(title="t", keyword="k", angle="a", hook="h", why_now="w", score=1, risk="")
    card = CardNews(slides=[], caption="", hashtags=[], threads_text="", sources=[])
    assert content.choose_style("chalkboard", (), topic, card) == "chalkboard"
    assert content.choose_style("chalkboard", ("chalkboard", "swiss-motion"), topic, card) == "caption-news"
    assert content.choose_style("caption-beat", (), topic, card) != "caption-beat"  # 비트 스타일은 쓰지 않는다
    assert content.choose_style("nonsense", ("caption-news",), topic, card) in ("chalkboard", "swiss-motion")


def test_run_once_publishes_only_the_newest_unpublished_ingested_draft(s, tmp_path, monkeypatch):
    from cardbot import pipeline

    class Pub:
        def __init__(self, name): self.name, self.needs_video = name, False
        def publish(self, assets, text): return PublishResult(platform=self.name, media_id=f"{self.name}-1", permalink=f"https://x/{self.name}")

    class Host:
        def upload(self, files, folder): return [f"https://cdn.example.com/{f.name}" for f in files]
        def candidates(self, url): return [url]

    monkeypatch.setattr(pipeline, "make_publishers", lambda st: [Pub(n) for n in st.platforms])
    monkeypatch.setattr(pipeline, "make_host", lambda st: Host())
    p = Pipeline(s, store=Store(tmp_path / "data" / "cardbot.db"))
    folder = p.ingest(make_content(tmp_path / "content", "20261116-1357-a"))
    assert p.unpublished_ingested() == [folder]
    assert p.run_once(publish=False) == [folder] and not (folder / "published.json").exists()  # 초안만
    assert p.run_once(publish=True) == [folder]
    posts = json.loads((folder / "published.json").read_text(encoding="utf-8"))["posts"]
    assert set(posts) == {"instagram", "threads"}
    assert p.unpublished_ingested() == []  # 다 올렸으니 다시 올리지 않는다
    # 30시간이 지난 초안은 늦게 올리지 않는다
    old = s.output_dir / f"{datetime.now() - timedelta(hours=40):%Y%m%d-%H%M%S}-old"
    old.mkdir()
    (old / "source.txt").write_text("x", encoding="utf-8")
    assert old not in p.unpublished_ingested()


def test_validator_needs_no_llm_sdk():
    """루틴 환경에는 pydantic·requests 만 설치한다: anthropic·google 없이도 불러와져야 한다."""
    code = ("import sys, builtins\n"
            "real = builtins.__import__\n"
            "def fake(name, *a, **k):\n"
            "    if name.split('.')[0] in ('anthropic', 'google', 'numpy'): raise ImportError(name)\n"
            "    return real(name, *a, **k)\n"
            "builtins.__import__ = fake\n"
            "import cardbot.validate_content\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parent.parent, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
