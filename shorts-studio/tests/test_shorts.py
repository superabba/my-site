import base64
import io
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
from PIL import Image

from shorts import aiimage, daily
from shorts.render import RenderOptions, render
from shorts.ffmpeg import run
from shorts.writer import normalize_script, split_text


def png_bytes(color=(200, 120, 60), size=(90, 160)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- 대본

def test_normalize_keeps_image_prompt_and_style():
    s = normalize_script({"title": "t", "mood": "calm", "caption": "c", "hashtags": ["#a"], "visual_style": "flat",
                          "scenes": [{"narration": "안녕", "headline": "", "keywords": "", "image_prompt": "a coin"}]})
    assert s["visual_style"] == "flat" and s["scenes"][0]["image_prompt"] == "a coin"
    assert split_text("첫 문장. 둘째 문장.")["scenes"][0]["image_prompt"] == ""


# ---------------------------------------------------------------- 나노바나나

class FakeClient:
    def __init__(self, data=None, error=None):
        self.calls = []
        self.data, self.error = data, error
        self.interactions = self

    def create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        img = type("Img", (), {"data": self.data, "mime_type": "image/png"})
        return type("Interaction", (), {"output_image": img})


def test_prompt_forbids_text_and_people():
    p = aiimage.build_prompt("통장에 쌓이는 동전", "flat pastel")
    assert "no text" in p and "No real, famous" in p and "flat pastel" in p and "통장에 쌓이는 동전" in p


def test_generate_image_saves_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(aiimage, "IMAGE_DIR", tmp_path)
    client = FakeClient(base64.b64encode(png_bytes()).decode())
    p1 = aiimage.generate_image("coins", "", client=client)
    p2 = aiimage.generate_image("coins", "", client=client)
    assert p1 == p2 and p1.suffix == ".png" and len(client.calls) == 1
    req = client.calls[0]
    assert req["model"] == "gemini-nano-banana-2.1"
    assert req["response_format"] == {"type": "image", "aspect_ratio": "9:16", "image_size": "2K"}
    assert Image.open(p1).size == (90, 160)


def test_generate_image_accepts_raw_bytes_and_lite_is_1k(tmp_path, monkeypatch):
    monkeypatch.setattr(aiimage, "IMAGE_DIR", tmp_path)
    client = FakeClient(png_bytes())
    aiimage.generate_image("x", "", model="gemini-3.1-flash-lite-image", client=client)
    assert client.calls[0]["response_format"]["image_size"] == "1K"


def test_billing_error_is_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(aiimage, "IMAGE_DIR", tmp_path)
    with pytest.raises(aiimage.ImageGenError) as e:
        aiimage.generate_image("x", "", client=FakeClient(error=RuntimeError("429 free tier limit: 0")))
    assert e.value.fatal and "결제" in str(e.value)
    with pytest.raises(aiimage.ImageGenError) as e:
        aiimage.generate_image("y", "", client=FakeClient(error=RuntimeError("500 internal")))
    assert not e.value.fatal


# ---------------------------------------------------------------- 영상 합성 (나노바나나 배경)

def small_opts(tmp_path, **kw):
    return RenderOptions(width=180, height=320, fps=10, voice_on=False, music_mode="none",
                         output_dir=tmp_path / "out", **kw)


def test_render_uses_ai_images(tmp_path, monkeypatch):
    calls = []

    def fake(desc, key, style, model, size):
        calls.append((desc, style))
        p = tmp_path / f"img{len(calls)}.png"
        p.write_bytes(png_bytes())
        return p

    monkeypatch.setattr(aiimage, "generate_image", fake)
    script = normalize_script({"title": "금리", "mood": "calm", "caption": "", "hashtags": [], "visual_style": "flat",
                               "scenes": [{"narration": "금리가 내렸어요.", "headline": "", "keywords": "",
                                           "image_prompt": "falling arrow"},
                                          {"narration": "대출 이자는요?", "headline": "", "keywords": "",
                                           "image_prompt": ""}]})
    res = render(script, small_opts(tmp_path, bg_mode="ai_image", gemini_key="k"))
    assert res.ai_images == 2 and res.warnings == []
    assert sorted(calls) == [("falling arrow", "flat"), ("대출 이자는요?", "flat")]
    assert json.loads(res.video.with_suffix(".json").read_text(encoding="utf-8"))["ai_images"] == 2


def test_render_falls_back_when_images_fail(tmp_path, monkeypatch):
    def fail(*a, **k):
        raise aiimage.ImageGenError("그림 생성 실패: billing", fatal=True)

    monkeypatch.setattr(aiimage, "generate_image", fail)
    script = split_text("하나입니다. 둘입니다. 셋입니다.", target_chars=5)
    res = render(script, small_opts(tmp_path, bg_mode="ai_image", gemini_key="k", image_fallback="gradient"))
    assert res.video.exists() and res.ai_images == 0
    assert len([w for w in res.warnings if "billing" in w]) == 1  # 같은 키 문제는 한 번만


def test_render_without_gemini_key_warns(tmp_path):
    res = render(split_text("한 문장입니다."), small_opts(tmp_path, bg_mode="ai_image"))
    assert any("Gemini API 키" in w for w in res.warnings)


# ---------------------------------------------------------------- 매일 자동

def test_theme_rotates_daily(monkeypatch):
    monkeypatch.delenv("SHORTS_THEMES", raising=False)
    names = [daily.theme_for(date(2026, 10, 20) + timedelta(days=i))[0] for i in range(8)]
    assert len(set(names[:7])) == 7 and names[7] == names[0]
    monkeypatch.setenv("SHORTS_THEMES", "A: 하나\nB: 둘")
    assert {daily.theme_for(date(2026, 10, 20))[0], daily.theme_for(date(2026, 10, 21))[0]} == {"A", "B"}


def write_record(d, name, **rec):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")


def test_published_today_only_counts_daily(tmp_path):
    kst_morning = datetime(2026, 10, 21, 7, 11, tzinfo=daily.KST).astimezone(timezone.utc).isoformat()
    write_record(tmp_path, "manual", posts={"reels": {"published_at": kst_morning}})
    assert not daily.published_today(tmp_path, date(2026, 10, 21))
    write_record(tmp_path, "auto", daily=True, posts={"youtube": {"published_at": kst_morning}})
    assert daily.published_today(tmp_path, date(2026, 10, 21))
    assert not daily.published_today(tmp_path, date(2026, 10, 22))


def test_cardnews_topics_read_only(tmp_path):
    db = tmp_path / "cardbot.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE posts (created_at TEXT, topic TEXT)")
    con.execute("INSERT INTO posts VALUES (?, ?)", (datetime.now().isoformat(), "연말정산 꿀팁"))
    con.execute("INSERT INTO posts VALUES (?, ?)", ((datetime.now() - timedelta(days=30)).isoformat(), "오래된 주제"))
    con.commit()
    con.close()
    before = db.read_bytes()
    assert daily.cardnews_topics(db) == ["연말정산 꿀팁"]
    assert db.read_bytes() == before
    assert daily.cardnews_topics(tmp_path / "없음.db") == []


