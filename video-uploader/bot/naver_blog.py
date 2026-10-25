"""숏폼 업로더 → 네이버 블로그(슈퍼아빠) 발행. GitHub Actions에서 Uplika REST API로 보낸다.

카드뉴스 봇의 네이버 발행(Claude 루틴)과는 별개로, 영상을 올릴 때 바로 발행한다.
- post.json에 naver_blog 항목이 있을 때만 동작한다.
- 계정: VIDEO_UPLIKA_API_KEY(superabba 워크스페이스 키)로 연결된 네이버 블로그 중
  아이디가 VIDEO_NAVER_BLOG_ID(기본 zeusvigor)인 블로그만 쓴다. 없으면 다른 블로그로 대신 올리지 않고 실패한다.
- 본문: PC 앱에서 쓴 글의 [사진 N], [영상] 줄을 Uplika 문법(![](media:<id>), @video(media:<id>))으로 바꾼다.
  사진·영상은 영상 브랜치에 함께 올라온 파일을 presign → PUT → complete로 Uplika에 올린다.
- 결과: video-records 브랜치 records/<ID>.json의 naver_blog 항목. 재실행 때는 이미 보낸 글을 다시 만들지 않는다.
네이버 글은 PC의 Edge(Uplika 확장)가 대신 쓰므로, Edge가 꺼져 있으면 Uplika에서 기다렸다가(최대 7일) 올라간다.

실행:  python -m bot.naver_blog <영상 폴더> --record <결과 json>
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

API = "https://api.uplika.com/v1"
DEFAULT_BLOG_ID = "zeusvigor"
PHOTO_LINE = re.compile(r"^\s*\[사진\s*(\d+)\]\s*$")
VIDEO_LINE = re.compile(r"^\s*\[영상\]\s*$")
PENDING = {"publishing", "queued", "scheduled", "pending", "processing"}
MAX_BODY_CHARS = 30000
MAX_TITLE_CHARS = 100
OPEN_TYPES = {"public", "neighbor", "mutual", "private"}


class UplikaError(Exception):
    def __init__(self, message: str, code: str = "", status: int = 0):
        super().__init__(message)
        self.code, self.status = code, status


def _unwrap(data):
    """응답이 {"data": …}로 감싸져 있으면 벗긴다."""
    if isinstance(data, dict) and "data" in data and "id" not in data:
        return data["data"]
    return data


class Uplika:
    def __init__(self, key: str, session: requests.Session | None = None):
        self.s = session or requests.Session()
        self.s.headers.update({"authorization": f"Bearer {key.strip()}"})

    def _req(self, method: str, path: str, **kw) -> dict:
        kw.setdefault("timeout", 60)
        try:
            resp = self.s.request(method, f"{API}{path}", **kw)
        except requests.RequestException as e:
            raise UplikaError(f"Uplika에 연결하지 못했습니다: {e}") from e
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {"raw": resp.text[:300]}
        if resp.status_code >= 400:
            err = data.get("error", data) if isinstance(data, dict) else data
            code = err.get("code", "") if isinstance(err, dict) else ""
            msg = err.get("message", "") if isinstance(err, dict) else str(err)
            if resp.status_code == 401:
                msg = "Uplika API 키가 잘못되었거나 만료되었습니다. 새 키를 VIDEO_UPLIKA_API_KEY에 넣어 주세요."
            elif resp.status_code == 403:
                msg = f"Uplika API 키 권한이 부족합니다 (accounts:read, posts:read, posts:write 필요). {msg}"
            raise UplikaError(f"Uplika {resp.status_code} {code}: {msg}".strip(), code, resp.status_code)
        return data

    def accounts(self) -> list[dict]:
        return _unwrap(self._req("GET", "/accounts")) or []

    def upload(self, path: Path) -> str:
        """파일 하나를 Uplika에 올리고 미디어 id를 돌려준다."""
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        slot = _unwrap(self._req("POST", "/media/presign", json={
            "fileName": path.name, "contentType": ctype, "bytes": path.stat().st_size,
        }))
        with open(path, "rb") as f:
            try:
                put = requests.put(slot["uploadUrl"], data=f, timeout=(30, 900),
                                   headers={"Content-Type": slot.get("contentType") or ctype})
            except requests.RequestException as e:
                raise UplikaError(f"{path.name} 올리기 실패: {e}") from e
        if put.status_code >= 400:
            raise UplikaError(f"{path.name} 올리기 실패: HTTP {put.status_code}")
        self._req("POST", f"/media/{slot['id']}/complete")
        return slot["id"]

    def create_post(self, body: dict, idempotency_key: str) -> dict:
        return _unwrap(self._req("POST", "/posts", json=body, timeout=120,
                                 headers={"Idempotency-Key": idempotency_key}))

    def get_post(self, post_id: str) -> dict:
        return _unwrap(self._req("GET", f"/posts/{post_id}"))

    def retry_post(self, post_id: str) -> dict:
        return _unwrap(self._req("POST", f"/posts/{post_id}/retry"))


# ── 글 만들기 ───────────────────────────────────────────────

def find_account(accounts: list[dict], blog_id: str) -> dict:
    blogs = [a for a in accounts if a.get("platform") == "naver_blog"]
    for a in blogs:
        if (a.get("handle") or "").lower() == blog_id.lower():
            if a.get("status") not in (None, "active"):
                raise UplikaError(f"Uplika의 '{blog_id}' 네이버 블로그 연결 상태가 '{a.get('status')}'입니다. "
                                  "Uplika 대시보드에서 다시 연결해 주세요.")
            return a
    seen = ", ".join(a.get("handle", "?") for a in blogs) or "없음"
    raise UplikaError(f"Uplika에 '{blog_id}' 네이버 블로그가 연결되어 있지 않습니다 (이 키로 보이는 블로그: {seen}). "
                      "API 키를 superabba 워크스페이스로 만들었는지 확인해 주세요.")


def clean_tags(tags: list[str]) -> list[str]:
    out = []
    for t in tags or []:
        t = re.sub(r"\s+", "", str(t)).lstrip("#")[:30]
        if t and t not in out:
            out.append(t)
    return out[:30]


def build_content(video_dir: Path, blog: dict, upload) -> str:
    """[사진 N]/[영상] 줄을 올린 미디어로 바꾼다. 없는 사진을 가리키는 줄은 뺀다."""
    photos = blog.get("photos") or []
    video = blog.get("video")
    uploaded: dict[str, str] = {}

    def media_id(name: str) -> str | None:
        path = video_dir / name
        if not path.is_file():
            return None
        if name not in uploaded:
            uploaded[name] = upload(path)
        return uploaded[name]

    out = []
    for line in (blog.get("body") or "").splitlines():
        if m := PHOTO_LINE.match(line):
            n = int(m.group(1))
            mid = media_id(photos[n - 1]) if 1 <= n <= len(photos) else None
            if mid:
                out.append(f"![](media:{mid})")
            continue
        if VIDEO_LINE.match(line):
            mid = media_id(video) if video else None
            if mid:
                out.append(f"@video(media:{mid})")
            continue
        out.append(line)
    return "\n".join(out).strip()


def post_state(post: dict) -> tuple[str, str, str]:
    """(상태, 글 주소, 오류) — 대상이 네이버 블로그 하나뿐이다."""
    targets = post.get("targets") or []
    t = targets[0] if targets else {}
    status = t.get("status") or post.get("status") or ""
    url = t.get("url") or t.get("permalink") or post.get("permalink") or ""
    err = t.get("error") or post.get("error") or ""
    if isinstance(err, dict):
        err = f"{err.get('code', '')}: {err.get('message', '')}".strip(": ")
    return status, url or "", str(err)


def publish_blog(video_dir: Path, record_path: Path, key: str, blog_id: str = DEFAULT_BLOG_ID,
                 client: Uplika | None = None, poll_s: int = 240, sleep=time.sleep) -> dict | None:
    """post.json의 naver_blog대로 발행하고 결과를 record에 쓴다. 블로그 글이 없으면 None."""
    post = json.loads((video_dir / "post.json").read_text(encoding="utf-8"))
    blog = post.get("naver_blog")
    if not blog:
        return None
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {}
    record.setdefault("id", video_dir.name)
    entry = record.get("naver_blog") or {}
    entry.update(title=blog.get("title", ""), blog=blog_id)

    def save():
        record["naver_blog"] = entry
        record["updated_at"] = datetime.now().isoformat(timespec="seconds")
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        if not key:
            raise UplikaError("VIDEO_UPLIKA_API_KEY Secret이 없습니다. video-uploader/README.md의 '네이버 블로그' 설정을 참고하세요.")
        client = client or Uplika(key)
        if entry.get("post_id") and entry.get("status") != "failed":
            p = client.get_post(entry["post_id"])  # 이미 보낸 글: 상태만 다시 확인
        elif entry.get("post_id"):
            client.retry_post(entry["post_id"])  # 실패했던 글: 같은 글로 다시 시도
            p = client.get_post(entry["post_id"])
        else:
            account = find_account(client.accounts(), blog_id)
            title = (blog.get("title") or "").strip()[:MAX_TITLE_CHARS]
            if not title:
                raise UplikaError("블로그 제목이 비어 있습니다.")
            content = build_content(video_dir, blog, client.upload)
            if not content:
                raise UplikaError("블로그 본문이 비어 있습니다.")
            if len(content) > MAX_BODY_CHARS:
                raise UplikaError(f"블로그 본문이 너무 깁니다 ({len(content)}자, 최대 {MAX_BODY_CHARS}자).")
            open_type = blog.get("open_type") if blog.get("open_type") in OPEN_TYPES else "public"
            options = {"title": title, "openType": open_type, "layout": "as-is", "alignCenter": True,
                       "tags": clean_tags(blog.get("tags"))}
            if blog.get("category"):
                options["category"] = blog["category"]
            p = client.create_post({"content": content, "accountIds": [account["id"]],
                                    "options": {"naver_blog": options}},
                                   idempotency_key=f"video-upload-{video_dir.name}-naver-blog")
            entry["post_id"] = p.get("id", "")
            bridge = p.get("bridge") or {}
            if bridge.get("userMessage"):
                entry["note"] = bridge["userMessage"]
        save()
        status, url, err = post_state(p)
        waited = 0
        while status in PENDING and waited < poll_s:  # Edge가 켜져 있으면 보통 1~3분 안에 올라간다
            sleep(10)
            waited += 10
            status, url, err = post_state(client.get_post(entry["post_id"]))
        entry.update(status=status or "unknown", permalink=url, error=err if status == "failed" else "")
    except UplikaError as e:
        entry.update(status="failed", error=str(e))
    save()
    return entry


def write_summary(entry: dict) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path or not entry:
        return
    status = entry.get("status")
    if status == "published":
        line = f"✅ {entry.get('permalink') or entry.get('post_id')}"
    elif status == "failed":
        line = f"❌ {entry.get('error', '')}".replace("|", "/")
    else:
        line = f"⏳ Uplika에 전달됨 ({status}). {entry.get('note', 'PC의 Edge(Uplika 확장)가 켜져 있으면 곧 올라갑니다.')}"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n| 네이버 블로그 ({entry.get('blog')}) | {line} |\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bot.naver_blog", description="숏폼 업로더 영상의 블로그 글을 네이버 블로그에 발행")
    ap.add_argument("video_dir", type=Path)
    ap.add_argument("--record", required=True, type=Path)
    a = ap.parse_args(argv)
    entry = publish_blog(a.video_dir, a.record, os.environ.get("VIDEO_UPLIKA_API_KEY", "").strip(),
                         os.environ.get("VIDEO_NAVER_BLOG_ID", "").strip() or DEFAULT_BLOG_ID)
    if entry is None:
        print("네이버 블로그 글 없음, 건너뜀")
        return 0
    write_summary(entry)
    print(f"네이버 블로그: {entry.get('status')} {entry.get('permalink') or entry.get('error') or ''}")
    return 1 if entry.get("status") == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
