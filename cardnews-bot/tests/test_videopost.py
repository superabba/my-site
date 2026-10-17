import json

import pytest

from cardbot.config import Settings
from cardbot.publishers import PublishError, PublishResult
from cardbot.videopost import (
    JSDELIVR_MAX_BYTES,
    ThreadsVideoPublisher,
    main,
    publish_video,
    video_urls,
    with_hashtags,
)


class FakeResp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, json.dumps(data)

    def json(self):
        return self._d


class FakePub:
    def __init__(self, name, fail=False):
        self.name, self.fail, self.calls = name, fail, []

    def publish(self, assets, text):
        self.calls.append((assets, text))
        if self.fail:
            raise PublishError("일부러 실패")
        return PublishResult(self.name, f"{self.name}-1", f"https://example.com/{self.name}")


def make_video_dir(tmp_path, platforms=("reels", "threads", "youtube"), meta_size=10):
    d = tmp_path / "video" / "videos" / "20261017-153000-1a2b3c4d"
    d.mkdir(parents=True)
    (d / "meta.mp4").write_bytes(b"m" * meta_size)
    (d / "video.mp4").write_bytes(b"v" * 20)
    (d / "post.json").write_text(json.dumps({
        "title": "성수동 카페 투어",
        "youtube_description": "새로 생긴 카페 세 곳을 다녀왔어요.",
        "instagram_caption": "성수동 카페 투어 ☕",
        "threads_text": "성수동 카페 어디가 제일 좋았냐면",
        "hashtags": ["#카페", "성수동"],
        "platforms": list(platforms),
        "youtube_privacy": "unlisted",
        "files": {"meta": "meta.mp4", "youtube": "video.mp4"},
    }, ensure_ascii=False), encoding="utf-8")
    return d


def test_video_urls_skip_jsdelivr_over_20mb():
    small = video_urls("o/r", "abc", "videos/x/meta.mp4", 1000)
    assert small == ["https://cdn.jsdelivr.net/gh/o/r@abc/videos/x/meta.mp4",
                     "https://raw.githubusercontent.com/o/r/abc/videos/x/meta.mp4"]
    assert video_urls("o/r", "abc", "videos/x/meta.mp4", JSDELIVR_MAX_BYTES + 1) == [small[1]]


def test_with_hashtags_limits_count():
    assert with_hashtags("본문", ["a", "#b"]) == "본문\n\n#a #b"
    assert with_hashtags("본문", [str(i) for i in range(40)], 30).count("#") == 30


def test_publish_video_texts_assets_and_record(tmp_path):
    d = make_video_dir(tmp_path)
    record_path = tmp_path / "records" / "records" / f"{d.name}.json"
    pubs = {n: FakePub(n) for n in ("reels", "threads", "youtube")}
    record = publish_video(d, "o/r", "abc", record_path, Settings(), pubs)

    assert set(record["posts"]) == {"reels", "threads", "youtube"} and record["errors"] == {}
    saved = json.loads(record_path.read_text(encoding="utf-8"))
    assert saved["posts"]["youtube"]["permalink"] == "https://example.com/youtube"

    assets, caption = pubs["reels"].calls[0]
    assert assets["video_urls"][0] == f"https://cdn.jsdelivr.net/gh/o/r@abc/videos/{d.name}/meta.mp4"
    assert caption == "성수동 카페 투어 ☕\n\n#카페 #성수동"
    assert pubs["threads"].calls[0][1] == "성수동 카페 어디가 제일 좋았냐면"
    yt_assets, yt_desc = pubs["youtube"].calls[0]
    assert yt_assets["video_path"].endswith("video.mp4") and yt_assets["title"] == "성수동 카페 투어"
    assert yt_assets["hashtags"] == ["카페", "성수동"] and yt_desc.endswith("#카페 #성수동")


def test_publish_video_retry_skips_done_platforms(tmp_path):
    d = make_video_dir(tmp_path)
    record_path = tmp_path / "rec.json"
    first = {"reels": FakePub("reels"), "threads": FakePub("threads", fail=True), "youtube": FakePub("youtube")}
    rec = publish_video(d, "o/r", "abc", record_path, Settings(), first)
    assert set(rec["posts"]) == {"reels", "youtube"} and "threads" in rec["errors"]

    again = {n: FakePub(n) for n in ("reels", "threads", "youtube")}
    rec = publish_video(d, "o/r", "abc", record_path, Settings(), again)
    assert again["reels"].calls == [] and again["youtube"].calls == []
    assert len(again["threads"].calls) == 1 and rec["errors"] == {}