def test_build_post(monkeypatch):
    monkeypatch.delenv("SHORTS_DAILY_PLATFORMS", raising=False)
    monkeypatch.setenv("SHORTS_YOUTUBE_PRIVACY", "unlisted")
    post = daily.build_post({"title": "금리 인하", "caption": "대출 이자 계산법", "hashtags": ["#금리", "경제"]},
                            {"topic": "금리 인하", "keyword": "기준금리"}, "경제 뉴스 쉽게 풀기", 3,
                            ["장면 2: Pixabay · a · u"], {"youtube": "video.mp4", "meta": "video.mp4"})
    assert post["daily"] and post["youtube_synthetic"] and post["youtube_privacy"] == "unlisted"
    assert post["platforms"] == ["reels", "threads", "youtube"]
    assert post["hashtags"][:3] == ["금리", "경제", "경제주막"]
    assert daily.DISCLAIMER in post["instagram_caption"] and "Pixabay" in post["youtube_description"]
    assert len(post["threads_text"]) <= 500


def test_wait_until(monkeypatch):
    slept = []
    fixed = datetime(2026, 10, 21, 6, 58, tzinfo=daily.KST)
    monkeypatch.setattr(daily, "now_kst", lambda: fixed)
    assert daily.wait_until("07:10", sleep=slept.append) == 720 and slept == [720]
    monkeypatch.setattr(daily, "now_kst", lambda: fixed.replace(hour=8))
    assert daily.wait_until("07:10", sleep=slept.append) == 0 and len(slept) == 1
    # 06:05에 깨워 06:09에 영상이 다 만들어져도 07:10까지 기다린다 (1시간 넘게)
    monkeypatch.setattr(daily, "now_kst", lambda: fixed.replace(hour=6, minute=9))
    assert daily.wait_until("07:10", sleep=slept.append) == 3660 and slept[-1] == 3660


