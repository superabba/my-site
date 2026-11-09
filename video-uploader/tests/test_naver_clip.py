import json

from bot import naver_clip as nc

CATEGORIES = [{"id": "126", "name": "라이프스타일 › 가족"}, {"id": "171", "name": "일상기록 › 사진"}]


class FakeUplika:
    def __init__(self, accounts=None, states=("published",), url="https://clip.naver.com/s/1"):
        self._accounts = accounts if accounts is not None else [
            {"id": "conn_other", "platform": "naver_clip", "handle": "other", "displayName": "다른 계정",
             "bridgeBlogId": "economypub", "status": "active", "naverClip": {"categories": CATEGORIES}},
            {"id": "conn_clip", "platform": "naver_clip", "handle": "s9zxoo1z4v", "displayName": "스노우볼23",
             "bridgeBlogId": "zeusvigor", "status": "active", "naverClip": {"categories": CATEGORIES}},
        ]
        self.states, self.url = list(states), url
        self.uploads, self.posts, self.retries = [], [], []

    def accounts(self):
        return self._accounts

    def upload(self, path):
        self.uploads.append(path.name)
        return "med_1"

    def create_post(self, body, idempotency_key):
        self.posts.append((body, idempotency_key))
        return {"id": "post_1", "status": "publishing", "targets": [{"platform": "naver_clip", "status": "publishing"}]}

    def get_post(self, post_id):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        target = {"platform": "naver_clip", "status": state}
        if state == "published":
            target["url"] = self.url
        if state == "failed":
            target["error"] = {"code": "naver_clip_profile_missing", "message": "클립 프로필이 없습니다"}
        return {"id": post_id, "targets": [target]}

    def retry_post(self, post_id):
        self.retries.append(post_id)
        self.states = ["published"]
        return {"id": post_id}


def make_dir(tmp_path, clip=True):
    d = tmp_path / "videos" / "20261109-101010-1a2b3c4d"
    d.mkdir(parents=True)
    (d / "video.mp4").write_bytes(b"x")
    post = {"title": "앗 벌레다!", "platforms": ["reels"], "hashtags": ["형제일상", "축제"]}
    if clip:
        post["naver_clip"] = {"description": "아이들과 축제 #나들이 다녀왔어요", "tags": ["형제 일상", "축제"],
                              "category": "라이프스타일 › 가족", "visibility": "public", "video": "video.mp4"}
    (d / "post.json").write_text(json.dumps(post, ensure_ascii=False), encoding="utf-8")
    return d


def test_clip_goes_to_the_account_of_the_zeusvigor_login(tmp_path):
    d = make_dir(tmp_path)
    rec = tmp_path / "rec.json"
    rec.write_text(json.dumps({"id": d.name, "posts": {"reels": {"media_id": "1"}}}), encoding="utf-8")
    fake = FakeUplika()
    entry = nc.publish_clip(d, rec, "k", client=fake, sleep=lambda s: None)

    assert entry["status"] == "published" and entry["permalink"] == "https://clip.naver.com/s/1"
    body, idem = fake.posts[0]
    assert body["accountIds"] == ["conn_clip"]  # 다른 로그인의 클립 계정이 아니라 슈퍼아빠 로그인
    assert idem == f"video-upload-{d.name}-naver-clip"
    assert body["mediaIds"] == ["med_1"] and fake.uploads == ["video.mp4"]
    assert body["content"] == "아이들과 축제 다녀왔어요"  # 설명 속 #해시태그는 태그 수에 들어가므로 뺀다
    assert body["options"]["naver_clip"] == {"categoryId": "126", "tags": ["형제일상", "축제"], "visibility": "public"}
    saved = json.loads(rec.read_text(encoding="utf-8"))
    assert saved["posts"]["reels"] == {"media_id": "1"} and saved["naver_clip"]["post_id"] == "post_1"


def test_no_clip_in_post_is_skipped(tmp_path):
    d = make_dir(tmp_path, clip=False)
    fake = FakeUplika()
    assert nc.publish_clip(d, tmp_path / "rec.json", "k", client=fake) is None
    assert fake.posts == []


def test_refuses_without_clip_account_for_the_login(tmp_path):
    d = make_dir(tmp_path)
    fake = FakeUplika(accounts=[{"id": "x", "platform": "naver_clip", "displayName": "다른 계정",
                                 "bridgeBlogId": "economypub", "naverClip": {"categories": CATEGORIES}}])
    entry = nc.publish_clip(d, tmp_path / "rec.json", "k", client=fake, sleep=lambda s: None)
    assert entry["status"] == "failed" and "zeusvigor" in entry["error"] and "economypub" in entry["error"]
    assert fake.posts == [] and fake.uploads == []


def test_unknown_category_and_missing_key_fail_before_upload(tmp_path):
    d = make_dir(tmp_path)
    post = json.loads((d / "post.json").read_text(encoding="utf-8"))
    post["naver_clip"]["category"] = "없는 카테고리"
    (d / "post.json").write_text(json.dumps(post, ensure_ascii=False), encoding="utf-8")
    fake = FakeUplika()
    entry = nc.publish_clip(d, tmp_path / "rec.json", "k", client=fake, sleep=lambda s: None)
    assert entry["status"] == "failed" and "없는 카테고리" in entry["error"] and fake.uploads == []
    entry = nc.publish_clip(d, tmp_path / "rec2.json", "", client=FakeUplika(), sleep=lambda s: None)
    assert "VIDEO_UPLIKA_API_KEY" in entry["error"]


def test_rerun_does_not_post_twice_and_retries_failed(tmp_path):
    d = make_dir(tmp_path)
    rec = tmp_path / "rec.json"
    fake = FakeUplika(states=("failed",))
    first = nc.publish_clip(d, rec, "k", client=fake, sleep=lambda s: None)
    assert first["status"] == "failed" and "클립 프로필" in first["error"]
    second = nc.publish_clip(d, rec, "k", client=fake, sleep=lambda s: None)
    assert second["status"] == "published" and fake.retries == ["post_1"] and len(fake.posts) == 1


def test_clip_text_fits_300_units_with_tags():
    text, tags = nc.clip_text("가" * 400, ["a", "b", "c", "d", "e", "f"])
    assert tags == ["a", "b", "c", "d", "e"]
    suffix = "".join(f" #{t}" for t in tags)
    assert nc.units(text + suffix) <= 300 and text.endswith("…")
    short, short_tags = nc.clip_text("안녕 #태그", ["하나"])
    assert short == "안녕" and short_tags == ["하나"]
    emoji, _ = nc.clip_text("😀" * 200, [])  # 이모지는 2단위
    assert nc.units(emoji) <= 300
