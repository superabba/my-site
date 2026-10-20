import json
from datetime import datetime, timedelta, timezone

import pytest

from cardbot import shortspost
from cardbot.publishers import PublishError, PublishResult
from cardbot.shortspost import main, publish_shorts, recent_posts, shorts_settings

NOW = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)


class FakePub:
    def __init__(self, name, fail=False):
        self.name, self.fail, self.calls = name, fail, []

    def publish(self, assets, text):
        self.calls.append((assets, text))
        if self.fail:
            raise PublishError("일부러 실패")
        return PublishResult(self.name, f"{self.name}-1", f"https://example.com/{self.name}")


def make_video_dir(tmp_path, platforms=("reels", "threads", "youtube"), vid="20261020-150000-1a2b3c4d", **extra):
    d = tmp_path / "video" / "videos" / vid
    d.mkdir(parents=True)
    (d / "video.mp4").write_bytes(b"v" * 20)
    (d / "post.json").write_text(json.dumps({
        "title": "금리 인하, 내 대출 이자는?",
        "youtube_description": "기준금리가 내리면 대출 이자가 어떻게 바뀌는지 정리했어요.",
        "instagram_caption": "금리 인하 총정리",
        "threads_text": "금리 내리면 내 대출은?",
        "hashtags": ["경제", "금리"],
        "platforms": list(platforms),
        "youtube_privacy": "public",
        "files": {"meta": "video.mp4", "youtube": "video.mp4"},
        **extra,
    }, ensure_ascii=False), encoding="utf-8")
    return d


