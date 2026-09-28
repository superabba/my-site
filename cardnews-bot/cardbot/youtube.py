"""유튜브 쇼츠 업로드 (YouTube Data API v3).

릴스용 세로 영상(1080x1920, 3분 이하)을 그대로 올리면 유튜브가 쇼츠로 분류한다.
Meta와 달리 공개 URL이 아니라 파일을 직접 올린다 (resumable upload).

인증: OAuth 2.0 refresh token (python -m cardbot.youtube_auth 로 한 번 발급).
한 구글 계정 아래 채널(브랜드 계정)이 여러 개면, 인증할 때 고른 채널로 올라간다.
YOUTUBE_CHANNEL_ID를 지정하면 업로드 직전에 채널을 확인하고 다르면 올리지 않는다.

주의: YouTube API 검수(audit)를 통과하지 않은 프로젝트에서 올린 영상은 공개로 요청해도
유튜브가 '비공개'로 잠근다. 검수 전에는 YouTube Studio에서 직접 공개로 바꿔야 한다.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import requests

from .publishers import PublishError, PublishResult

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",  # 채널 확인·조회수 수집
]


def access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    r = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise PublishError(f"유튜브 토큰 갱신 실패 {r.status_code}: {r.text[:300]}")
    return r.json()["access_token"]


def _clean(text: str) -> str:
    # 유튜브 제목·설명에는 '<', '>'를 쓸 수 없다
    return text.replace("<", "(").replace(">", ")").strip()


def build_metadata(title: str, caption: str, hashtags: list[str], category: str, privacy: str,
                   synthetic: bool) -> dict:
    title = _clean(re.sub(r"\s+", " ", title))
    if len(title) > 100:
        title = title[:99].rstrip() + "…"
    desc = _clean(caption)
    if "#shorts" not in desc.lower():
        desc += "\n\n#Shorts"
    tags, total = [], 0
    for t in hashtags:  # 태그 전체 길이 500자 제한
        t = t.lstrip("#").strip()
        if t and t not in tags and total + len(t) + 1 <= 450:
            tags.append(t)
            total += len(t) + 1
    return {
        "snippet": {
            "title": title,
            "description": desc[:5000],
            "tags": tags,
            "categoryId": category,
            "defaultLanguage": "ko",
            "defaultAudioLanguage": "ko",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": synthetic,
        },
    }


class YouTubePublisher:
    name = "youtube"
    needs_video = False  # 공개 URL이 아니라
    needs_video_file = True  # 로컬 reel.mp4 파일을 직접 올린다

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, channel_id: str = "",
                 privacy: str = "private", category: str = "27", synthetic: bool = False):
        if not (client_id and client_secret and refresh_token):
            raise ValueError("YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN이 필요합니다")
        if privacy not in {"public", "unlisted", "private"}:
            raise ValueError(f"YOUTUBE_PRIVACY는 public/unlisted/private 중 하나여야 합니다: {privacy}")
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.channel_id, self.privacy, self.category, self.synthetic = channel_id, privacy, category, synthetic
        self._token = ""

    def _headers(self) -> dict:
        if not self._token:
            self._token = access_token(self.client_id, self.client_secret, self.refresh_token)
        return {"Authorization": f"Bearer {self._token}"}

    def _check(self, r: requests.Response) -> dict:
        if r.status_code >= 400:
            raise PublishError(f"유튜브 API {r.status_code}: {r.text[:400]}")
        return r.json() if r.content else {}

    def my_channel(self) -> tuple[str, str]:
        data = self._check(requests.get(f"{API}/channels", params={"part": "id,snippet", "mine": "true"},
                                        headers=self._headers(), timeout=30))
        items = data.get("items") or []
        if not items:
            raise PublishError("인증된 계정에 유튜브 채널이 없습니다")
        return items[0]["id"], items[0]["snippet"]["title"]

    def publish(self, assets: dict, caption: str) -> PublishResult:
        video = assets.get("video_path")
        if not video or not Path(video).exists():
            raise PublishError("유튜브에 올릴 릴스 영상(reel.mp4)이 없습니다")
        channel_id, channel_title = self.my_channel()
        if self.channel_id and channel_id != self.channel_id:
            raise PublishError(
                f"인증된 채널({channel_title}, {channel_id})이 YOUTUBE_CHANNEL_ID({self.channel_id})와 달라 올리지 않았습니다"
            )
        meta = build_metadata(assets.get("title") or "", caption, assets.get("hashtags") or [],
                              self.category, self.privacy, self.synthetic)
        size = Path(video).stat().st_size
        start = requests.post(
            UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={**self._headers(), "X-Upload-Content-Type": "video/mp4",
                     "X-Upload-Content-Length": str(size)},
            json=meta,
            timeout=60,
        )
        self._check(start)
        session = start.headers.get("Location")
        if not session:
            raise PublishError("유튜브 업로드 세션을 받지 못했습니다")
        with open(video, "rb") as f:
            done = requests.put(session, headers={**self._headers(), "Content-Type": "video/mp4"},
                                data=f, timeout=600)
        data = self._check(done)
        vid = data["id"]
        got = (data.get("status") or {}).get("privacyStatus")
        if got and got != self.privacy:
            log.warning("유튜브가 공개 범위를 %s → %s로 바꿨습니다 (API 검수 전 프로젝트는 비공개로 잠김)",
                        self.privacy, got)
        log.info("유튜브 채널 '%s'에 업로드: %s", channel_title, vid)
        return PublishResult(self.name, vid, f"https://youtube.com/shorts/{vid}")

    def insights(self, media_id: str) -> dict[str, int]:
        data = self._check(requests.get(f"{API}/videos", params={"part": "statistics", "id": media_id},
                                        headers=self._headers(), timeout=30))
        items = data.get("items") or []
        st = items[0].get("statistics", {}) if items else {}
        return {"views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                "comments": int(st.get("commentCount", 0))}
