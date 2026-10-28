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


def use_tmp_data_dirs(monkeypatch, tmp_path):
    dirs = {}
    for name in ("VIDEO_DIR", "WORK_DIR", "FRAME_DIR"):
        d = tmp_path / name.lower()
        d.mkdir()
        monkeypatch.setattr(config, name, d)
        dirs[name] = d
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "HISTORY_FILE", tmp_path / "history.json")
    return dirs


def test_clear_working_files_keeps_originals(tmp_path, monkeypatch):
    dirs = use_tmp_data_dirs(monkeypatch, tmp_path)
    original = tmp_path / "내 영상 원본.mp4"
    original.write_bytes(b"original")
    (dirs["VIDEO_DIR"] / "copy.mp4").write_bytes(b"copy")
    (dirs["WORK_DIR"] / "meta.mp4").write_bytes(b"meta")
    (dirs["FRAME_DIR"] / "frame_00.jpg").write_bytes(b"jpg")
    assert config.clear_working_files() == 3
    assert all(not any(d.iterdir()) for d in dirs.values())
    assert original.read_bytes() == b"original"


class FakeBot:
    instances = []

    def __init__(self, token, repo):
        self.repo, self.calls = repo, []
        FakeBot.instances.append(self)

    def check(self):
        return {"default_branch": "main", "private": False}

    def upload(self, video_id, files, post, progress_cb=None):
        # 보내는 시점에는 작업용 사본이 아직 있어야 한다
        assert all(p.exists() for p in files.values())
        self.calls.append(("upload", video_id, sorted(files), post["platforms"]))
        return "sha"

    def dispatch(self, video_id, ref):
        self.calls.append(("dispatch", video_id, ref))

    def delete_video_branch(self, video_id):
        self.calls.append(("delete", video_id))

    def find_run(self, video_id, wait_s=90):
        return {"id": 1, "status": "completed", "html_url": "https://github.com/o/r/actions/runs/1"}

    def get_run(self, run_id):
        return {"id": run_id, "status": "completed"}

    def read_record(self, video_id):
        return {"platforms": ["threads"], "posts": {"threads": {"media_id": "1", "permalink": "https://threads/x"}},
                "errors": {}}