@pytest.fixture
def env(monkeypatch, tmp_path):
    for name in ("IG_USER_ID", "IG_ACCESS_TOKEN", "THREADS_USER_ID", "THREADS_ACCESS_TOKEN",
                 "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN", "YOUTUBE_CHANNEL_ID",
                 "YOUTUBE_CATEGORY", "SHORTS_YOUTUBE_CLIENT_ID", "SHORTS_YOUTUBE_CLIENT_SECRET",
                 "SHORTS_YOUTUBE_REFRESH_TOKEN", "SHORTS_YOUTUBE_CATEGORY", "SHORTS_ACCOUNT", "SHORTS_DAILY_MAX",
                 "SHORTS_RESERVE", "VIDEO_IG_USER_ID", "VIDEO_IG_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CARDBOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CARDBOT_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setattr("cardbot.config.load_dotenv", lambda path: None)
    # 기본: 계정은 economypub, 게시 한도는 넉넉함
    monkeypatch.setattr(shortspost, "account_username", lambda name, s: "economypub")
    monkeypatch.setattr(shortspost, "remaining_quota", lambda name, s: 90)
    return monkeypatch


def fake_build(fail=()):
    pubs = {}

    def build(name, s, post):
        pubs[name] = FakePub(name, fail=name in fail)
        return pubs[name]

    return build, pubs


def run(tmp_path, d, build, records=None, now=NOW):
    rec_path = tmp_path / "records" / f"{d.name}.json"
    return publish_shorts(d, "o/r", "abc", rec_path, records, shorts_settings(), now, build)


def test_uses_cardnews_account_and_optional_youtube_app(env):
    env.setenv("IG_USER_ID", "card-ig")
    env.setenv("YOUTUBE_CLIENT_ID", "card-client")
    env.setenv("YOUTUBE_CLIENT_SECRET", "card-secret")
    env.setenv("YOUTUBE_REFRESH_TOKEN", "card-rt")
    env.setenv("VIDEO_IG_USER_ID", "video-ig")  # 영상 업로더 계정은 쓰지 않는다
    s = shorts_settings()
    assert s.ig_user_id == "card-ig"
    assert (s.youtube_client_id, s.youtube_refresh_token) == ("card-client", "card-rt")

    env.setenv("SHORTS_YOUTUBE_REFRESH_TOKEN", "own-rt")
    env.setenv("SHORTS_YOUTUBE_CLIENT_ID", "own-client")
    s = shorts_settings()
    assert (s.youtube_client_id, s.youtube_client_secret, s.youtube_refresh_token) == \
           ("own-client", "card-secret", "own-rt")


def test_publishes_and_stamps_record(env, tmp_path):
    d = make_video_dir(tmp_path)
    build, pubs = fake_build()
    rec = run(tmp_path, d, build)
    assert set(rec["posts"]) == {"reels", "threads", "youtube"} and rec["errors"] == {}
    assert rec["account"] == "economypub"
    saved = json.loads((tmp_path / "records" / f"{d.name}.json").read_text(encoding="utf-8"))
    assert saved["posts"]["reels"]["published_at"] == NOW.isoformat(timespec="seconds")
    assert pubs["reels"].calls[0][1].endswith("#경제 #금리")


def test_wrong_account_is_never_posted(env, tmp_path):
    env.setattr(shortspost, "account_username", lambda name, s: "someone_else")
    d = make_video_dir(tmp_path, platforms=("reels", "threads", "youtube"))
    build, pubs = fake_build()
    rec = run(tmp_path, d, build)
    assert pubs["reels"].calls == [] and pubs["threads"].calls == []
    assert "@someone_else" in rec["errors"]["reels"] and "economypub" in rec["errors"]["threads"]
    assert "youtube" in rec["posts"]  # 유튜브는 채널 ID로 따로 확인


def test_account_name_can_be_changed(env, tmp_path):
    env.setattr(shortspost, "account_username", lambda name, s: "Economy.Pub")
    env.setenv("SHORTS_ACCOUNT", "@economy.pub")
    d = make_video_dir(tmp_path, platforms=("reels",))
    build, _ = fake_build()
    assert "reels" in run(tmp_path, d, build)["posts"]


def test_keeps_quota_for_cardnews(env, tmp_path):
    env.setattr(shortspost, "remaining_quota", lambda name, s: 10 if name == "reels" else None)
    d = make_video_dir(tmp_path, platforms=("reels", "threads"))
    build, pubs = fake_build()
    rec = run(tmp_path, d, build)
    assert pubs["reels"].calls == [] and "카드뉴스 몫" in rec["errors"]["reels"]
    assert "threads" in rec["posts"]  # 한도를 모르면 막지 않는다


def test_daily_max_counts_recent_shorts(env, tmp_path):
    records = tmp_path / "records"
    records.mkdir()
    for i, hours_ago in enumerate((1, 5, 30)):
        stamp = (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
        (records / f"old{i}.json").write_text(json.dumps(
            {"posts": {"youtube": {"media_id": "x", "published_at": stamp}}}), encoding="utf-8")
    assert recent_posts(records, "youtube", NOW) == 2
    env.setenv("SHORTS_DAILY_MAX", "2")
    d = make_video_dir(tmp_path, platforms=("youtube", "reels"))
    build, pubs = fake_build()
    rec = run(tmp_path, d, build, records)
    assert "youtube" not in pubs and "SHORTS_DAILY_MAX=2" in rec["errors"]["youtube"]
    assert "reels" in rec["posts"]


def test_retry_skips_done_platforms(env, tmp_path):
    d = make_video_dir(tmp_path, platforms=("reels", "threads"))
    build, _ = fake_build(fail=("threads",))
    first = run(tmp_path, d, build)
    assert "threads" in first["errors"]
    build2, pubs2 = fake_build()
    again = run(tmp_path, d, build2, now=NOW + timedelta(minutes=5))
    assert "reels" not in pubs2 and again["errors"] == {}
    assert again["posts"]["reels"]["published_at"] == NOW.isoformat(timespec="seconds")


def test_youtube_gets_post_privacy_and_synthetic(env, tmp_path):
    for k, v in {"YOUTUBE_CLIENT_ID": "cid", "YOUTUBE_CLIENT_SECRET": "sec", "YOUTUBE_REFRESH_TOKEN": "rt",
                 "YOUTUBE_CHANNEL_ID": "UC_ECON", "SHORTS_YOUTUBE_CATEGORY": "25"}.items():
        env.setenv(k, v)
    built = {}
    env.setattr("cardbot.youtube.YouTubePublisher", lambda *a, **k: built.setdefault("yt", a) and FakePub("youtube"))
    d = make_video_dir(tmp_path, platforms=("youtube",), youtube_privacy="unlisted", youtube_synthetic=True)
    rec = run(tmp_path, d, shortspost.make_publisher)
    assert "youtube" in rec["posts"]
    assert built["yt"] == ("cid", "sec", "rt", "UC_ECON", "unlisted", "25", True)


def test_missing_secrets_fail_only_that_platform(env, tmp_path):
    env.setenv("IG_USER_ID", "card-ig")
    env.setenv("IG_ACCESS_TOKEN", "card-token")
    env.setattr(shortspost, "ReelsPublisher", lambda *a, **k: FakePub("reels"))
    d = make_video_dir(tmp_path, platforms=("reels", "threads"))
    rec = run(tmp_path, d, shortspost.make_publisher)
    assert "reels" in rec["posts"] and "THREADS_USER_ID" in rec["errors"]["threads"]


def test_does_not_touch_cardnews_history(env, tmp_path):
    env.setattr(shortspost, "make_publisher", lambda name, s, post: FakePub(name))
    d = make_video_dir(tmp_path, platforms=("youtube",))
    rec_path = tmp_path / "rec" / "x.json"
    code = main([str(d), "--repo", "o/r", "--sha", "abc", "--record", str(rec_path),
                 "--records-dir", str(tmp_path / "rec")])
    assert code == 0
    assert not (tmp_path / "data" / "cardbot.db").exists()


def test_remaining_quota_parses_and_tolerates_errors(env, monkeypatch):
    class Api:
        def __init__(self, data=None, err=None):
            self.data, self.err = data, err

        def get(self, path, **params):
            if self.err:
                raise self.err
            return self.data

    monkeypatch.undo()  # 실제 remaining_quota를 쓴다
    from cardbot.config import Settings

    s = Settings()
    monkeypatch.setattr(shortspost, "_graph", lambda name, s: (
        Api({"data": [{"config": {"quota_total": 50, "quota_duration": 86400}, "quota_usage": 3}]}), "1"))
    assert shortspost.remaining_quota("reels", s) == 47
    monkeypatch.setattr(shortspost, "_graph", lambda name, s: (Api(err=PublishError("400 권한 없음")), "1"))
    assert shortspost.remaining_quota("threads", s) is None


def test_daily_fields_are_recorded(env, tmp_path):
    d = make_video_dir(tmp_path, platforms=("youtube",), daily=True, topic="금리 동결", keyword="기준금리",
                       theme="경제 뉴스 쉽게 풀기")
    build, _ = fake_build()
    rec = run(tmp_path, d, build)
    assert rec["daily"] is True and rec["topic"] == "금리 동결" and rec["theme"] == "경제 뉴스 쉽게 풀기"
    saved = json.loads((tmp_path / "records" / f"{d.name}.json").read_text(encoding="utf-8"))
    assert saved["keyword"] == "기준금리"
