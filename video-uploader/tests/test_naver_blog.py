import json

import pytest

from bot import naver_blog as nb


class FakeUplika:
    def __init__(self, accounts=None, states=("published",), url="https://blog.naver.com/zeusvigor/1"):
        self._accounts = accounts if accounts is not None else [
            {"id": "conn_econ", "platform": "naver_blog", "handle": "economypub", "status": "active"},
            {"id": "conn_super", "platform": "naver_blog", "handle": "zeusvigor", "status": "active"},
            {"id": "conn_th", "platform": "threads", "handle": "zeusvigor", "status": "active"},
        ]
        self.states, self.url = list(states), url
        self.uploads, self.posts, self.retries, self.gets = [], [], [], 0

    def accounts(self):
        return self._accounts

    def upload(self, path):
        self.uploads.append(path.name)
        return f"med_{len(self.uploads)}"

    def create_post(self, body, idempotency_key):
        self.posts.append((body, idempotency_key))
        return {"id": "post_1", "status": "publishing",
                "targets": [{"platform": "naver_blog", "status": "publishing"}]}

    def get_post(self, post_id):
        self.gets += 1
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        target = {"platform": "naver_blog", "status": state}
        if state == "published":
            target["url"] = self.url
        if state == "failed":
            target["error"] = {"code": "naver_tab_lost", "message": "창이 닫혔습니다"}
        return {"id": post_id, "targets": [target]}

    def retry_post(self, post_id):
        self.retries.append(post_id)
        self.states = ["published"]
        return {"id": post_id}


def make_dir(tmp_path, blog=True, body=None):
    d = tmp_path / "videos" / "20261025-101010-1a2b3c4d"
    d.mkdir(parents=True)
    for name in ("blog_01.jpg", "blog_02.jpg", "video.mp4"):
        (d / name).write_bytes(b"x")
    post = {"title": "앗 벌레다!", "platforms": ["reels"]}
    if blog:
        post["naver_blog"] = {
            "title": "아이들과 영산강 축제 나들이",
            "body": body or "## 축제 가는 길\n오늘은 축제에 갔어요.\n\n[영상]\n\n[사진 1]\n\n## 벌레 발견\n[사진 2]\n[사진 3]\n끝!",
            "tags": ["#가족 나들이", "축제", "축제"],
            "category": "가족기록",
            "open_type": "public",
            "photos": ["blog_01.jpg", "blog_02.jpg", "blog_03.jpg"],
            "video": "video.mp4",
        }
    (d / "post.json").write_text(json.dumps(post, ensure_ascii=False), encoding="utf-8")
    return d


def test_publish_goes_to_zeusvigor_with_as_is_markdown(tmp_path):
    d = make_dir(tmp_path)
    rec = tmp_path / "rec.json"
    rec.write_text(json.dumps({"id": d.name, "platforms": ["reels"], "posts": {"reels": {"media_id": "1"}}}),
                   encoding="utf-8")
    fake = FakeUplika()
    entry = nb.publish_blog(d, rec, "uplk_live_x", client=fake, sleep=lambda s: None)

    assert entry["status"] == "published" and entry["permalink"] == "https://blog.naver.com/zeusvigor/1"
    body, idem = fake.posts[0]
    assert body["accountIds"] == ["conn_super"]  # 경제주막이 아니라 슈퍼아빠 블로그
    assert idem == f"video-upload-{d.name}-naver-blog"
    opts = body["options"]["naver_blog"]
    assert opts["title"] == "아이들과 영산강 축제 나들이" and opts["category"] == "가족기록"
    assert opts["layout"] == "as-is" and opts["openType"] == "public"
    assert opts["tags"] == ["가족나들이", "축제"]
    # [영상]·[사진 1]·[사진 2]는 미디어로, 파일이 없는 [사진 3]은 빠진다. 각 파일은 한 번만 올린다
    assert fake.uploads == ["video.mp4", "blog_01.jpg", "blog_02.jpg"]
    assert "@video(media:med_1)" in body["content"] and "![](media:med_2)" in body["content"]
    assert "![](media:med_3)" in body["content"] and "[사진 3]" not in body["content"]
    assert body["content"].startswith("## 축제 가는 길")
    saved = json.loads(rec.read_text(encoding="utf-8"))
    assert saved["posts"]["reels"] == {"media_id": "1"}  # 영상 게시 기록은 그대로
    assert saved["naver_blog"]["post_id"] == "post_1"