def test_publish_video_only_selected_platforms_and_unknown(tmp_path):
    d = make_video_dir(tmp_path, platforms=("youtube", "tiktok"))
    pubs = {n: FakePub(n) for n in ("reels", "threads", "youtube")}
    rec = publish_video(d, "o/r", "abc", tmp_path / "rec.json", Settings(), pubs)
    assert pubs["reels"].calls == [] and pubs["threads"].calls == []
    assert "youtube" in rec["posts"] and "tiktok" in rec["errors"]


def test_missing_credentials_fail_only_that_platform(tmp_path, monkeypatch):
    d = make_video_dir(tmp_path, platforms=("threads", "youtube"))
    rec = publish_video(d, "o/r", "abc", tmp_path / "rec.json", Settings(), {"youtube": FakePub("youtube")})
    assert "VIDEO_THREADS_USER_ID" in rec["errors"]["threads"] and "youtube" in rec["posts"]


def isolated_env(monkeypatch, tmp_path, **env):
    for name in ("IG_USER_ID", "IG_ACCESS_TOKEN", "THREADS_USER_ID", "THREADS_ACCESS_TOKEN",
                 "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN", "YOUTUBE_CHANNEL_ID",
                 "VIDEO_IG_USER_ID", "VIDEO_IG_ACCESS_TOKEN", "VIDEO_THREADS_USER_ID", "VIDEO_THREADS_ACCESS_TOKEN",
                 "VIDEO_YOUTUBE_REFRESH_TOKEN", "VIDEO_YOUTUBE_CLIENT_ID", "VIDEO_YOUTUBE_CLIENT_SECRET",
                 "VIDEO_YOUTUBE_CHANNEL_ID", "VIDEO_YOUTUBE_CATEGORY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CARDBOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CARDBOT_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setattr("cardbot.config.load_dotenv", lambda path: None)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_video_settings_use_only_video_accounts(monkeypatch, tmp_path):
    from cardbot.videopost import video_settings

    isolated_env(monkeypatch, tmp_path,
                 IG_USER_ID="card-ig", IG_ACCESS_TOKEN="card-ig-token",
                 YOUTUBE_CLIENT_ID="shared-client", YOUTUBE_CLIENT_SECRET="shared-secret",
                 YOUTUBE_REFRESH_TOKEN="card-channel", YOUTUBE_CHANNEL_ID="UC_CARD",
                 VIDEO_THREADS_USER_ID="video-th", VIDEO_THREADS_ACCESS_TOKEN="video-th-token",
                 VIDEO_YOUTUBE_REFRESH_TOKEN="video-channel", VIDEO_YOUTUBE_CHANNEL_ID="UC_VIDEO")
    s = video_settings()
    # 카드뉴스 인스타 계정으로 대신 채우지 않는다
    assert (s.ig_user_id, s.ig_access_token) == ("", "")
    assert (s.threads_user_id, s.threads_access_token) == ("video-th", "video-th-token")
    assert (s.youtube_refresh_token, s.youtube_channel_id) == ("video-channel", "UC_VIDEO")
    # OAuth 클라이언트(앱)는 카드뉴스 것을 같이 쓴다
    assert (s.youtube_client_id, s.youtube_client_secret) == ("shared-client", "shared-secret")
    assert s.youtube_category == "22"


def test_missing_video_account_never_posts_to_cardnews_account(tmp_path, monkeypatch):
    isolated_env(monkeypatch, tmp_path, IG_USER_ID="card-ig", IG_ACCESS_TOKEN="card-ig-token")
    built = []
    monkeypatch.setattr("cardbot.videopost.ReelsPublisher", lambda *a, **k: built.append(a) or FakePub("reels"))
    d = make_video_dir(tmp_path, platforms=("reels",))
    code = main([str(d), "--repo", "o/r", "--sha", "abc", "--record", str(tmp_path / "rec.json")])
    rec = json.loads((tmp_path / "rec.json").read_text(encoding="utf-8"))
    assert code == 1 and built == []
    assert "VIDEO_IG_USER_ID" in rec["errors"]["reels"]


def test_video_accounts_are_passed_to_publishers(tmp_path, monkeypatch):
    isolated_env(monkeypatch, tmp_path,
                 VIDEO_IG_USER_ID="v-ig", VIDEO_IG_ACCESS_TOKEN="v-ig-token",
                 YOUTUBE_CLIENT_ID="cid", YOUTUBE_CLIENT_SECRET="sec",
                 VIDEO_YOUTUBE_REFRESH_TOKEN="v-rt", VIDEO_YOUTUBE_CHANNEL_ID="UC_VIDEO")
    built = {}
    monkeypatch.setattr("cardbot.videopost.ReelsPublisher",
                        lambda *a, **k: built.setdefault("reels", a) and FakePub("reels"))
    monkeypatch.setattr("cardbot.youtube.YouTubePublisher",
                        lambda *a, **k: built.setdefault("youtube", a) and FakePub("youtube"))
    d = make_video_dir(tmp_path, platforms=("reels", "youtube"))
    assert main([str(d), "--repo", "o/r", "--sha", "abc", "--record", str(tmp_path / "rec.json")]) == 0
    assert built["reels"][:2] == ("v-ig", "v-ig-token")
    assert built["youtube"][:5] == ("cid", "sec", "v-rt", "UC_VIDEO", "unlisted")
    assert built["youtube"][5] == "22"


def test_videopost_does_not_touch_cardnews_history(tmp_path, monkeypatch):
    """영상 게시는 카드뉴스 이력 DB를 쓰지 않아 카드뉴스 자동 발행('오늘 이미 게시함')에 영향이 없다."""
    monkeypatch.setenv("CARDBOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CARDBOT_OUTPUT_DIR", str(tmp_path / "output"))
    d = make_video_dir(tmp_path, platforms=("youtube",))
    monkeypatch.setattr("cardbot.videopost.make_publisher", lambda name, s, post: FakePub(name))
    code = main([str(d), "--repo", "o/r", "--sha", "abc", "--record", str(tmp_path / "rec.json")])
    assert code == 0
    assert not (tmp_path / "data" / "cardbot.db").exists()


def test_main_exit_code_and_summary_on_failure(tmp_path, monkeypatch):
    d = make_video_dir(tmp_path, platforms=("reels",))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("CARDBOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CARDBOT_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setattr("cardbot.videopost.make_publisher", lambda name, s, post: FakePub(name, fail=True))
    assert main([str(d), "--repo", "o/r", "--sha", "abc", "--record", str(tmp_path / "rec.json")]) == 1
    assert "❌ 일부러 실패" in summary.read_text(encoding="utf-8")


def test_threads_video_flow_and_url_fallback(monkeypatch):
    posts = []
    status = {"C1": "ERROR", "C2": "FINISHED"}

    def post(url, data, timeout):
        posts.append((url.split("/v1.0/")[1], dict(data)))
        if url.endswith("/threads_publish"):
            return FakeResp({"id": "TH1"})
        return FakeResp({"id": f"C{len([p for p in posts if 'video_url' in p[1]])}"})

    def get(url, params, timeout):
        cid = url.rsplit("/", 1)[1]
        if params.get("fields") == "permalink":
            return FakeResp({"permalink": "https://threads.net/@me/post/x"})
        return FakeResp({"status": status.get(cid, "FINISHED")})

    monkeypatch.setattr("cardbot.publishers.requests.post", post)
    monkeypatch.setattr("cardbot.publishers.requests.get", get)
    pub = ThreadsVideoPublisher("9", "TOK", "https://graph.threads.net/v1.0", sleep=lambda s: None)
    res = pub.publish({"video_urls": ["https://cdn/a.mp4", "https://raw/a.mp4"]}, "글" * 600)
    assert res.media_id == "TH1" and res.permalink.endswith("/post/x")
    creates = [p for p in posts if "video_url" in p[1]]
    assert [p[1]["video_url"] for p in creates] == ["https://cdn/a.mp4", "https://raw/a.mp4"]
    assert creates[0][0] == "9/threads" and creates[0][1]["media_type"] == "VIDEO"
    assert len(creates[0][1]["text"]) == 500
    assert posts[-1][0] == "9/threads_publish" and posts[-1][1]["creation_id"] == "C2"


def test_threads_video_all_urls_fail(monkeypatch):
    monkeypatch.setattr("cardbot.publishers.requests.post", lambda url, data, timeout: FakeResp({"id": "C1"}))
    monkeypatch.setattr("cardbot.publishers.requests.get",
                        lambda url, params, timeout: FakeResp({"status": "ERROR"}))
    pub = ThreadsVideoPublisher("9", "TOK", "https://graph.threads.net/v1.0", sleep=lambda s: None)
    with pytest.raises(PublishError, match="가져오지 못했습니다"):
        pub.publish({"video_urls": ["https://cdn/a.mp4"]}, "")
