"""숏폼 업로더 → 네이버 클립 발행. GitHub Actions에서 Uplika REST API로 보낸다 (네이버 블로그와 같은 방식).

- post.json에 naver_clip 항목이 있을 때만 동작한다.
- 계정: VIDEO_UPLIKA_API_KEY(superabba 워크스페이스 키)로 연결된 네이버 클립 중, 네이버 블로그 아이디가
  VIDEO_NAVER_BLOG_ID(기본 zeusvigor)와 같은 로그인으로 연결된 클립 계정만 쓴다 (Uplika의 bridgeBlogId).
  없으면 다른 클립 계정으로 대신 올리지 않고 실패한다.
- 클립은 세로 영상 1개 + 설명(최대 300자, 해시태그는 설명 뒤에 붙는다). 카테고리는 필수다.
- 결과: video-records 브랜치 records/<ID>.json의 naver_clip 항목. 재실행 때는 이미 보낸 클립을 다시 만들지 않는다.
클립도 PC의 Edge(Uplika 확장)가 대신 올리므로, Edge가 꺼져 있으면 Uplika에서 기다렸다가(최대 7일) 올라간다.

실행:  python -m bot.naver_clip <영상 폴더> --record <결과 json>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from bot.naver_blog import DEFAULT_BLOG_ID, PENDING, Uplika, UplikaError, clean_tags, post_state

MAX_TEXT_UNITS = 300  # 설명 + 붙는 해시태그, UTF-16 코드 단위 (이모지는 2)
MAX_TAGS = 5
VISIBILITIES = {"public", "private"}


def units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def clip_text(description: str, tags: list[str]) -> tuple[str, list[str]]:
    """설명과 해시태그를 300자 안으로 맞춘다. 설명 속 #해시태그는 태그 수(5개)에 들어가므로 뺀다.

    Uplika가 태그를 설명 뒤에 ' #태그'로 붙이므로, 설명은 태그가 들어갈 자리를 남기고 줄인다.
    """
    text = re.sub(r"\s*#\S+", "", description or "").strip()
    tags = clean_tags(tags)[:MAX_TAGS]
    while tags and sum(units(t) + 2 for t in tags) > MAX_TEXT_UNITS - 20:
        tags.pop()  # 태그만으로 설명 자리가 없을 때만 뒤쪽 태그부터 포기 (보통은 설명을 줄인다)
    room = MAX_TEXT_UNITS - sum(units(t) + 2 for t in tags)
    if units(text) > room:
        while text and units(text + "…") > room:
            text = text[:-1]
        text = text.rstrip() + "…"
    return text, tags


def find_clip_account(accounts: list[dict], blog_id: str) -> dict:
    clips = [a for a in accounts if a.get("platform") == "naver_clip"]
    for a in clips:
        if (a.get("bridgeBlogId") or "").lower() == blog_id.lower():
            if a.get("status") not in (None, "active"):
                raise UplikaError(f"Uplika의 네이버 클립 '{a.get('displayName') or a.get('handle')}' 연결 상태가 "
                                  f"'{a.get('status')}'입니다. Uplika 대시보드에서 다시 연결해 주세요.")
            return a
    seen = ", ".join(f"{a.get('displayName') or a.get('handle', '?')}({a.get('bridgeBlogId') or '?'})"
                     for a in clips) or "없음"
    raise UplikaError(f"Uplika에 '{blog_id}' 로그인으로 연결된 네이버 클립이 없습니다 (이 키로 보이는 클립: {seen}). "
                      "Uplika 대시보드에서 네이버 클립을 연결했는지, API 키를 superabba 워크스페이스로 만들었는지 확인해 주세요.")


def category_id(account: dict, name: str) -> str:
    """'라이프스타일 › 가족' 같은 이름을 Uplika가 준 카테고리 id로 바꾼다 (표기 차이는 공백만 무시)."""
    cats = ((account.get("naverClip") or {}).get("categories")) or []
    squash = lambda s: re.sub(r"\s+", "", str(s))  # noqa: E731
    for c in cats:
        if squash(c.get("name")) == squash(name):
            return str(c["id"])
    raise UplikaError(f"네이버 클립 카테고리 '{name}'이(가) 없습니다. 계정 설정에서 다른 카테고리를 고르거나 "
                      "Uplika에서 계정 새로고침을 해 주세요.")


def publish_clip(video_dir: Path, record_path: Path, key: str, blog_id: str = DEFAULT_BLOG_ID,
                 client: Uplika | None = None, poll_s: int = 240, sleep=time.sleep) -> dict | None:
    """post.json의 naver_clip대로 발행하고 결과를 record에 쓴다. 클립이 없으면 None."""
    post = json.loads((video_dir / "post.json").read_text(encoding="utf-8"))
    clip = post.get("naver_clip")
    if not clip:
        return None
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {}
    record.setdefault("id", video_dir.name)
    entry = record.get("naver_clip") or {}

    def save():
        record["naver_clip"] = entry
        record["updated_at"] = datetime.now().isoformat(timespec="seconds")
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        if not key:
            raise UplikaError("VIDEO_UPLIKA_API_KEY Secret이 없습니다. video-uploader/README.md의 '네이버 블로그' 설정을 참고하세요.")
        client = client or Uplika(key)
        if entry.get("post_id") and entry.get("status") != "failed":
            p = client.get_post(entry["post_id"])  # 이미 보낸 클립: 상태만 다시 확인
        elif entry.get("post_id"):
            client.retry_post(entry["post_id"])  # 실패했던 클립: 같은 것으로 다시 시도
            p = client.get_post(entry["post_id"])
        else:
            account = find_clip_account(client.accounts(), blog_id)
            entry["account"] = account.get("displayName") or account.get("handle", "")
            video = video_dir / (clip.get("video") or "")
            if not clip.get("video") or not video.is_file():
                raise UplikaError("클립으로 올릴 영상 파일이 없습니다.")
            text, tags = clip_text(clip.get("description", ""), clip.get("tags") or post.get("hashtags") or [])
            if not text and not tags:
                raise UplikaError("클립 설명이 비어 있습니다.")
            options = {"categoryId": category_id(account, clip.get("category", "")), "tags": tags,
                       "visibility": clip.get("visibility") if clip.get("visibility") in VISIBILITIES else "public"}
            media_id = client.upload(video)
            p = client.create_post({"content": text, "accountIds": [account["id"]], "mediaIds": [media_id],
                                    "options": {"naver_clip": options}},
                                   idempotency_key=f"video-upload-{video_dir.name}-naver-clip")
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
        f.write(f"\n| 네이버 클립 ({entry.get('account', '')}) | {line} |\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bot.naver_clip", description="숏폼 업로더 영상을 네이버 클립에 발행")
    ap.add_argument("video_dir", type=Path)
    ap.add_argument("--record", required=True, type=Path)
    a = ap.parse_args(argv)
    entry = publish_clip(a.video_dir, a.record, os.environ.get("VIDEO_UPLIKA_API_KEY", "").strip(),
                         os.environ.get("VIDEO_NAVER_BLOG_ID", "").strip() or DEFAULT_BLOG_ID)
    if entry is None:
        print("네이버 클립 없음, 건너뜀")
        return 0
    write_summary(entry)
    print(f"네이버 클립: {entry.get('status')} {entry.get('permalink') or entry.get('error') or ''}")
    return 1 if entry.get("status") == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