def test_check_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(daily, "now_kst", lambda: datetime(2026, 10, 21, 13, 0, tzinfo=daily.KST))
    assert daily.main(["check", "--records-dir", str(tmp_path), "--hours", "6-11"]) == daily.SKIP_CODE
    monkeypatch.setattr(daily, "now_kst", lambda: datetime(2026, 10, 21, 6, 45, tzinfo=daily.KST))
    assert daily.main(["check", "--records-dir", str(tmp_path), "--hours", "6-11"]) == 0


def test_make_end_to_end_with_fakes(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    monkeypatch.setattr(daily, "collect_signals", lambda q: ["[뉴스] 기준금리 동결"])
    seen = {}
    monkeypatch.setattr(daily, "pick_topic", lambda key, model, theme, signals, avoid: seen.update(avoid=avoid) or
                        {"topic": "금리 동결, 내 대출은?", "keyword": "기준금리", "angle": "대출자 입장", "why": "검색 급증"})
    monkeypatch.setattr(daily, "research", lambda key, model, topic: "- 기준금리 2.50% (한국은행, 2026-10-16)")

    def fake_write(key, model, topic, category, tone, length, notes="", facts=""):
        seen.update(topic=topic, tone=tone, length=length, facts=facts)
        return split_text("금리가 동결됐어요. 대출 이자는 그대로예요. 저장해 두세요.", "금리 동결")

    monkeypatch.setattr(daily, "write_script", fake_write)
    real_render = daily.render
    monkeypatch.setattr(daily, "render", lambda script, opts, progress=None: real_render(
        script, RenderOptions(width=180, height=320, fps=10, voice_on=False, music_mode="none",
                              bg_mode=opts.bg_mode, output_dir=opts.output_dir), progress))
    write_record(tmp_path / "records", "old", topic="지난주 주제", created_at=datetime.now().isoformat())
    blog_calls = []

    def fake_blog(key, model, script, facts, topic, theme, images):
        blog_calls.append(images)
        return {"titles": ["금리 동결, 내 대출 이자는?"], "sections": [{"heading": "h", "body": "b", "image": images[0],
                                                                    "experience_hint": ""}], "tags": ["금리"]}

    monkeypatch.setattr(daily, "write_blog", fake_blog)
    post = daily.make(tmp_path / "out", tmp_path / "records", None)
    out = tmp_path / "out"
    assert (out / "video.mp4").exists() and (out / "post.json").exists() and (out / "summary.md").exists()
    assert len((out / "id.txt").read_text()) == 24
    blog = json.loads((out / "blog" / "blog.json").read_text(encoding="utf-8"))
    assert blog["video_id"] == (out / "id.txt").read_text() and blog["cover"] == "cover.jpg"
    n_scenes = len(json.loads((out / "script.json").read_text(encoding="utf-8"))["scenes"])
    expected = [f"scene_{i:02d}.jpg" for i in range(1, n_scenes + 1)]
    assert blog_calls[0] == expected == blog["images"]
    assert (out / "blog" / "cover.jpg").exists() and all((out / "blog" / n).exists() for n in expected)
    assert "블로그 글: 금리 동결" in (out / "summary.md").read_text(encoding="utf-8")
    assert seen["tone"] == "존댓말 (~해요)" and seen["length"] == 30 and "2.50%" in seen["facts"]
    assert "지난주 주제" in seen["avoid"]
    assert post["topic"] == "금리 동결, 내 대출은?" and post["files"] == {"youtube": "video.mp4", "meta": "video.mp4"}
    assert "금리" in (out / "summary.md").read_text(encoding="utf-8")


def test_long_script_is_shortened_once(tmp_path, monkeypatch):
    from shorts.writer import narration_chars

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    monkeypatch.setattr(daily, "collect_signals", lambda q: [])
    monkeypatch.setattr(daily, "pick_topic", lambda *a: {"topic": "연금저축", "keyword": "연금", "angle": "a", "why": "w"})
    monkeypatch.setattr(daily, "research", lambda *a: "- 사실")
    long_text = " ".join(["연금저축 예상수령액은 가입 당시 이율이 유지된다는 가정이에요."] * 8)
    monkeypatch.setattr(daily, "write_script", lambda *a, **k: split_text(long_text, "연금"))
    shortened = []

    def fake_shorten(key, model, script, length, facts):
        shortened.append((narration_chars(script), length, facts))
        return split_text("연금 예상액은 확정이 아니에요. 지금 이율부터 확인하세요.", "연금")

    monkeypatch.setattr(daily, "shorten_script", fake_shorten)
    monkeypatch.setenv("SHORTS_BLOG", "false")
    real_render = daily.render
    monkeypatch.setattr(daily, "render", lambda script, opts, progress=None: real_render(
        script, RenderOptions(width=180, height=320, fps=10, voice_on=False, music_mode="none",
                              output_dir=opts.output_dir), progress))
    daily.make(tmp_path / "out", None, None)
    assert len(shortened) == 1 and shortened[0][0] > 150 and shortened[0][1:] == (30, "- 사실")
    saved = json.loads((tmp_path / "out" / "script.json").read_text(encoding="utf-8"))
    assert narration_chars(saved) < 40


def test_image_prompt_asks_for_blank_papers():
    assert "must be completely blank" in aiimage.build_prompt("계약서와 동전")


def test_length_target_is_six_chars_per_second():
    from shorts.writer import target_chars

    assert target_chars(30) == 180 and target_chars(60) == 360


class Resp:
    def __init__(self, data, status):
        self._d, self.status_code, self.text = data, status, json.dumps(data)
        self.content = self.text.encode()

    def json(self):
        return self._d


def test_publish_upload_retries_unseen_blob(tmp_path, monkeypatch):
    from shorts import publish as pub

    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
    slept, trees = [], []
    monkeypatch.setattr(pub.time, "sleep", slept.append)

    def request(method, url, **kw):
        path = url.split("/repos/o/r", 1)[1]
        if kw.get("data") is not None:
            while kw["data"].read(1 << 16):
                pass
            return Resp({"sha": "blob1"}, 201)
        if path == "/git/trees":
            trees.append(1)
            return Resp({"message": "tree.sha blob1 is not a valid blob"}, 422) if len(trees) < 3 \
                else Resp({"sha": "tree1"}, 201)
        if path == "/git/commits":
            return Resp({"sha": "c1"}, 201)
        return Resp({}, 201)

    bot = pub.GitHubBot("github_pat_test", "o/r")
    bot.session.request = request
    assert bot.upload("20261021-191129-0c0061e1", {"video.mp4": video}, {"title": "t"}) == "c1"
    assert len(trees) == 3 and slept == [2, 4]


def test_publish_upload_gives_up_after_retries(tmp_path, monkeypatch):
    from shorts import publish as pub

    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
    slept = []
    monkeypatch.setattr(pub.time, "sleep", slept.append)

    def request(method, url, **kw):
        if kw.get("data") is not None:
            while kw["data"].read(1 << 16):
                pass
            return Resp({"sha": "blob1"}, 201)
        return Resp({"message": "tree.sha blob1 is not a valid blob"}, 422)

    bot = pub.GitHubBot("github_pat_test", "o/r")
    bot.session.request = request
    with pytest.raises(pub.PublishError, match="not a valid blob"):
        bot.upload("20261021-191129-0c0061e1", {"video.mp4": video}, {"title": "t"})
    assert slept == list(pub.BLOB_RETRY_DELAYS)


# ---------------------------------------------------------------- 네이버 블로그 글

def test_crop_for_blog_is_4_by_5(tmp_path):
    from shorts.blog import crop_for_blog

    src = tmp_path / "tall.png"
    Image.new("RGB", (1536, 2752), (10, 20, 30)).save(src)
    out = crop_for_blog(src, tmp_path / "out.jpg")
    with Image.open(out) as im:
        assert im.size == (1080, 1350) and im.format == "JPEG"


def test_normalize_blog_assigns_unique_images_and_cleans_tags():
    from shorts.blog import normalize_blog

    data = {"titles": [" 제목 ", ""], "main_keyword": "금리", "sub_keywords": ["대출"], "meta_description": "m",
            "intro": "i", "conclusion": "c", "sources": ["금융감독원 (2026.10.21)", " "],
            "tags": ["#금리 인하", "금리인하", "대출"],
            "faq": [{"question": "Q", "answer": "A"}, {"question": " ", "answer": "x"}],
            "sections": [{"heading": "a", "body": "b", "image": "scene_02.jpg", "experience_hint": ""},
                         {"heading": "c", "body": "d", "image": "scene_02.jpg", "experience_hint": "h"},
                         {"heading": "e", "body": "f", "image": "없는파일.jpg", "experience_hint": ""},
                         {"heading": "", "body": "빈 소제목은 뺌", "image": "", "experience_hint": ""}]}
    blog = normalize_blog(data, ["scene_01.jpg", "scene_02.jpg", "scene_03.jpg"])
    assert blog["titles"] == ["제목"] and blog["tags"] == ["금리인하", "대출"]
    assert [s["image"] for s in blog["sections"]] == ["scene_02.jpg", "scene_01.jpg", "scene_03.jpg"]
    assert len(blog["faq"]) == 1 and blog["sources"] == ["금융감독원 (2026.10.21)"]


def test_save_blog_images_uses_ai_images_and_video_frames(tmp_path):
    from shorts.blog import save_blog_images

    video = tmp_path / "v.mp4"
    run(["-f", "lavfi", "-i", "testsrc2=size=180x320:rate=10", "-t", "4", "-pix_fmt", "yuv420p", str(video)])
    ai = tmp_path / "ai.png"
    Image.new("RGB", (900, 1600), (200, 120, 60)).save(ai)
    names = save_blog_images(video, [{"media": str(ai), "start": 0, "dur": 2},
                                     {"media": None, "start": 2, "dur": 2}], tmp_path / "blog")
    assert names == ["scene_01.jpg", "scene_02.jpg"]
    for n in ("cover.jpg", *names):
        with Image.open(tmp_path / "blog" / n) as im:
            assert im.width == 1080 and abs(im.height / im.width - 1.25) < 0.02
    with Image.open(tmp_path / "blog" / "scene_01.jpg") as im:
        assert im.getpixel((540, 675))[0] > 150  # 나노바나나 그림(주황)을 그대로 썼는지


def test_blog_failure_does_not_stop_video(tmp_path, monkeypatch):
    from shorts.writer import WriterError

    monkeypatch.setattr(daily, "save_blog_images", lambda video, scenes, d: ["scene_01.jpg"])

    def boom(*a):
        raise WriterError("크레딧 부족")

    monkeypatch.setattr(daily, "write_blog", boom)
    res = type("R", (), {"scenes": []})()
    msg = daily.make_blog(tmp_path, tmp_path / "v.mp4", res, {}, "", {"topic": "t"}, "테마", "id", {"title": "t"},
                          "k", "m")
    assert "만들지 못했어요" in msg and not (tmp_path / "blog" / "blog.json").exists()
