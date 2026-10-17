import base64
import json
import subprocess
from pathlib import Path

import imageio_ffmpeg
import pytest

from core import config, media
from core.github_bot import GitHubBot, GitHubError, new_video_id

APP_DIR = Path(__file__).resolve().parent.parent


class FakeResp:
    def __init__(self, data=None, status=200, content=None):
        self._d, self.status_code = data, status
        self.content = content if content is not None else json.dumps(data or {}).encode()
        self.text = self.content.decode("utf-8", "replace")

    def json(self):
        return self._d


def make_video(path, size="1080x1920", seconds=4, rate=30):
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:v", "libx264", "-b:v", "8M", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                   check=True)
    return path


def test_new_video_id_matches_workflow_pattern():
    import re
    assert re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{8}", new_video_id())


def test_upload_creates_single_orphan_commit_on_video_branch(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00\x01video-bytes")
    calls = []

    def request(method, url, **kw):
        path = url.split("/repos/o/r", 1)[1]
        body = kw.get("json")
        if "data" in kw:  # 블롭은 진행률을 위해 바이트 스트림으로 보낸다
            stream = kw["data"]
            chunks = []
            while chunk := stream.read(7):
                chunks.append(chunk)
            body = json.loads(b"".join(chunks))
        calls.append((method, path, body))
        if path == "/git/blobs":
            return FakeResp({"sha": f"blob{len(calls)}"}, 201)
        if path == "/git/trees":
            return FakeResp({"sha": "tree1"}, 201)
        if path == "/git/commits":
            return FakeResp({"sha": "commit1"}, 201)
        return FakeResp({}, 201)

    bot = GitHubBot("github_pat_test", "o/r")
    bot.session.request = request
    progress = []
    sha = bot.upload("20261017-153000-1a2b3c4d", {"video.mp4": video}, {"title": "제목"}, progress.append)

    assert sha == "commit1" and progress[-1] == 1.0
    blob_video, blob_post = calls[0][2], calls[1][2]
    assert base64.b64decode(blob_video["content"]) == video.read_bytes()
    assert json.loads(base64.b64decode(blob_post["content"]).decode("utf-8")) == {"title": "제목"}
    tree = calls[2][2]["tree"]
    assert [e["path"] for e in tree] == ["videos/20261017-153000-1a2b3c4d/video.mp4",
                                         "videos/20261017-153000-1a2b3c4d/post.json"]
    assert calls[3][2] == {"message": "video: 20261017-153000-1a2b3c4d", "tree": "tree1", "parents": []}
    assert calls[4] == ("POST", "/git/refs", {"ref": "refs/heads/video/20261017-153000-1a2b3c4d", "sha": "commit1"})


def test_check_explains_missing_workflow_and_bad_token():
    bot = GitHubBot("github_pat_test", "o/r")
    bot.session.request = lambda m, url, **kw: (
        FakeResp({"default_branch": "main"}) if url.endswith("/repos/o/r") else FakeResp({"message": "Not Found"}, 404))
    with pytest.raises(GitHubError, match="병합"):
        bot.check()
    bot.session.request = lambda m, url, **kw: FakeResp({"message": "Bad credentials"}, 401)
    with pytest.raises(GitHubError, match="토큰"):
        bot.check()


def test_read_record_and_find_run(monkeypatch):
    record = {"platforms": ["reels"], "posts": {"reels": {"media_id": "1", "permalink": "https://ig/x"}}}
    bot = GitHubBot("github_pat_test", "o/r")

    def request(method, url, **kw):
        if "/contents/records/" in url:
            if "missing" in url:
                return FakeResp({"message": "Not Found"}, 404)
            assert kw["params"] == {"ref": "video-records"}
            return FakeResp(content=json.dumps(record, ensure_ascii=False).encode("utf-8"))
        return FakeResp({"workflow_runs": [{"id": 5, "display_title": "영상 업로드 ID-1", "html_url": "u"}]})

    bot.session.request = request
    assert bot.read_record("ID-1") == record
    assert bot.read_record("missing") is None
    assert bot.find_run("ID-1")["id"] == 5


def test_status_from_record():
    rec = {"platforms": ["reels", "threads"], "posts": {"reels": {"media_id": "1", "permalink": "https://ig/x"}},
           "errors": {"threads": "토큰 만료"}}
    assert config.status_from_record(rec) == ("일부 실패", {"reels": "https://ig/x", "threads": "실패: 토큰 만료"})
    rec["posts"]["threads"] = {"media_id": "2", "permalink": ""}
    assert config.status_from_record(rec)[0] == "완료"


def test_prepare_small_mp4_uses_original_once(tmp_path):
    info = media.probe(make_video(tmp_path / "small.mp4"))
    files = media.prepare_upload_files(info, ["reels", "threads", "youtube"], tmp_path / "work")
    assert files == {"youtube": info.path, "meta": info.path}


def test_prepare_large_video_compresses_meta_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "META_MAX_BYTES", 1_500_000)
    info = media.probe(make_video(tmp_path / "big.mp4", seconds=6))
    assert info.size_bytes > media.META_MAX_BYTES
    progress = []
    files = media.prepare_upload_files(info, ["reels", "youtube"], tmp_path / "work",
                                       lambda label, p: progress.append(p))
    assert files["youtube"] == info.path
    assert files["meta"].name == "meta.mp4" and files["meta"].stat().st_size <= media.META_MAX_BYTES
    assert progress and progress[-1] == 1.0
    out = media.probe(files["meta"])
    assert (out.width, out.height) == (1080, 1920) and abs(out.duration - info.duration) < 0.5