def test_upload_deletes_working_copies_and_resets_picker(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    import core.github_bot as github_bot

    dirs = use_tmp_data_dirs(monkeypatch, tmp_path)
    config.save_settings({**config.DEFAULT_SETTINGS, "github_token": "github_pat_x", "github_repo": "o/r",
                          "default_platforms": ["threads"]})
    monkeypatch.setattr(github_bot, "GitHubBot", FakeBot)
    working_copy = make_video(dirs["VIDEO_DIR"] / "clip.mp4")
    (dirs["FRAME_DIR"] / "frame_00.jpg").write_bytes(b"jpg")

    at = AppTest.from_file(str(APP_DIR / "app_pages" / "upload.py"), default_timeout=120)
    at.session_state["video"] = {"file_id": "f1", "key": "clip.mp4:123", "info": media.probe(working_copy)}
    at.session_state["threads_text"] = "스레드 글"
    at.run()
    next(b for b in at.button if b.label == "업로드 시작").click()
    at.run()

    assert not at.exception
    bot = FakeBot.instances[-1]
    assert [c[0] for c in bot.calls] == ["upload", "dispatch"]
    assert all(not any(d.iterdir()) for d in dirs.values()), "작업용 사본이 남아 있으면 안 된다"
    assert at.session_state["video"] is None and at.session_state["uploader_n"] == 1
    assert "clip.mp4:123" in at.session_state["uploaded_keys"]
    assert at.session_state["last_result"]["status"] == "완료"
    assert config.load_history()[0]["status"] == "완료"


def test_upload_retries_when_github_has_not_seen_new_blob(tmp_path, monkeypatch):
    """GitHub가 방금 만든 blob을 잠깐 못 찾으면(422 not a valid blob) 기다렸다가 다시 트리를 만든다."""
    from core import github_bot

    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
    slept, tree_calls = [], []
    monkeypatch.setattr(github_bot.time, "sleep", slept.append)

    def request(method, url, **kw):
        path = url.split("/repos/o/r", 1)[1]
        if "data" in kw:
            while kw["data"].read(1 << 16):
                pass
            return FakeResp({"sha": "blob1"}, 201)
        if path == "/git/trees":
            tree_calls.append(1)
            if len(tree_calls) == 1:
                return FakeResp({"message": "tree.sha blob1 is not a valid blob"}, 422)
            return FakeResp({"sha": "tree1"}, 201)
        if path == "/git/commits":
            return FakeResp({"sha": "commit1"}, 201)
        return FakeResp({}, 201)

    bot = GitHubBot("github_pat_test", "o/r")
    bot.session.request = request
    assert bot.upload("20261021-191129-0c0061e1", {"video.mp4": video}, {"title": "t"}) == "commit1"
    assert len(tree_calls) == 2 and slept == [2]


def test_upload_does_not_retry_other_errors(tmp_path, monkeypatch):
    from core import github_bot

    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(github_bot.time, "sleep", lambda s: pytest.fail("다시 시도하면 안 됨"))

    def request(method, url, **kw):
        if "data" in kw:
            while kw["data"].read(1 << 16):
                pass
            return FakeResp({"sha": "blob1"}, 201)
        return FakeResp({"message": "Validation Failed"}, 422)

    bot = GitHubBot("github_pat_test", "o/r")
    bot.session.request = request
    with pytest.raises(GitHubError, match="Validation Failed"):
        bot.upload("20261021-191129-0c0061e1", {"video.mp4": video}, {"title": "t"})


def test_status_from_record_includes_naver_blog():
    base = {"platforms": ["reels"], "posts": {"reels": {"media_id": "1", "permalink": "https://ig/x"}}, "errors": {}}
    state, results = config.status_from_record({**base, "naver_blog": {"status": "published",
                                                                        "permalink": "https://blog.naver.com/zeusvigor/1"}})
    assert state == "완료" and results["naver_blog"] == "https://blog.naver.com/zeusvigor/1"
    state, results = config.status_from_record({**base, "naver_blog": {"status": "queued"}})
    assert state == "완료" and results["naver_blog"].startswith("Uplika에 전달됨")
    state, results = config.status_from_record({**base, "naver_blog": {"status": "failed", "error": "키 없음"}})
    assert state == "일부 실패" and results["naver_blog"] == "실패: 키 없음"
    only_blog = {"platforms": [], "posts": {}, "errors": {}, "naver_blog": {"status": "failed", "error": "x"}}
    assert config.status_from_record(only_blog)[0] == "실패"


def test_extract_blog_photos(tmp_path):
    info = media.probe(make_video(tmp_path / "v.mp4", size="1080x1920", seconds=4))
    photos = media.extract_blog_photos(info, tmp_path / "work", [0.5, 2.0, 3.5])
    assert [p.name for p in photos] == ["blog_01.jpg", "blog_02.jpg", "blog_03.jpg"]
    from PIL import Image
    with Image.open(photos[0]) as img:
        assert img.size == (1080, 1920)
    previews = media.extract_blog_photos(info, tmp_path / "work", [1.0], prefix="preview", width=360)
    with Image.open(previews[0]) as img:
        assert img.size[0] == 360
    assert media.extract_blog_photos(info, tmp_path / "work", []) == []
    assert media.blog_photo_times(8.0, 4) == [1.0, 3.0, 5.0, 7.0]


def test_blog_candidate_count_grows_with_length():
    assert media.blog_candidate_count(20) == 8 and media.blog_candidate_count(60) == 8
    assert media.blog_candidate_count(120) == 13 and media.blog_candidate_count(600) == 24


class BlogFakeBot(FakeBot):
    def upload(self, video_id, files, post, progress_cb=None):
        self.post, self.files = post, sorted(files)
        return super().upload(video_id, files, post, progress_cb)

    def read_record(self, video_id):
        return {"platforms": [], "posts": {}, "errors": {},
                "naver_blog": {"status": "published", "permalink": "https://blog.naver.com/zeusvigor/9"}}


def test_upload_sends_naver_blog_with_photos_and_video(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    import core.github_bot as github_bot

    dirs = use_tmp_data_dirs(monkeypatch, tmp_path)
    config.save_settings({**config.DEFAULT_SETTINGS, "github_token": "github_pat_x", "github_repo": "o/r",
                          "default_platforms": ["naver_blog"], "naver_photo_count": 4})
    monkeypatch.setattr(github_bot, "GitHubBot", BlogFakeBot)
    working_copy = make_video(dirs["VIDEO_DIR"] / "clip.mp4", seconds=4)

    at = AppTest.from_file(str(APP_DIR / "app_pages" / "upload.py"), default_timeout=120)
    at.session_state["video"] = {"file_id": "f2", "key": "clip.mp4:9", "info": media.probe(working_copy)}
    at.session_state["blog_title"] = "아이들과 축제 나들이"
    at.session_state["blog_body"] = "## 도착\n신나게 놀았어요.\n\n[영상]\n\n[사진 1]\n\n[사진 2]"
    at.session_state["blog_tags"] = "#가족 #축제"
    at.run()
    next(b for b in at.button if b.label == "업로드 시작").click()
    at.run()

    assert not at.exception
    bot = BlogFakeBot.instances[-1]
    assert bot.post["platforms"] == []  # 네이버 블로그는 videopost가 아니라 bot/naver_blog.py가 올린다
    blog = bot.post["naver_blog"]
    assert blog["title"] == "아이들과 축제 나들이" and blog["tags"] == ["가족", "축제"]
    assert blog["category"] == "가족기록" and blog["open_type"] == "public"
    # AI 초안이 아니면 고르게 뽑은 장면 중 본문이 쓰는 사진만 보낸다
    assert blog["photos"] == ["blog_01.jpg", "blog_02.jpg"]
    assert blog["video"] == "video.mp4"
    assert bot.files == ["blog_01.jpg", "blog_02.jpg", "video.mp4"]
    assert at.session_state["last_result"]["results"]["naver_blog"] == "https://blog.naver.com/zeusvigor/9"
    assert all(not any(d.iterdir()) for d in dirs.values()), "블로그 사진 사본도 지워야 한다"


def test_big_files_are_pushed_with_git(tmp_path, monkeypatch):
    """큰 파일은 blob API 대신 git으로 push한다. 결과 모양(videos/<ID>/파일 + post.json, 부모 없는 커밋)은 API와 같다."""
    import shutil as _shutil

    from core import github_bot as gb

    git = _shutil.which("git")
    remote = tmp_path / "remote.git"
    subprocess.run([git, "init", "--bare", "-q", str(remote)], check=True)
    monkeypatch.setattr(gb, "BLOB_API_MAX_BYTES", 10)  # 작은 파일로도 git 경로를 타게
    big = tmp_path / "video.mp4"
    big.write_bytes(b"v" * 5000)
    photo = tmp_path / "blog_01.jpg"
    photo.write_bytes(b"jpg")

    bot = GitHubBot("github_pat_test", "o/r")
    bot.remote_url = lambda: str(remote)
    bot.session.request = lambda *a, **k: (_ for _ in ()).throw(AssertionError("API를 쓰면 안 된다"))
    progress = []
    vid = "20261029-101010-1a2b3c4d"
    sha = bot.upload(vid, {"video.mp4": big, "blog_01.jpg": photo}, {"title": "큰 영상"}, progress.append)

    def git_out(*args):
        return subprocess.run([git, "--git-dir", str(remote), *args], capture_output=True, check=True).stdout

    assert git_out("rev-parse", f"refs/heads/video/{vid}").decode().strip() == sha
    files = git_out("ls-tree", "-r", "--name-only", f"video/{vid}").decode().split()
    assert sorted(files) == [f"videos/{vid}/blog_01.jpg", f"videos/{vid}/post.json", f"videos/{vid}/video.mp4"]
    assert git_out("show", f"video/{vid}:videos/{vid}/video.mp4") == big.read_bytes()
    assert json.loads(git_out("show", f"video/{vid}:videos/{vid}/post.json").decode("utf-8")) == {"title": "큰 영상"}
    assert git_out("rev-list", "--count", f"video/{vid}").decode().strip() == "1"  # 부모 없는 커밋
    assert progress[-1] == 1.0
    # 토큰은 명령줄이 아니라 환경 변수 설정으로만 넘긴다
    env = bot._git_env()
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader" and "github_pat_test" not in env["GIT_CONFIG_VALUE_0"]


def test_big_files_without_git_explain_install(tmp_path, monkeypatch):
    import pytest

    from core import github_bot as gb

    monkeypatch.setattr(gb, "BLOB_API_MAX_BYTES", 10)
    monkeypatch.setattr(gb.shutil, "which", lambda name: None)
    big = tmp_path / "video.mp4"
    big.write_bytes(b"v" * 100)
    with pytest.raises(GitHubError, match="Git이 없습니다"):
        GitHubBot("github_pat_test", "o/r").upload("20261029-101010-1a2b3c4d", {"video.mp4": big}, {})


def test_small_files_still_use_api(tmp_path, monkeypatch):
    from core import github_bot as gb

    calls = []
    bot = GitHubBot("github_pat_test", "o/r")
    monkeypatch.setattr(bot, "_upload_with_git", lambda *a, **k: calls.append("git"))
    small = tmp_path / "v.mp4"
    small.write_bytes(b"v" * 100)
    bot.session.request = lambda m, url, **kw: (calls.append(url.rsplit("/repos/o/r", 1)[1]) or
                                                FakeResp({"sha": "s"}, 201))
    bot.upload("20261029-101010-1a2b3c4d", {"v.mp4": small}, {})
    assert "git" not in calls and "/git/blobs" in calls
