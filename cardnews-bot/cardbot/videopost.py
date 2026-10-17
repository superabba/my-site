"""직접 찍은 영상 게시 (PC의 video-uploader 앱이 올린 영상 → 릴스·스레드·쇼츠).

카드뉴스 자동 발행과는 완전히 따로 동작한다.
- 영상: `video/<ID>` 브랜치의 `videos/<ID>/` 폴더 (post.json + 영상 파일).
  모든 플랫폼에 게시되면 워크플로(video-upload.yml)가 이 브랜치를 지운다.
- 결과: `video-records` 브랜치의 `records/<ID>.json`.
  카드뉴스 이력 DB(cardbot.db)에는 쓰지 않으므로 카드뉴스의 '오늘 이미 게시함' 판단과
  주제 추천에 영향을 주지 않는다.
- 계정: 카드뉴스와 다른 '영상 전용' 계정. VIDEO_* Secrets만 쓰고, 없으면 카드뉴스 계정으로
  대신 올리지 않고 그 플랫폼을 실패 처리한다 (video_settings 참고).
  Meta 앱과 구글 OAuth 클라이언트(YOUTUBE_CLIENT_ID/SECRET)는 같이 써도 된다 — 어느 계정에 올라갈지는
  계정 토큰(VIDEO_IG_ACCESS_TOKEN, VIDEO_YOUTUBE_REFRESH_TOKEN 등)이 정한다.

인스타 릴스·스레드는 공개 URL에서 영상을 가져가므로 jsDelivr(파일당 20MB 제한) 주소를 먼저,
raw 주소를 다음으로 시도한다. PC 앱이 인스타·스레드용 영상(meta)을 20MB 아래로 맞춰 올리고,
유튜브는 원본 화질 영상 파일을 직접 올린다.

post.json 예:
  {"title": "...", "youtube_description": "...", "instagram_caption": "...", "threads_text": "...",
   "hashtags": ["일상", "브이로그"], "platforms": ["reels", "threads", "youtube"],
   "youtube_privacy": "public", "files": {"meta": "meta.mp4", "youtube": "video.mp4"}}

실행:  python -m cardbot.videopost <영상 폴더> --repo owner/repo --sha <커밋> --record <결과 json>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import Settings
from .publishers import GraphClient, PublishError, PublishResult, ReelsPublisher

log = logging.getLogger(__name__)

PLATFORMS = ("reels", "threads", "youtube")
LABELS = {"reels": "인스타 릴스", "threads": "스레드", "youtube": "유튜브 쇼츠"}
JSDELIVR_MAX_BYTES = 20 * 1024 * 1024  # jsDelivr는 20MB 넘는 파일을 403으로 거절한다
IG_MAX_HASHTAGS = 30


class ThreadsVideoPublisher:
    """스레드 영상 게시 (media_type=VIDEO). 카드뉴스용 ThreadsPublisher는 이미지 캐러셀만 다룬다."""

    name = "threads"

    def __init__(self, user_id: str, token: str, base: str, sleep=time.sleep):
        if not user_id or not token:
            raise ValueError("THREADS_USER_ID, THREADS_ACCESS_TOKEN이 필요합니다")
        self.user_id = user_id
        self.api = GraphClient(base, token, sleep)

    def publish(self, assets: dict, text: str) -> PublishResult:
        urls = assets.get("video_urls") or []
        if not urls:
            raise PublishError("스레드에 올릴 영상이 없습니다")
        errors = []
        for url in urls:  # 호스팅 URL 후보를 차례로 시도
            params = {"media_type": "VIDEO", "video_url": url}
            if text.strip():
                params["text"] = text.strip()[:500]
            try:
                container = self.api.post(f"{self.user_id}/threads", **params)
                self.api.wait_ready(container["id"], "status", timeout_s=600)
            except PublishError as e:
                log.warning("스레드 영상 URL 실패, 다음 후보 시도: %s (%s)", url, e)
                errors.append(f"{url}: {e}")
                continue
            media = self.api.publish_container(f"{self.user_id}/threads_publish", container["id"], "status")
            link = ""
            try:
                link = self.api.get(media["id"], fields="permalink").get("permalink", "")
            except PublishError as e:
                log.warning("permalink 조회 실패: %s", e)
            return PublishResult(self.name, media["id"], link)
        raise PublishError("스레드가 영상을 가져오지 못했습니다 — " + " | ".join(errors))


def video_urls(repo: str, sha: str, rel_path: str, size: int) -> list[str]:
    """Meta가 가져갈 공개 URL 후보. 커밋을 고정해 브랜치가 지워지기 전까지 같은 파일을 가리킨다."""
    raw = f"https://raw.githubusercontent.com/{repo}/{sha}/{rel_path}"
    if size > JSDELIVR_MAX_BYTES:
        return [raw]
    return [f"https://cdn.jsdelivr.net/gh/{repo}@{sha}/{rel_path}", raw]


def with_hashtags(text: str, hashtags: list[str], limit: int | None = None) -> str:
    tags = [f"#{h.lstrip('#')}" for h in hashtags if h.strip("# ")]
    if limit is not None:
        tags = tags[:limit]
    return f"{text.strip()}\n\n{' '.join(tags)}".strip()


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def video_settings() -> Settings:
    """영상 업로드 전용 계정 설정.

    계정 토큰은 VIDEO_* 값만 쓴다. 비어 있어도 카드뉴스 계정(IG_*, THREADS_*, YOUTUBE_REFRESH_TOKEN)으로
    대신 채우지 않는다 — 잘못된 계정에 올리는 것보다 실패하는 편이 낫다.
    구글 OAuth 클라이언트는 계정이 아니라 앱이므로 VIDEO_YOUTUBE_CLIENT_*가 없으면 카드뉴스 것을 쓴다.
    """
    s = Settings.load()
    s.ig_user_id, s.ig_access_token = _env("VIDEO_IG_USER_ID"), _env("VIDEO_IG_ACCESS_TOKEN")
    s.threads_user_id, s.threads_access_token = _env("VIDEO_THREADS_USER_ID"), _env("VIDEO_THREADS_ACCESS_TOKEN")
    s.youtube_refresh_token = _env("VIDEO_YOUTUBE_REFRESH_TOKEN")
    s.youtube_client_id = _env("VIDEO_YOUTUBE_CLIENT_ID") or s.youtube_client_id
    s.youtube_client_secret = _env("VIDEO_YOUTUBE_CLIENT_SECRET") or s.youtube_client_secret
    s.youtube_channel_id = _env("VIDEO_YOUTUBE_CHANNEL_ID")
    s.youtube_category = _env("VIDEO_YOUTUBE_CATEGORY") or "22"  # 22=인물/블로그 (카드뉴스는 27=교육)
    return s


SETUP_HINT = "video-uploader/README.md의 '영상 전용 계정 연결'을 참고하세요."


def make_publisher(name: str, s: Settings, post: dict):
    if name == "reels":
        if not (s.ig_user_id and s.ig_access_token):
            raise ValueError(f"영상용 인스타 계정 Secrets(VIDEO_IG_USER_ID, VIDEO_IG_ACCESS_TOKEN)가 없습니다. {SETUP_HINT}")
        return ReelsPublisher(s.ig_user_id, s.ig_access_token, s.ig_graph_base)
    if name == "threads":
        if not (s.threads_user_id and s.threads_access_token):
            raise ValueError(
                f"영상용 스레드 계정 Secrets(VIDEO_THREADS_USER_ID, VIDEO_THREADS_ACCESS_TOKEN)가 없습니다. {SETUP_HINT}")
        return ThreadsVideoPublisher(s.threads_user_id, s.threads_access_token, s.threads_graph_base)
    if name == "youtube":
        from .youtube import YouTubePublisher

        if not s.youtube_refresh_token:
            raise ValueError(f"영상용 유튜브 채널 Secret(VIDEO_YOUTUBE_REFRESH_TOKEN)이 없습니다. {SETUP_HINT}")
        if not (s.youtube_client_id and s.youtube_client_secret):
            raise ValueError("유튜브 OAuth 클라이언트(YOUTUBE_CLIENT_ID/SECRET 또는 VIDEO_YOUTUBE_CLIENT_ID/SECRET)가 없습니다.")
        return YouTubePublisher(
            s.youtube_client_id, s.youtube_client_secret, s.youtube_refresh_token, s.youtube_channel_id,
            post.get("youtube_privacy") or s.youtube_privacy, s.youtube_category, s.youtube_synthetic,
        )
    raise ValueError(f"알 수 없는 플랫폼: {name}")


def publish_video(video_dir: Path, repo: str, sha: str, record_path: Path, settings: Settings,
                  publishers: dict | None = None) -> dict:
    """post.json대로 게시하고 결과를 record_path에 쓴다. 이미 게시된 플랫폼은 건너뛴다(재시도용)."""
    post = json.loads((video_dir / "post.json").read_text(encoding="utf-8"))
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {}
    record.setdefault("id", video_dir.name)
    record.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    record.update(title=post.get("title", ""), platforms=post.get("platforms", []))
    record.setdefault("posts", {})
    record.setdefault("errors", {})

    def save():
        record["updated_at"] = datetime.now().isoformat(timespec="seconds")
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    files = post.get("files") or {}
    meta_file = video_dir / files.get("meta", "video.mp4")
    youtube_file = video_dir / files.get("youtube", meta_file.name)
    hashtags = [h.lstrip("#") for h in post.get("hashtags", []) if h.strip("# ")]
    title = (post.get("title") or "").strip() or (post.get("instagram_caption") or "").strip().split("\n")[0]
    assets = {
        "video_urls": video_urls(repo, sha, f"videos/{video_dir.name}/{meta_file.name}", meta_file.stat().st_size)
        if meta_file.exists() else [],
        "reel_cover_url": None,
        "video_path": str(youtube_file),
        "title": title,
        "hashtags": hashtags,
    }
    texts = {
        "reels": with_hashtags(post.get("instagram_caption", ""), hashtags, IG_MAX_HASHTAGS)[:2200],
        "threads": (post.get("threads_text") or post.get("instagram_caption") or "")[:500],
        "youtube": with_hashtags(post.get("youtube_description", ""), hashtags),
    }

    for name in record["platforms"]:
        if name in record["posts"]:
            log.info("%s: 이미 게시됨, 건너뜀", name)
            continue
        try:
            if name not in PLATFORMS:
                raise ValueError(f"알 수 없는 플랫폼: {name}")
            pub = (publishers or {}).get(name) or make_publisher(name, settings, post)
            res = pub.publish(assets, texts[name])
        except Exception as e:
            log.error("%s 게시 실패: %s", name, e)
            record["errors"][name] = str(e)
            save()
            continue
        log.info("%s 게시 완료: %s", name, res.permalink or res.media_id)
        record["posts"][name] = {"media_id": res.media_id, "permalink": res.permalink}
        record["errors"].pop(name, None)
        save()
    save()
    return record


def write_summary(record: dict) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [f"## 영상 업로드: {record.get('title') or record['id']}", "", "| 플랫폼 | 결과 |", "|---|---|"]
    for name in record.get("platforms", []):
        if name in record["posts"]:
            info = record["posts"][name]
            lines.append(f"| {LABELS.get(name, name)} | ✅ {info.get('permalink') or info.get('media_id')} |")
        else:
            err = record["errors"].get(name, "").replace("|", "/").replace("\n", " ")[:300]
            lines.append(f"| {LABELS.get(name, name)} | ❌ {err} |")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cardbot.videopost", description="직접 찍은 영상을 릴스·스레드·쇼츠에 게시")
    ap.add_argument("video_dir", type=Path)
    ap.add_argument("--repo", required=True, help="영상이 올라간 저장소 (owner/repo)")
    ap.add_argument("--sha", required=True, help="영상 커밋 sha (공개 URL 고정용)")
    ap.add_argument("--record", required=True, type=Path, help="결과를 쓸 json 경로")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    record = publish_video(a.video_dir, a.repo, a.sha, a.record, video_settings())
    write_summary(record)
    failed = [n for n in record["platforms"] if n not in record["posts"]]
    for n in failed:
        print(f"게시 실패: {n}: {record['errors'].get(n, '')}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
