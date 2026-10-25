"""블로그 초안 쓰기와 AI가 고른 장면으로 사진 보내기 (가짜 Claude, 가짜 봇 — 실제 호출·게시 없음)."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

import core.github_bot as github_bot
from core import analyzer, config, media
from tests.test_core import APP_DIR, BlogFakeBot, make_video, use_tmp_data_dirs


def test_upload_uses_ai_chosen_blog_scenes(tmp_path, monkeypatch):
    dirs = use_tmp_data_dirs(monkeypatch, tmp_path)
    config.save_settings({**config.DEFAULT_SETTINGS, "github_token": "github_pat_x", "github_repo": "o/r",
                          "default_platforms": ["naver_blog"]})
    monkeypatch.setattr(github_bot, "GitHubBot", BlogFakeBot)
    picked = []
    real = media.extract_blog_photos
    monkeypatch.setattr(media, "extract_blog_photos",
                        lambda info, out, times, **kw: picked.append(list(times)) or real(info, out, times, **kw))
    working_copy = make_video(dirs["VIDEO_DIR"] / "clip.mp4", seconds=4)

    at = AppTest.from_file(str(APP_DIR / "app_pages" / "upload.py"), default_timeout=120)
    at.session_state["video"] = {"file_id": "f3", "key": "clip.mp4:7", "info": media.probe(working_copy)}
    at.session_state["blog_title"] = "제목"
    at.session_state["blog_body"] = "도입\n\n[사진 1]\n\n본문\n\n[사진 2]"
    at.session_state["blog_photo_times"] = [3.2, 0.8]
    at.run()
    next(b for b in at.button if b.label == "업로드 시작").click()
    at.run()

    assert not at.exception
    assert picked[-1] == [3.2, 0.8]
    blog = BlogFakeBot.instances[-1].post["naver_blog"]
    assert blog["photos"] == ["blog_01.jpg", "blog_02.jpg"] and blog["video"] is None


def test_blog_draft_button_fills_blog_and_previews(tmp_path, monkeypatch):
    dirs = use_tmp_data_dirs(monkeypatch, tmp_path)
    config.save_settings({**config.DEFAULT_SETTINGS, "anthropic_api_key": "sk-ant-x",
                          "default_platforms": ["naver_blog"], "naver_web_search": True})
    calls = {}

    def fake_research(key, model, brief):
        calls["brief"] = brief
        return "주제: 영산강 축제\n- 10월 개최 (https://example.com)"

    def fake_write(key, model, frames, duration, brief, transcript, research, include_video):
        calls.update(frames=len(frames), research=research, include_video=include_video)
        return {"title": "영산강 축제 다녀왔어요", "body": "## 도착\n[영상]\n[사진 1]\n## 정보\n[사진 2]",
                "tags": ["영산강축제", "가족나들이"],
                "photos": [{"time": 1.0, "caption": "입구"}, {"time": 3.0, "caption": "벌레"}]}

    monkeypatch.setattr(analyzer, "research_topic", fake_research)
    monkeypatch.setattr(analyzer, "write_blog", fake_write)
    working_copy = make_video(dirs["VIDEO_DIR"] / "clip.mp4", seconds=4)

    at = AppTest.from_file(str(APP_DIR / "app_pages" / "upload.py"), default_timeout=120)
    at.session_state["video"] = {"file_id": "f4", "key": "clip.mp4:5", "info": media.probe(working_copy)}
    at.session_state["yt_title"] = "앗 벌레다!"
    at.session_state["hashtags"] = "#나주영산강축제 #형제일상"
    at.run()
    next(b for b in at.button if b.label == "AI로 블로그 초안 쓰기").click()
    at.run()

    assert not at.exception
    assert "앗 벌레다!" in calls["brief"] and "#나주영산강축제" in calls["brief"]
    assert calls["frames"] == 5 and calls["research"].startswith("주제") and calls["include_video"] is True
    assert at.session_state["blog_title"] == "영산강 축제 다녀왔어요"
    assert at.session_state["blog_tags"] == "#영산강축제 #가족나들이"
    assert at.session_state["blog_photo_times"] == [1.0, 3.0]
    previews = at.session_state["blog_previews"]
    assert [p[2] for p in previews] == ["입구", "벌레"] and all(Path(p[0]).exists() for p in previews)
