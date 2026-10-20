"""쇼츠 스튜디오(PC 앱)가 만든 영상 → 경제주막(economypub) 계정의 릴스·스레드·유튜브 쇼츠에 게시.

카드뉴스 자동 발행과 '같은 계정'에 올리지만, 실행·데이터는 완전히 따로다.
- 영상: `shorts/<ID>` 브랜치의 `videos/<ID>/` 폴더 (post.json + 영상). 다 게시되면 워크플로가 브랜치를 지운다.
- 결과: `shorts-records` 브랜치의 `records/<ID>.json`. 카드뉴스 이력 DB(cardbot.db)와 cardbot-data 브랜치는
  건드리지 않으므로 카드뉴스의 '오늘 이미 게시함' 판단과 주제 추천에 영향이 없다.
- 계정 값: 카드뉴스 Secrets(IG_*, THREADS_*, YOUTUBE_*)를 읽기만 한다. 토큰 갱신은 지금처럼 카드뉴스 토큰 갱신
  워크플로만 맡는다 (두 곳에서 갱신하면 서로 토큰을 바꿔 끼워 카드뉴스가 멈출 수 있다).

카드뉴스를 멈추지 않게 하는 장치 (워크플로 Variables로 조정):
1. 계정 확인: 인스타·스레드 아이디가 SHORTS_ACCOUNT(기본 economypub)와 다르면 올리지 않는다.
   유튜브는 YOUTUBE_CHANNEL_ID(카드뉴스와 같은 값)가 있으면 그 채널인지 확인한다.
2. 카드뉴스 몫 남기기: 인스타·스레드의 24시간 게시 한도를 조회해, 남은 횟수가 SHORTS_RESERVE(기본 10) 이하면 건너뛴다.
3. 쇼츠 상한: 플랫폼마다 최근 24시간에 SHORTS_DAILY_MAX(기본 5)개까지만 올린다.
   유튜브 API 하루 할당량은 카드뉴스와 같은 구글 프로젝트에서 나가므로 이것으로 함께 지킨다.

실행:  python -m cardbot.shortspost <영상 폴더> --repo owner/repo --sha <커밋> --record <결과 json> --records-dir <결과 폴더>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings
from .publishers import GraphClient, PublishError, PublishResult, ReelsPublisher
from .videopost import LABELS, PLATFORMS, ThreadsVideoPublisher, publish_video

log = logging.getLogger(__name__)

DEFAULT_ACCOUNT = "economypub"
DEFAULT_DAILY_MAX = 5
DEFAULT_RESERVE = 10
SETUP_HINT = "카드뉴스 봇과 같은 계정 Secrets를 씁니다. cardnews-bot/README.md의 '쇼츠 스튜디오 발행'을 참고하세요."


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _int_env(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def shorts_settings() -> Settings:
    """경제주막(카드뉴스) 계정 설정. 유튜브 OAuth 앱만 SHORTS_YOUTUBE_*로 따로 둘 수 있다 (할당량 분리용)."""
    s = Settings.load()
    if _env("SHORTS_YOUTUBE_REFRESH_TOKEN"):
        # 다른 구글 OAuth 앱으로 받은 토큰이면 그 앱의 ID·비밀번호와 짝을 맞춰야 한다
        s.youtube_refresh_token = _env("SHORTS_YOUTUBE_REFRESH_TOKEN")
        s.youtube_client_id = _env("SHORTS_YOUTUBE_CLIENT_ID") or s.youtube_client_id
        s.youtube_client_secret = _env("SHORTS_YOUTUBE_CLIENT_SECRET") or s.youtube_client_secret
    s.youtube_category = _env("SHORTS_YOUTUBE_CATEGORY") or s.youtube_category
    return s


def expected_account() -> str:
    return (_env("SHORTS_ACCOUNT") or DEFAULT_ACCOUNT).lstrip("@").lower()


def make_publisher(name: str, s: Settings, post: dict):
    """카드뉴스 계정 값으로 게시기를 만든다. 값이 없으면 다른 계정으로 대신 올리지 않고 실패한다."""
    if name == "reels":
        if not (s.ig_user_id and s.ig_access_token):
            raise ValueError(f"인스타 계정 Secrets(IG_USER_ID, IG_ACCESS_TOKEN)가 없습니다. {SETUP_HINT}")
        return ReelsPublisher(s.ig_user_id, s.ig_access_token, s.ig_graph_base)
    if name == "threads":
        if not (s.threads_user_id and s.threads_access_token):
            raise ValueError(f"스레드 계정 Secrets(THREADS_USER_ID, THREADS_ACCESS_TOKEN)가 없습니다. {SETUP_HINT}")
        return ThreadsVideoPublisher(s.threads_user_id, s.threads_access_token, s.threads_graph_base)
    if name == "youtube":
        from .youtube import YouTubePublisher

        if not (s.youtube_client_id and s.youtube_client_secret and s.youtube_refresh_token):
            raise ValueError(f"유튜브 Secrets(YOUTUBE_CLIENT_ID/SECRET, YOUTUBE_REFRESH_TOKEN)가 없습니다. {SETUP_HINT}")
        return YouTubePublisher(
            s.youtube_client_id, s.youtube_client_secret, s.youtube_refresh_token, s.youtube_channel_id,
            post.get("youtube_privacy") or "public", s.youtube_category, bool(post.get("youtube_synthetic")),
        )
    raise ValueError(f"알 수 없는 플랫폼: {name}")


# ---------------------------------------------------------------- 안전장치

def _graph(name: str, s: Settings) -> tuple[GraphClient, str]:
    if name == "reels":
        return GraphClient(s.ig_graph_base, s.ig_access_token), s.ig_user_id
    return GraphClient(s.threads_graph_base, s.threads_access_token), s.threads_user_id


def account_username(name: str, s: Settings) -> str:
    api, _ = _graph(name, s)
    return str(api.get("me", fields="username").get("username", ""))


def remaining_quota(name: str, s: Settings) -> int | None:
    """24시간 게시 한도에서 남은 횟수. 조회가 안 되면 None (그때는 막지 않는다)."""
    api, user_id = _graph(name, s)
    path = "content_publishing_limit" if name == "reels" else "threads_publishing_limit"
    try:
        data = api.get(f"{user_id}/{path}", fields="quota_usage,config")
        item = (data.get("data") or [data])[0]
        total = int((item.get("config") or {}).get("quota_total"))
        used = int(item.get("quota_usage") or 0)
    except (PublishError, TypeError, ValueError, IndexError) as e:
        log.warning("%s 게시 한도를 조회하지 못해 그대로 진행합니다: %s", LABELS.get(name, name), e)
        return None
    log.info("%s 24시간 게시 한도: %d/%d 사용", LABELS.get(name, name), used, total)
    return total - used


def recent_posts(records_dir: Path | None, name: str, now: datetime, skip_id: str = "") -> int:
    """최근 24시간 안에 쇼츠 스튜디오가 이 플랫폼에 게시한 수."""
    if not records_dir or not records_dir.is_dir():
        return 0
    since = now - timedelta(hours=24)
    count = 0
    for path in records_dir.glob("*.json"):
        if path.stem == skip_id:
            continue
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
            stamp = (rec.get("posts") or {}).get(name, {}).get("published_at")
            if stamp and datetime.fromisoformat(stamp) >= since:
                count += 1
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    return count


class GuardedPublisher:
    """게시 직전에 안전장치를 확인하고, 통과하면 실제 게시기로 올린다."""

    def __init__(self, name: str, s: Settings, post: dict, records_dir: Path | None, video_id: str,
                 now: datetime | None = None, build=None):
        self.name, self.s, self.post = name, s, post
        self.records_dir, self.video_id = records_dir, video_id
        self.now = now or datetime.now(timezone.utc)
        self.build = build

    def publish(self, assets: dict, text: str) -> PublishResult:
        label = LABELS.get(self.name, self.name)
        daily_max = _int_env("SHORTS_DAILY_MAX", DEFAULT_DAILY_MAX)
        done = recent_posts(self.records_dir, self.name, self.now, self.video_id)
        if done >= daily_max:
            raise PublishError(f"최근 24시간에 {label}에 쇼츠를 {done}개 올려서 더 올리지 않았습니다 "
                               f"(하루 상한 SHORTS_DAILY_MAX={daily_max}). 내일 다시 시도해 주세요.")
        pub = (self.build or make_publisher)(self.name, self.s, self.post)
        if self.name in ("reels", "threads"):
            want = expected_account()
            got = account_username(self.name, self.s).lstrip("@").lower()
            if got != want:
                raise PublishError(f"{label} 계정이 @{got}라서 올리지 않았습니다 (발행 계정 @{want}). "
                                   "계정이 맞다면 저장소 Variables의 SHORTS_ACCOUNT를 바꿔 주세요.")
            reserve = _int_env("SHORTS_RESERVE", DEFAULT_RESERVE)
            left = remaining_quota(self.name, self.s)
            if left is not None and left <= reserve:
                raise PublishError(f"{label} 24시간 게시 한도가 {left}개 남아, 카드뉴스 몫(SHORTS_RESERVE={reserve})을 "
                                   "남기려고 올리지 않았습니다. 나중에 다시 시도해 주세요.")
        return pub.publish(assets, text)


# ---------------------------------------------------------------- 실행

def publish_shorts(video_dir: Path, repo: str, sha: str, record_path: Path, records_dir: Path | None,
                   settings: Settings, now: datetime | None = None, build=None) -> dict:
    post = json.loads((video_dir / "post.json").read_text(encoding="utf-8"))
    now = now or datetime.now(timezone.utc)
    pubs = {name: GuardedPublisher(name, settings, post, records_dir, video_dir.name, now, build)
            for name in PLATFORMS}
    record = publish_video(video_dir, repo, sha, record_path, settings, pubs)
    record["account"] = expected_account()
    for key in ("daily", "topic", "keyword", "theme"):  # 매일 자동 쇼츠: '오늘 이미 게시함'·주제 중복 판단용
        if key in post:
            record[key] = post[key]
    for info in record.get("posts", {}).values():
        info.setdefault("published_at", now.isoformat(timespec="seconds"))
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def write_summary(record: dict) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [f"## 쇼츠 발행 (@{record.get('account', '')}): {record.get('title') or record['id']}", "",
             "| 플랫폼 | 결과 |", "|---|---|"]
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
    ap = argparse.ArgumentParser(prog="cardbot.shortspost", description="쇼츠 스튜디오 영상을 경제주막 계정에 게시")
    ap.add_argument("video_dir", type=Path)
    ap.add_argument("--repo", required=True, help="영상이 올라간 저장소 (owner/repo)")
    ap.add_argument("--sha", required=True, help="영상 커밋 sha (공개 URL 고정용)")
    ap.add_argument("--record", required=True, type=Path, help="결과를 쓸 json 경로")
    ap.add_argument("--records-dir", type=Path, help="지난 결과 폴더 (하루 상한 계산용)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    record = publish_shorts(a.video_dir, a.repo, a.sha, a.record, a.records_dir, shorts_settings())
    write_summary(record)
    failed = [n for n in record["platforms"] if n not in record["posts"]]
    for n in failed:
        print(f"게시 실패: {n}: {record['errors'].get(n, '')}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
