"""Instagram Graph API / Threads API 게시(캐러셀·릴스) 및 인사이트 조회.

캐러셀은 3단계로 동작한다.
  1) 이미지마다 캐러셀 아이템 컨테이너 생성 (image_url, is_carousel_item=true)
  2) 캐러셀 컨테이너 생성 (children=아이템 ID들, 캡션/본문)
  3) 컨테이너 게시 (media_publish / threads_publish)
릴스는 영상 컨테이너(media_type=REELS, video_url) 하나를 만들어 처리가 끝나면 게시한다.

publish(assets, text)의 assets:
  image_urls: 슬라이드 이미지 URL 목록
  video_urls: 릴스 영상 URL 후보 목록 (앞쪽부터 시도)
  reel_cover_url: 릴스 커버 이미지 URL
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)


class PublishError(RuntimeError):
    pass


@dataclass
class PublishResult:
    platform: str
    media_id: str
    permalink: str = ""


class GraphClient:
    def __init__(self, base: str, token: str, sleep=time.sleep):
        self.base, self.token, self.sleep = base.rstrip("/"), token, sleep

    def _check(self, r: requests.Response) -> dict:
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:300]}
        if r.status_code >= 400 or "error" in data:
            raise PublishError(f"{r.status_code} {data.get('error', data)}")
        return data

    def post(self, path: str, **params) -> dict:
        params["access_token"] = self.token
        return self._check(requests.post(f"{self.base}/{path.lstrip('/')}", data=params, timeout=60))

    def get(self, path: str, **params) -> dict:
        params["access_token"] = self.token
        return self._check(requests.get(f"{self.base}/{path.lstrip('/')}", params=params, timeout=60))

    def wait_ready(self, container_id: str, field: str, timeout_s: int = 300) -> None:
        """컨테이너가 FINISHED가 될 때까지 대기 (서버가 이미지를 가져가는 시간)."""
        deadline = time.monotonic() + timeout_s
        delay = 3
        while True:
            status = self.get(container_id, fields=field).get(field, "")
            if status == "FINISHED":
                return
            if status in {"ERROR", "EXPIRED"}:
                detail = ""
                try:  # 인스타그램은 status 필드에 오류 설명이 들어 있다
                    detail = self.get(container_id, fields="status").get("status", "")
                except PublishError:
                    pass
                raise PublishError(f"컨테이너 {container_id} 상태 {status} {detail}".strip())
            if time.monotonic() > deadline:
                raise PublishError(f"컨테이너 {container_id} 준비 시간 초과 (상태 {status})")
            self.sleep(delay)
            delay = min(delay * 2, 20)


class InstagramPublisher:
    """Instagram API (Instagram 로그인 방식: graph.instagram.com, 비즈니스/크리에이터 계정)."""

    name = "instagram"
    needs_video = False
    # 인사이트 지표 (API 버전에 따라 이름이 바뀌면 IG_METRICS로 조정)
    metrics = "views,reach,likes,comments,shares,saved"

    def __init__(self, user_id: str, token: str, base: str, sleep=time.sleep):
        if not user_id or not token:
            raise ValueError("IG_USER_ID, IG_ACCESS_TOKEN이 필요합니다")
        self.user_id = user_id
        self.api = GraphClient(base, token, sleep)

    def publish(self, assets: dict, caption: str) -> PublishResult:
        image_urls = assets["image_urls"]
        if not 2 <= len(image_urls) <= 10:
            raise PublishError("인스타그램 캐러셀은 2~10장이어야 합니다")
        children = []
        for url in image_urls:
            c = self.api.post(f"{self.user_id}/media", image_url=url, is_carousel_item="true")
            children.append(c["id"])
        for cid in children:
            self.api.wait_ready(cid, "status_code")
        carousel = self.api.post(
            f"{self.user_id}/media",
            media_type="CAROUSEL",
            children=",".join(children),
            caption=caption,
        )
        self.api.wait_ready(carousel["id"], "status_code")
        media = self.api.post(f"{self.user_id}/media_publish", creation_id=carousel["id"])
        link = ""
        try:
            link = self.api.get(media["id"], fields="permalink").get("permalink", "")
        except PublishError as e:
            log.warning("permalink 조회 실패: %s", e)
        return PublishResult(self.name, media["id"], link)

    def insights(self, media_id: str) -> dict[str, int]:
        data = self.api.get(f"{media_id}/insights", metric=self.metrics)
        return _flatten_insights(data)


class ReelsPublisher:
    """인스타그램 릴스 (같은 Instagram 계정·토큰 사용)."""

    name = "reels"
    needs_video = True
    metrics = "views,reach,likes,comments,shares,saved"

    def __init__(self, user_id: str, token: str, base: str, sleep=time.sleep):
        if not user_id or not token:
            raise ValueError("IG_USER_ID, IG_ACCESS_TOKEN이 필요합니다")
        self.user_id = user_id
        self.api = GraphClient(base, token, sleep)

    def publish(self, assets: dict, caption: str) -> PublishResult:
        urls = assets.get("video_urls") or []
        if not urls:
            raise PublishError("릴스 영상이 없습니다")
        errors = []
        for url in urls:  # 호스팅 URL 후보를 차례로 시도
            params = {"media_type": "REELS", "video_url": url, "caption": caption, "share_to_feed": "true"}
            if assets.get("reel_cover_url"):
                params["cover_url"] = assets["reel_cover_url"]
            try:
                container = self.api.post(f"{self.user_id}/media", **params)
                self.api.wait_ready(container["id"], "status_code", timeout_s=600)
            except PublishError as e:
                log.warning("릴스 영상 URL 실패, 다음 후보 시도: %s (%s)", url, e)
                errors.append(f"{url}: {e}")
                continue
            media = self.api.post(f"{self.user_id}/media_publish", creation_id=container["id"])
            link = ""
            try:
                link = self.api.get(media["id"], fields="permalink").get("permalink", "")
            except PublishError as e:
                log.warning("permalink 조회 실패: %s", e)
            log.info("릴스 게시 성공 (영상 URL: %s)", url)
            return PublishResult(self.name, media["id"], link)
        raise PublishError("릴스 영상을 가져오지 못했습니다 — " + " | ".join(errors))

    def insights(self, media_id: str) -> dict[str, int]:
        data = self.api.get(f"{media_id}/insights", metric=self.metrics)
        return _flatten_insights(data)


class ThreadsPublisher:
    """Threads API (graph.threads.net)."""

    name = "threads"
    needs_video = False
    metrics = "views,likes,replies,reposts,quotes,shares"

    def __init__(self, user_id: str, token: str, base: str, sleep=time.sleep):
        if not user_id or not token:
            raise ValueError("THREADS_USER_ID, THREADS_ACCESS_TOKEN이 필요합니다")
        self.user_id = user_id
        self.api = GraphClient(base, token, sleep)

    def publish(self, assets: dict, text: str) -> PublishResult:
        image_urls = assets["image_urls"]
        if not 2 <= len(image_urls) <= 20:
            raise PublishError("스레드 캐러셀은 2~20장이어야 합니다")
        children = []
        for url in image_urls:
            c = self.api.post(
                f"{self.user_id}/threads", media_type="IMAGE", image_url=url, is_carousel_item="true"
            )
            children.append(c["id"])
        for cid in children:
            self.api.wait_ready(cid, "status")
        carousel = self.api.post(
            f"{self.user_id}/threads",
            media_type="CAROUSEL",
            children=",".join(children),
            text=text[:500],
        )
        self.api.wait_ready(carousel["id"], "status")
        media = self.api.post(f"{self.user_id}/threads_publish", creation_id=carousel["id"])
        link = ""
        try:
            link = self.api.get(media["id"], fields="permalink").get("permalink", "")
        except PublishError as e:
            log.warning("permalink 조회 실패: %s", e)
        return PublishResult(self.name, media["id"], link)

    def insights(self, media_id: str) -> dict[str, int]:
        data = self.api.get(f"{media_id}/insights", metric=self.metrics)
        return _flatten_insights(data)


def _flatten_insights(data: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in data.get("data", []):
        name = m.get("name")
        if "values" in m and m["values"]:
            val = m["values"][0].get("value", 0)
        else:
            val = m.get("total_value", {}).get("value", 0)
        if name:
            out[name] = int(val or 0)
    # 공통 키로 정규화
    if "saved" in out:
        out["saves"] = out.pop("saved")
    return out


def make_publishers(s, sleep=time.sleep) -> list:
    pubs = []
    for p in s.platforms:
        if p == "instagram":
            pubs.append(InstagramPublisher(s.ig_user_id, s.ig_access_token, s.ig_graph_base, sleep))
        elif p == "reels":
            pubs.append(ReelsPublisher(s.ig_user_id, s.ig_access_token, s.ig_graph_base, sleep))
        elif p == "threads":
            pubs.append(ThreadsPublisher(s.threads_user_id, s.threads_access_token, s.threads_graph_base, sleep))
        else:
            raise ValueError(f"알 수 없는 플랫폼: {p}")
    return pubs


def refresh_token(platform: str, token: str) -> dict:
    """장기 토큰(60일) 갱신. 만료 전에 주기적으로 호출해야 한다."""
    if platform == "instagram":
        url = "https://graph.instagram.com/refresh_access_token"
        grant = "ig_refresh_token"
    else:
        url = "https://graph.threads.net/refresh_access_token"
        grant = "th_refresh_token"
    r = requests.get(url, params={"grant_type": grant, "access_token": token}, timeout=30)
    r.raise_for_status()
    return r.json()
