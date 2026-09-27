import json
from pathlib import Path

import pytest

from cardbot import trends
from cardbot.config import Settings
from cardbot.llm import CardNews, Slide, TopicIdea, instagram_caption, normalize
from cardbot.pipeline import Pipeline
from cardbot.publishers import InstagramPublisher, PublishResult, ThreadsPublisher
from cardbot.render import Fonts, ensure_fonts, render_card, wrap
from cardbot.store import Store

TRENDS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item><title>연말정산</title><ht:approx_traffic>20,000+</ht:approx_traffic>
<ht:news_item><ht:news_item_title>올해 연말정산 달라지는 점</ht:news_item_title>
<ht:news_item_url>https://example.com/a</ht:news_item_url></ht:news_item></item>
<item><title>아이폰</title><ht:approx_traffic>5K+</ht:approx_traffic></item>
</channel></rss>"""


def sample_card(n=6):
    slides = [Slide(kind="cover", heading="연말정산 13월의 월급 받는 법", body="놓치면 손해", highlight="최대 100만원")]
    for i in range(n - 3):
        slides.append(Slide(kind="content", heading=f"포인트 {i+1}: 카드 공제 한도 확인하기",
                            body="신용카드보다 체크카드·현금영수증 공제율이 두 배 높아요.\n하반기엔 체크카드를 쓰세요.",
                            highlight="공제율 30%" if i % 2 == 0 else ""))
    slides.append(Slide(kind="summary", heading="한눈에 정리", body="1. 카드 한도\n2. 월세 공제\n3. 연금저축", highlight=""))
    slides.append(Slide(kind="cta", heading="저장해두고 1월에 꺼내 보세요", body="친구에게도 공유해 주세요", highlight=""))
    return CardNews(slides=slides, caption="연말정산 이것만 챙기세요", hashtags=["#연말정산", "절세", "절세", "직장인 팁"],
                    threads_text="가" * 600, sources=["https://example.com/a"])


def sample_blog(images=("slide_01.jpg", "slide_02.jpg")):
    from cardbot.llm import BlogFAQ, BlogPost, BlogSection
    return BlogPost(
        titles=["연말정산 환급 늘리는 5가지 방법", "13월의 월급 챙기는 법"],
        main_keyword="연말정산",
        sub_keywords=["연말정산 환급", "카드 공제"],
        meta_description="연말정산 환급액을 늘리는 방법 정리",
        intro="연말정산 시즌입니다.\n\n미리 챙기면 환급이 늘어요.",
        sections=[
            BlogSection(heading="카드 공제 한도", body="체크카드 공제율이 <높아요> & 좋아요.", image=images[1], experience_hint="내 카드 사용 비율"),
            BlogSection(heading="월세 공제", body="월세도 공제됩니다.", image="없는파일.jpg", experience_hint=""),
        ],
        faq=[BlogFAQ(question="언제 하나요?", answer="1월입니다.")],
        conclusion="미리 준비하세요.",
        tags=["#연말정산", "절세", "절세", "직장인 팁"],
        sources=["https://example.com/a", "국세청"],
    )


def test_parse_traffic():
    assert trends.parse_traffic("20,000+") == 20000
    assert trends.parse_traffic("5K+") == 5000
    assert trends.parse_traffic("2만+") == 20000
    assert trends.parse_traffic("") == 0


def test_parse_google_trends():
    sigs = trends.parse_google_trends_rss(TRENDS_XML)
    assert [s.keyword for s in sigs] == ["연말정산", "아이폰"]
    assert sigs[0].traffic == 20000 and sigs[0].context == ["올해 연말정산 달라지는 점"]
    assert "20,000+" in sigs[0].to_prompt_line()


def test_normalize_and_caption():
    card = normalize(sample_card(), 5, 8)
    assert card.hashtags == ["연말정산", "절세", "직장인팁"]
    assert len(card.threads_text) == 500
    assert instagram_caption(card).endswith("#연말정산 #절세 #직장인팁")


def test_normalize_keeps_cta_when_truncating():
    card = normalize(sample_card(12), 5, 8)
    assert len(card.slides) == 8 and card.slides[-1].kind == "cta"


def test_wrap_korean_long_word():
    from PIL import Image, ImageDraw
    fonts = Fonts(ensure_fonts(Path(__file__).parent.parent / "fonts"))
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    f = fonts.get("Regular", 40)
    lines = wrap(d, "가" * 60 + " 짧은 단어", f, 400)
    assert all(d.textlength(l, font=f) <= 400 for l in lines)
    assert "".join(lines).replace(" ", "") == "가" * 60 + "짧은단어"


@pytest.mark.parametrize("theme", ["midnight", "cream"])
def test_render(tmp_path, theme):
    paths = render_card(sample_card(), tmp_path, Path(__file__).parent.parent / "fonts", theme, "mybrand")
    assert len(paths) == 6
    from PIL import Image
    assert Image.open(paths[0]).size == (1080, 1350)


class FakeResp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, json.dumps(data)

    def json(self):
        return self._d


def test_instagram_carousel_flow(monkeypatch):
    calls = []

    def post(url, data, timeout):
        calls.append(("POST", url.split("/v23.0/")[1], dict(data)))
        if url.endswith("/media_publish"):
            return FakeResp({"id": "MEDIA1"})
        return FakeResp({"id": f"C{len(calls)}"})

    def get(url, params, timeout):
        calls.append(("GET", url.split("/v23.0/")[1], dict(params)))
        if params.get("fields") == "permalink":
            return FakeResp({"permalink": "https://instagram.com/p/x"})
        return FakeResp({"status_code": "FINISHED"})

    monkeypatch.setattr("cardbot.publishers.requests.post", post)
    monkeypatch.setattr("cardbot.publishers.requests.get", get)
    pub = InstagramPublisher("123", "TOKEN", "https://graph.instagram.com/v23.0", sleep=lambda s: None)
    res = pub.publish({"image_urls": ["https://x/1.jpg", "https://x/2.jpg"]}, "캡션")
    assert res.media_id == "MEDIA1" and res.permalink.endswith("/p/x")
    posts = [c for c in calls if c[0] == "POST"]
    assert posts[0][2]["is_carousel_item"] == "true"
    assert posts[2][2]["media_type"] == "CAROUSEL" and posts[2][2]["children"] == "C1,C2"
    assert posts[3][1] == "123/media_publish"


def test_threads_waits_and_errors(monkeypatch):
    states = iter(["IN_PROGRESS", "ERROR", "ERROR"])
    monkeypatch.setattr("cardbot.publishers.requests.post", lambda url, data, timeout: FakeResp({"id": "T1"}))
    monkeypatch.setattr("cardbot.publishers.requests.get",
                        lambda url, params, timeout: FakeResp({"status": next(states)}))
    pub = ThreadsPublisher("9", "TOK", "https://graph.threads.net/v1.0", sleep=lambda s: None)
    with pytest.raises(Exception, match="ERROR"):
        pub.publish({"image_urls": ["a", "b"]}, "text")


class FakeWriter:
    def recommend(self, signals, top, recent, n=5):
        return [TopicIdea(title="연말정산 꿀팁", keyword="연말정산", angle="체크리스트", hook="h",
                          why_now="시즌", score=90, risk="")]

    def research(self, topic, signals):
        return "notes"

    def write(self, topic, notes):
        return normalize(sample_card(), 5, 8)

    def write_blog(self, topic, notes, card, images):
        return sample_blog(images)


from cardbot.hosting import ImageHost


class FakeHost(ImageHost):
    def upload(self, files, folder):
        return [f"https://cdn/{folder}/{f.name}" for f in files]


class FakePub:
    def __init__(self, name, fail=False):
        self.name, self.fail, self.calls = name, fail, 0

    needs_video = False

    def publish(self, assets, text):
        self.calls += 1
        self.last = (assets, text)
        if self.fail:
            raise RuntimeError("boom")
        return PublishResult(self.name, f"{self.name}-id", f"https://{self.name}/p")

    def insights(self, media_id):
        return {"views": 1000, "saves": 10}


def test_pipeline_end_to_end(tmp_path, monkeypatch):
    s = Settings()
    s.output_dir, s.data_dir = tmp_path / "out", tmp_path
    s.fonts_dir = Path(__file__).parent.parent / "fonts"
    s.output_dir.mkdir()
    s.platforms = ["instagram", "threads"]  # 릴스 렌더링은 전용 테스트에서
    monkeypatch.setattr("cardbot.pipeline.trends.collect", lambda s: [])
    p = Pipeline(s, writer=FakeWriter(), store=Store(tmp_path / "db.sqlite"))
    topics, sigs = p.recommend()
    folder = p.make(topics[0], sigs)
    assert len(list(folder.glob("slide_*.jpg"))) == 6

    ig, th = FakePub("instagram"), FakePub("threads", fail=True)
    meta = p.publish(folder, publishers=[ig, th], host=FakeHost())
    assert "instagram" in meta["posts"] and "threads" in meta["errors"]

    # 재시도 시 이미 성공한 플랫폼은 다시 올리지 않는다
    th.fail = False
    meta = p.publish(folder, publishers=[ig, th], host=FakeHost())
    assert ig.calls == 1 and th.calls == 2 and not meta.get("errors")
    assert json.loads((folder / "published.json").read_text())["posts"].keys() == {"instagram", "threads"}

    assert p.store.recent_topics() == ["연말정산 꿀팁"]
    assert p.refresh_insights(publishers=[ig, th]) == 2
    assert p.store.top_posts()[0]["views"] == 1000


def test_dotenv_inline_comments(tmp_path, monkeypatch):
    from cardbot.config import load_dotenv
    env = tmp_path / ".env"
    env.write_text('A_X=high   # 설명\nB_X="a # b"\nC_X=https://x.com/#frag\n', encoding="utf-8")
    for k in ("A_X", "B_X", "C_X"):
        monkeypatch.delenv(k, raising=False)
    load_dotenv(env)
    import os
    assert os.environ["A_X"] == "high" and os.environ["B_X"] == "a # b"
    assert os.environ["C_X"] == "https://x.com/#frag"


def test_empty_env_falls_back_to_default(monkeypatch, tmp_path):
    # GitHub Actions에서 설정하지 않은 vars는 빈 문자열로 들어온다
    monkeypatch.setenv("ANTHROPIC_MODEL", "")
    monkeypatch.setenv("SLIDES_MAX", "")
    monkeypatch.setenv("CARDBOT_DATA_DIR", str(tmp_path / "d"))
    s = Settings.load()
    assert s.anthropic_model == "claude-opus-5" and s.slides_max == 8
    assert s.data_dir == tmp_path / "d" and s.data_dir.exists()


# ---------- LLM 제공자 ----------
from types import SimpleNamespace

from cardbot.llm import ClaudeWriter, GeminiWriter, TopicList, make_writer


class FakeGeminiModels:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def generate_content(self, model, contents, config):
        self.calls.append(SimpleNamespace(model=model, contents=contents, config=config))
        return self.responses.pop(0)


def gemini_resp(text, parsed=None, finish="STOP", chunks=None):
    cand = SimpleNamespace(
        finish_reason=finish,
        grounding_metadata=SimpleNamespace(grounding_chunks=chunks) if chunks else None,
    )
    return SimpleNamespace(text=text, parsed=parsed, candidates=[cand], prompt_feedback=None)


TOPICS_JSON = json.dumps({"topics": [
    {"title": "낮은 점수", "keyword": "k1", "angle": "a", "hook": "h", "why_now": "w", "score": 40, "risk": ""},
    {"title": "높은 점수", "keyword": "k2", "angle": "a", "hook": "h", "why_now": "w", "score": 90, "risk": ""},
]}, ensure_ascii=False)


def test_make_writer_selects_provider(monkeypatch):
    s = Settings()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert isinstance(make_writer(s), ClaudeWriter)
    s.llm_provider = "gemini"
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert isinstance(make_writer(s), GeminiWriter)
    s.llm_provider = "gpt"
    with pytest.raises(ValueError):
        make_writer(s)


def test_gemini_recommend_structured_output():
    s = Settings()
    models = FakeGeminiModels([gemini_resp(TOPICS_JSON)])  # parsed 없음 → text에서 검증
    w = GeminiWriter(s, client=SimpleNamespace(models=models))
    topics = w.recommend([trends.TrendSignal("연말정산", "google_trends", 20000)], [], [], n=2)
    assert [t.title for t in topics] == ["높은 점수", "낮은 점수"]
    cfg = models.calls[0].config
    assert models.calls[0].model == "gemini-flash-latest"
    assert cfg.response_mime_type == "application/json" and cfg.response_schema is TopicList
    assert "연말정산" in models.calls[0].contents


def test_gemini_research_uses_google_search_and_lists_sources():
    s = Settings()
    chunk = SimpleNamespace(web=SimpleNamespace(uri="https://news.example/a", title="example.com", domain=None))
    models = FakeGeminiModels([gemini_resp("팩트1 (출처)", chunks=[chunk, chunk])])
    w = GeminiWriter(s, client=SimpleNamespace(models=models))
    topic = TopicIdea(title="t", keyword="k", angle="a", hook="h", why_now="w", score=1, risk="")
    notes = w.research(topic, [])
    assert "팩트1" in notes and notes.count("https://news.example/a") == 1
    assert models.calls[0].config.tools[0].google_search is not None


def test_gemini_blocked_response_raises_and_research_degrades():
    s = Settings()
    w = GeminiWriter(s, client=SimpleNamespace(models=FakeGeminiModels([gemini_resp("", finish="SAFETY")])))
    with pytest.raises(RuntimeError, match="차단"):
        w._parse("sys", "user", TopicList)
    w = GeminiWriter(s, client=SimpleNamespace(models=FakeGeminiModels([gemini_resp("", finish="SAFETY")])))
    topic = TopicIdea(title="t", keyword="k", angle="a", hook="h", why_now="w", score=1, risk="")
    assert "팩트 시트" not in w.research(topic, [])  # 헤드라인만으로 계속 진행


def test_gemini_write_normalizes_card():
    s = Settings()
    card = sample_card()
    models = FakeGeminiModels([gemini_resp(card.model_dump_json(), parsed=card)])
    w = GeminiWriter(s, client=SimpleNamespace(models=models))
    topic = TopicIdea(title="t", keyword="k", angle="a", hook="h", why_now="w", score=1, risk="")
    out = w.write(topic, "notes")
    assert len(out.threads_text) == 500 and out.hashtags[0] == "연말정산"


# ---------- 이미지 호스팅 ----------
from cardbot.hosting import GitHubHost, public_folder


def test_public_folder_is_ascii_and_stable():
    name = "20260927-091029-연휴-다음-날-통장"
    out = public_folder(name)
    assert out.isascii() and out.startswith("20260927-091029-") and out == public_folder(name)
    assert public_folder("직접주제").isascii()
    assert public_folder(name) != public_folder(name + "2")


def test_github_host_ascii_urls_and_overwrite(monkeypatch, tmp_path):
    img = tmp_path / "slide_01.jpg"
    img.write_bytes(b"jpg")
    puts = []

    def get(url, headers, params, timeout):
        return FakeResp({"sha": "abc"}, 200)  # 이미 있는 파일

    def put(url, headers, json, timeout):
        puts.append((url, json))
        return FakeResp({}, 200)

    monkeypatch.setattr("cardbot.hosting.requests.get", get)
    monkeypatch.setattr("cardbot.hosting.requests.put", put)
    urls = GitHubHost("t", "o/r", "data", "images").upload([img], "20260927-091029-한글-폴더")
    assert urls[0].isascii() and urls[0].startswith("https://raw.githubusercontent.com/o/r/data/images/20260927-091029-")
    assert puts[0][1]["sha"] == "abc" and puts[0][0].isascii()


def test_publish_reuploads_non_ascii_urls(tmp_path, monkeypatch):
    s = Settings()
    s.output_dir, s.data_dir = tmp_path / "out", tmp_path
    s.fonts_dir = Path(__file__).parent.parent / "fonts"
    s.output_dir.mkdir()
    s.platforms = ["instagram", "threads"]  # 릴스 렌더링은 전용 테스트에서
    monkeypatch.setattr("cardbot.pipeline.trends.collect", lambda s: [])
    p = Pipeline(s, writer=FakeWriter(), store=Store(tmp_path / "db.sqlite"))
    folder = p.make(p.recommend()[0][0], [])
    # 이전 버전이 한글 URL을 저장해 두고 게시에 실패한 상태
    (folder / "published.json").write_text(json.dumps({"image_urls": ["https://x/한글/1.jpg"]}))

    class Host:
        calls = 0

        def upload(self, files, name):
            Host.calls += 1
            return [f"https://cdn/{public_folder(name)}/{f.name}" for f in files]

    meta = p.publish(folder, publishers=[FakePub("instagram")], host=Host())
    assert Host.calls == 1 and all(u.isascii() for u in meta["image_urls"])
    p.publish(folder, publishers=[FakePub("instagram")], host=Host())  # ASCII면 다시 올리지 않음
    assert Host.calls == 1



# ---------- 블로그 ----------
from cardbot.blog import BLOG_FILES, naver_text, tistory_html
from cardbot.llm import normalize_blog


def test_normalize_blog_drops_unknown_images_and_dedupes_tags():
    post = normalize_blog(sample_blog(), ["slide_01.jpg", "slide_02.jpg"])
    assert [s.image for s in post.sections] == ["slide_02.jpg", ""]
    assert post.tags == ["연말정산", "절세", "직장인팁"]


def test_blog_formats():
    post = normalize_blog(sample_blog(), ["slide_01.jpg", "slide_02.jpg"])
    txt = naver_text(post)
    assert "■ 카드 공제 한도" in txt and "slide_02.jpg" in txt and "Q. 언제 하나요?" in txt
    assert "✍️" in txt and "- 국세청" in txt
    html = tistory_html(post)
    assert "<h2>카드 공제 한도</h2>" in html and "&lt;높아요&gt; &amp; 좋아요" in html  # 이스케이프
    assert '<a href="https://example.com/a">' in html and "<li>국세청</li>" in html
    assert html.count("<p>") >= 3  # 문단 분리


def _pipeline(tmp_path, monkeypatch, writer):
    s = Settings()
    s.output_dir, s.data_dir = tmp_path / "out", tmp_path
    s.fonts_dir = Path(__file__).parent.parent / "fonts"
    s.output_dir.mkdir()
    s.platforms = ["instagram", "threads"]  # 릴스 렌더링은 전용 테스트에서
    monkeypatch.setattr("cardbot.pipeline.trends.collect", lambda s: [])
    return Pipeline(s, writer=writer, store=Store(tmp_path / "db.sqlite"))


def test_make_creates_blog_files(tmp_path, monkeypatch):
    p = _pipeline(tmp_path, monkeypatch, FakeWriter())
    folder = p.make(p.recommend()[0][0], [])
    for f in BLOG_FILES:
        assert (folder / f).exists(), f
    assert "연말정산 환급 늘리는 5가지 방법" in (folder / "blog_guide.md").read_text()


def test_blog_failure_does_not_block_card_or_publish(tmp_path, monkeypatch):
    class BrokenBlog(FakeWriter):
        def write_blog(self, *a):
            raise RuntimeError("quota")

    p = _pipeline(tmp_path, monkeypatch, BrokenBlog())
    folder = p.make(p.recommend()[0][0], [])
    assert len(list(folder.glob("slide_*.jpg"))) == 6
    assert (folder / "blog_error.txt").read_text() == "quota" and not (folder / "blog.json").exists()
    meta = p.publish(folder, publishers=[FakePub("instagram")], host=FakeHost())
    assert "instagram" in meta["posts"] and not p.failures


def test_publish_backfills_blog_for_old_drafts(tmp_path, monkeypatch):
    p = _pipeline(tmp_path, monkeypatch, FakeWriter())
    p.s.blog_enabled = False
    folder = p.make(p.recommend()[0][0], [])  # 블로그 기능 이전 초안
    assert not (folder / "blog.json").exists()
    p.s.blog_enabled = True
    p.publish(folder, publishers=[FakePub("instagram")], host=FakeHost())
    assert (folder / "blog_naver.txt").exists() and not (folder / "blog_error.txt").exists()


def test_gemini_write_blog_passes_slide_list():
    from cardbot.llm import BlogPost
    s = Settings()
    post = sample_blog()
    models = FakeGeminiModels([gemini_resp(post.model_dump_json(), parsed=post)])
    w = GeminiWriter(s, client=SimpleNamespace(models=models))
    topic = TopicIdea(title="t", keyword="k", angle="a", hook="h", why_now="w", score=1, risk="")
    out = w.write_blog(topic, "notes", sample_card(), ["slide_01.jpg", "slide_02.jpg"])
    assert models.calls[0].config.response_schema is BlogPost
    assert "slide_02.jpg: [content]" in models.calls[0].contents
    assert out.sections[1].image == ""



# ---------- 릴스 ----------
from cardbot.hosting import GitHubHost as _GH
from cardbot.publishers import ReelsPublisher
from cardbot.reel import render_reel, slide_seconds


def test_slide_seconds_range():
    assert slide_seconds("짧음", "", "cover") == 3.0
    assert slide_seconds("가" * 5, "", "content") == 3.0
    assert slide_seconds("가" * 200, "", "content") == 6.0


def test_render_reel_produces_vertical_mp4_with_audio(tmp_path):
    import subprocess
    import imageio_ffmpeg
    card = sample_card()
    slides = render_card(card, tmp_path, Path(__file__).parent.parent / "fonts", "cream", "me")
    video, cover = render_reel(card, slides, tmp_path / "reel.mp4", Path(__file__).parent.parent / "fonts",
                               "cream", "me", fps=4)
    from PIL import Image
    assert Image.open(cover).size == (1080, 1920)
    info = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-i", str(video)], capture_output=True, text=True).stderr
    assert "1080x1920" in info and "Audio: aac" in info and "h264" in info


def test_github_candidates_prefer_jsdelivr_for_video(monkeypatch, tmp_path):
    f = tmp_path / "reel.mp4"
    f.write_bytes(b"v")
    monkeypatch.setattr("cardbot.hosting.requests.get", lambda url, headers, params, timeout: FakeResp({}, 404))
    monkeypatch.setattr("cardbot.hosting.requests.put",
                        lambda url, headers, json, timeout: FakeResp({"commit": {"sha": "abc123"}}, 201))
    host = _GH("t", "o/r", "data", "images")
    (url,) = host.upload([f], "20260927-091029-x")
    cands = host.candidates(url)
    assert cands[0].startswith("https://cdn.jsdelivr.net/gh/o/r@abc123/images/20260927-091029-")
    assert cands[0].endswith("/reel.mp4") and cands[1] == url


def test_reels_publisher_falls_back_to_next_url(monkeypatch):
    posts = []
    status = {"C1": "ERROR", "C2": "FINISHED"}

    def post(url, data, timeout):
        posts.append(dict(data))
        if url.endswith("/media_publish"):
            return FakeResp({"id": "REEL1"})
        return FakeResp({"id": f"C{len([p for p in posts if 'video_url' in p])}"})

    def get(url, params, timeout):
        cid = url.rsplit("/", 1)[1]
        if params.get("fields") == "permalink":
            return FakeResp({"permalink": "https://instagram.com/reel/x"})
        return FakeResp({"status_code": status.get(cid, "FINISHED"), "status": "Error: bad media"})

    monkeypatch.setattr("cardbot.publishers.requests.post", post)
    monkeypatch.setattr("cardbot.publishers.requests.get", get)
    pub = ReelsPublisher("1", "T", "https://graph.instagram.com/v23.0", sleep=lambda s: None)
    res = pub.publish({"video_urls": ["https://cdn/a.mp4", "https://raw/a.mp4"], "reel_cover_url": "https://c.jpg"}, "캡션")
    assert res.media_id == "REEL1" and res.permalink.endswith("/reel/x")
    tried = [p["video_url"] for p in posts if "video_url" in p]
    assert tried == ["https://cdn/a.mp4", "https://raw/a.mp4"]
    assert posts[0]["media_type"] == "REELS" and posts[0]["share_to_feed"] == "true" and posts[0]["cover_url"] == "https://c.jpg"


def test_pipeline_renders_and_publishes_reel(tmp_path, monkeypatch):
    p = _pipeline(tmp_path, monkeypatch, FakeWriter())
    p.s.platforms = ["instagram", "reels", "threads"]
    p.s.reels_fps = 4
    folder = p.make(p.recommend()[0][0], [])
    assert (folder / "reel.mp4").exists() and (folder / "reel_cover.jpg").exists()

    class Host(FakeHost):
        def candidates(self, url):
            return ["https://cdn/" + url.rsplit("/", 1)[1], url]

    reels = FakePub("reels")
    reels.needs_video = True
    ig, th = FakePub("instagram"), FakePub("threads")
    meta = p.publish(folder, publishers=[ig, reels, th], host=Host())
    assert set(meta["posts"]) == {"instagram", "reels", "threads"}
    assets, text = reels.last
    assert assets["video_urls"][0] == "https://cdn/reel.mp4" and assets["reel_cover_url"].endswith("reel_cover.jpg")
    assert text == (folder / "caption.txt").read_text().strip()  # 릴스는 인스타 캡션 사용
    assert th.last[1] == (folder / "threads.txt").read_text().strip()[:500]


def test_publish_renders_reel_for_old_drafts(tmp_path, monkeypatch):
    p = _pipeline(tmp_path, monkeypatch, FakeWriter())
    folder = p.make(p.recommend()[0][0], [])  # 릴스 기능 이전 초안
    assert not (folder / "reel.mp4").exists()
    p.s.platforms = ["instagram", "reels"]
    p.s.reels_fps = 4
    reels = FakePub("reels")
    reels.needs_video = True
    meta = p.publish(folder, publishers=[reels], host=FakeHost())
    assert (folder / "reel.mp4").exists() and "reels" in meta["posts"]