def test_prepare_mov_is_converted_for_meta_only(tmp_path):
    info = media.probe(make_video(tmp_path / "clip.mov", size="720x1280"))
    files = media.prepare_upload_files(info, ["threads"], tmp_path / "work")
    assert set(files) == {"meta"} and files["meta"].suffix == ".mp4"


def test_token_problem_recognizes_wrong_tokens():
    from core.github_bot import repo_problem, token_problem

    assert token_problem("github_pat_11ABC") is None
    assert "인스타그램" in token_problem("IGAAxxxxxxxx")
    assert "스레드" in token_problem("THAAxxxxxxxx")
    assert "VIDEO_YOUTUBE_REFRESH_TOKEN" in token_problem("1//0abc")
    assert "classic" in token_problem("ghp_abc")
    assert "github_pat_" in token_problem("random")
    assert token_problem("  ") == "GitHub 토큰을 입력해 주세요."
    assert repo_problem("superabba/my-site") is None and repo_problem("my-site")
    with pytest.raises(GitHubError, match="인스타그램"):
        GitHubBot("IGAAxxxx", "o/r")


def run_settings_page(tmp_path, monkeypatch, token):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    config.save_settings({**config.DEFAULT_SETTINGS, "github_token": "github_pat_OLD"})
    at = AppTest.from_file(str(APP_DIR / "app_pages" / "settings.py"), default_timeout=60).run()
    field = next(w for w in at.text_input if w.label == "GitHub 토큰")
    field.set_value(token)
    next(b for b in at.button if b.label == "저장하고 연결 확인").click()
    return at.run()


def test_settings_refuses_non_github_token(tmp_path, monkeypatch):
    at = run_settings_page(tmp_path, monkeypatch, "IGAAinstagramtoken")
    assert any("저장하지 않았습니다" in e.value and "인스타그램" in e.value for e in at.error)
    assert config.load_settings()["github_token"] == "github_pat_OLD"  # 기존 토큰이 그대로 남는다


def test_settings_warns_about_saved_wrong_token(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    config.save_settings({**config.DEFAULT_SETTINGS, "github_token": "IGAAsavedbymistake"})
    at = AppTest.from_file(str(APP_DIR / "app_pages" / "settings.py"), default_timeout=60).run()
    assert any("저장된 GitHub 토큰이 올바르지 않습니다" in w.value for w in at.warning)
