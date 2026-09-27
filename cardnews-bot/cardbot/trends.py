"""실시간 트렌드 신호 수집.

- Google Trends '실시간 인기 검색어' RSS (검색량 추정치 포함)
- Google News 주요 뉴스 RSS
- YouTube 인기 동영상 (YOUTUBE_API_KEY가 있을 때)
- 사용자가 지정한 추가 RSS
각 소스는 실패해도 전체 파이프라인을 멈추지 않는다.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable

import requests

log = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (cardnews-bot)"}
HT_NS = "https://trends.google.com/trending/rss"


@dataclass
class TrendSignal:
    keyword: str
    source: str
    traffic: int = 0  # 추정 검색량/조회수 (모르면 0)
    context: list[str] = field(default_factory=list)  # 관련 뉴스 제목 등
    url: str = ""

    def to_prompt_line(self) -> str:
        t = f"{self.traffic:,}+" if self.traffic else "-"
        ctx = " / ".join(self.context[:3])
        return f"- [{self.source}] {self.keyword} (규모: {t}) {('— ' + ctx) if ctx else ''}".rstrip()


def parse_traffic(text: str) -> int:
    """'20,000+', '2만+', '1M+' 같은 표기를 정수로."""
    if not text:
        return 0
    s = text.replace(",", "").replace("+", "").strip().upper()
    mult = 1
    for suffix, m in (("K", 1_000), ("M", 1_000_000), ("만", 10_000), ("천", 1_000)):
        if s.endswith(suffix.upper()):
            mult = m
            s = s[: -len(suffix)]
            break
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def _get(url: str, **kw) -> requests.Response:
    r = requests.get(url, headers=UA, timeout=15, **kw)
    r.raise_for_status()
    return r


def parse_google_trends_rss(xml_text: str) -> list[TrendSignal]:
    root = ET.fromstring(xml_text)
    out: list[TrendSignal] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        traffic = parse_traffic(item.findtext(f"{{{HT_NS}}}approx_traffic") or "")
        news = [
            (n.findtext(f"{{{HT_NS}}}news_item_title") or "").strip()
            for n in item.findall(f"{{{HT_NS}}}news_item")
        ]
        url = ""
        first = item.find(f"{{{HT_NS}}}news_item")
        if first is not None:
            url = (first.findtext(f"{{{HT_NS}}}news_item_url") or "").strip()
        out.append(TrendSignal(title, "google_trends", traffic, [n for n in news if n], url))
    return out


def parse_generic_rss(xml_text: str, source: str) -> list[TrendSignal]:
    root = ET.fromstring(xml_text)
    out: list[TrendSignal] = []
    for item in root.iter("item"):
        title = re.sub(r"\s+", " ", (item.findtext("title") or "")).strip()
        if title:
            out.append(TrendSignal(title, source, 0, [], (item.findtext("link") or "").strip()))
    return out


def google_trends(geo: str) -> list[TrendSignal]:
    return parse_google_trends_rss(_get(f"https://trends.google.com/trending/rss?geo={geo}").text)


def google_news(geo: str) -> list[TrendSignal]:
    hl = "ko" if geo == "KR" else "en"
    url = f"https://news.google.com/rss?hl={hl}&gl={geo}&ceid={geo}:{hl}"
    return parse_generic_rss(_get(url).text, "google_news")[:30]


def youtube_trending(geo: str, api_key: str) -> list[TrendSignal]:
    r = _get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={
            "part": "snippet,statistics",
            "chart": "mostPopular",
            "regionCode": geo,
            "maxResults": 25,
            "key": api_key,
        },
    )
    out = []
    for v in r.json().get("items", []):
        sn = v.get("snippet", {})
        views = int(v.get("statistics", {}).get("viewCount", 0) or 0)
        out.append(
            TrendSignal(
                sn.get("title", ""),
                "youtube",
                views,
                [sn.get("channelTitle", "")],
                f"https://youtu.be/{v.get('id')}",
            )
        )
    return out


def collect(settings) -> list[TrendSignal]:
    jobs: list[tuple[str, Callable[[], list[TrendSignal]]]] = [
        ("google_trends", lambda: google_trends(settings.geo)),
        ("google_news", lambda: google_news(settings.geo)),
    ]
    if settings.youtube_api_key:
        jobs.append(("youtube", lambda: youtube_trending(settings.geo, settings.youtube_api_key)))
    for url in settings.extra_rss:
        jobs.append((url, lambda u=url: parse_generic_rss(_get(u).text, "rss")[:20]))

    signals: list[TrendSignal] = []
    for name, job in jobs:
        try:
            got = job()
            log.info("트렌드 수집 %s: %d건", name, len(got))
            signals.extend(got)
        except Exception as e:  # 한 소스 실패가 전체를 막지 않도록
            log.warning("트렌드 소스 실패 %s: %s", name, e)
    return signals