def test_waits_while_extension_is_writing(tmp_path):
    d = make_dir(tmp_path)
    fake = FakeUplika(states=("publishing", "publishing", "published"))
    slept = []
    entry = nb.publish_blog(d, tmp_path / "rec.json", "k", client=fake, sleep=slept.append)
    assert entry["status"] == "published" and slept == [10, 10, 10]  # 처음 응답 + 확인 2번이 진행 중


def test_still_pending_after_wait_is_not_failure(tmp_path):
    d = make_dir(tmp_path)
    fake = FakeUplika(states=("queued",))
    entry = nb.publish_blog(d, tmp_path / "rec.json", "k", client=fake, poll_s=30, sleep=lambda s: None)
    assert entry["status"] == "queued" and entry["error"] == ""


def test_refuses_when_zeusvigor_blog_is_not_connected(tmp_path):
    d = make_dir(tmp_path)
    fake = FakeUplika(accounts=[{"id": "conn_econ", "platform": "naver_blog", "handle": "economypub"}])
    entry = nb.publish_blog(d, tmp_path / "rec.json", "k", client=fake, sleep=lambda s: None)
    assert entry["status"] == "failed" and "zeusvigor" in entry["error"] and "economypub" in entry["error"]
    assert fake.posts == [] and fake.uploads == []


def test_missing_key_fails_clearly(tmp_path):
    d = make_dir(tmp_path)
    entry = nb.publish_blog(d, tmp_path / "rec.json", "", client=FakeUplika(), sleep=lambda s: None)
    assert entry["status"] == "failed" and "VIDEO_UPLIKA_API_KEY" in entry["error"]


def test_rerun_does_not_post_twice_and_retries_failed(tmp_path):
    d = make_dir(tmp_path)
    rec = tmp_path / "rec.json"
    fake = FakeUplika(states=("failed",))
    first = nb.publish_blog(d, rec, "k", client=fake, sleep=lambda s: None)
    assert first["status"] == "failed" and "naver_tab_lost" in first["error"]

    again = FakeUplika(states=("failed",))
    second = nb.publish_blog(d, rec, "k", client=again, sleep=lambda s: None)
    assert again.posts == [] and again.retries == ["post_1"] and second["status"] == "published"

    third = FakeUplika()
    nb.publish_blog(d, rec, "k", client=third, sleep=lambda s: None)
    assert third.posts == [] and third.retries == []  # 이미 발행된 글은 상태만 확인


def test_no_blog_in_post_does_nothing(tmp_path):
    d = make_dir(tmp_path, blog=False)
    rec = tmp_path / "rec.json"
    assert nb.publish_blog(d, rec, "k", client=FakeUplika()) is None
    assert not rec.exists()


def test_main_exit_codes_and_summary(tmp_path, monkeypatch):
    d = make_dir(tmp_path)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.delenv("VIDEO_UPLIKA_API_KEY", raising=False)
    assert nb.main([str(d), "--record", str(tmp_path / "rec.json")]) == 1
    assert "❌" in summary.read_text(encoding="utf-8")
    assert nb.main([str(make_dir(tmp_path / "x", blog=False)), "--record", str(tmp_path / "r2.json")]) == 0


class FakeResp:
    def __init__(self, status, data):
        self.status_code, self._d = status, data
        self.content = json.dumps(data).encode()
        self.text = self.content.decode()

    def json(self):
        return self._d


def test_api_errors_are_explained():
    client = nb.Uplika("bad")
    client.s.request = lambda m, url, **kw: FakeResp(401, {"error": {"code": "unauthorized", "message": "no"}})
    with pytest.raises(nb.UplikaError, match="VIDEO_UPLIKA_API_KEY"):
        client.accounts()
    client.s.request = lambda m, url, **kw: FakeResp(
        422, {"error": {"code": "naver_category_unknown", "message": "없는 카테고리"}})
    with pytest.raises(nb.UplikaError, match="naver_category_unknown"):
        client.create_post({}, "k")
    client.s.request = lambda m, url, **kw: FakeResp(200, {"data": [{"id": "a", "platform": "naver_blog"}]})
    assert client.accounts() == [{"id": "a", "platform": "naver_blog"}]
    assert client.s.headers["authorization"] == "Bearer bad"
