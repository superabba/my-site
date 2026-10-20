"""매일 자동 쇼츠: 오늘의 경제 테마 → 인기 키워드 고르기 → 웹 조사 → 대본(존댓말·30초) → 나노바나나 그림 → 영상.

GitHub Actions의 shorts-daily.yml이 부른다. 게시는 카드뉴스 봇의 cardbot.shortspost가 한다 (경제주막 계정, 안전장치 포함).
  python -m shorts.daily check --records-dir R [--hours 6-11]   # 오늘 이미 게시했거나 시간대 밖이면 종료 코드 10
  python -m shorts.daily make --out DIR --records-dir R [--cardnews-db DB] [--theme 이름] [--topic 주제]
  python -m shorts.daily wait --until 07:10                     # 그 시각(KST)까지 기다림
make는 DIR에 id.txt, video.mp4(필요하면 meta.mp4), post.json, script.json, summary.md를 만든다.

카드뉴스와 겹치지 않게: 카드뉴스 이력 DB는 읽기만 해서 최근 7일 카드뉴스 주제를 피하고, 카드뉴스 쪽 데이터에는 쓰지 않는다.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import anthropic
import requests

from . import aiimage
from .publish import META_MAX_BYTES, YOUTUBE_MAX_BYTES, transcode
from .render import RenderOptions, render
from .writer import WriterError, _call_json, write_script

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
SKIP_CODE = 10
DISCLAIMER = "※ 정보 제공용이며 특정 상품의 투자 권유가 아닙니다."
UA = {"User-Agent": "Mozilla/5.0 (shorts-daily)"}

# (테마 이름, 설명, 뉴스 검색어) — 날짜마다 차례로 돌아간다
THEMES = [
    ("경제 뉴스 쉽게 풀기", "금리·환율·물가·경기처럼 요즘 뉴스에 나오는 경제 이슈를 직장인 눈높이로 설명",
     ["기준금리", "환율", "물가"]),
    ("직장인 재테크", "월급 관리, 비상금, 예적금·파킹통장, 사회초년생 돈 모으기", ["직장인 재테크", "적금 금리", "파킹통장"]),
    ("세금·연말정산", "연말정산, 종합소득세, 공제·환급 같은 절세 정보", ["연말정산", "세금 환급", "절세"]),
    ("투자 기초", "ETF, 연금저축·IRP, ISA 같은 투자 상품과 제도의 기본 (특정 종목 추천 금지)", ["ETF", "연금저축", "ISA"]),
    ("부동산·청약·전월세", "청약 제도, 전월세 계약, 대출 규제, 주거 지원 정책", ["청약", "전세", "주택담보대출"]),
    ("소비·절약 꿀팁", "카드 혜택, 구독료·통신비 절약, 생활비 줄이는 방법", ["카드 혜택", "생활비 절약", "통신비"]),
    ("정부 지원·제도 변화", "청년·직장인 지원금, 새로 바뀌는 경제 제도와 신청 방법", ["지원금 신청", "청년 지원", "제도 변경"]),
]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def now_kst() -> datetime:
    return datetime.now(KST)


def themes() -> list[tuple[str, str, list[str]]]:
    """SHORTS_THEMES 변수(한 줄에 '이름: 설명')가 있으면 그것을 쓴다."""
    custom = [line.strip() for line in re.split(r"[\n|]", _env("SHORTS_THEMES")) if line.strip()]
    if not custom:
        return THEMES
    out = []
    for line in custom:
        name, _, desc = line.partition(":")
        out.append((name.strip(), desc.strip() or name.strip(), [name.strip()]))
    return out


def theme_for(day: date) -> tuple[str, str, list[str]]:
    items = themes()
    return items[day.toordinal() % len(items)]


# ---------------------------------------------------------------- 기록 확인

def _records(records_dir: Path | None) -> list[dict]:
    out = []
    if records_dir and records_dir.is_dir():
        for p in records_dir.glob("*.json"):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return out


def published_today(records_dir: Path | None, today: date) -> bool:
    """오늘(KST) 자동 쇼츠가 한 곳에라도 게시됐는지."""
    for rec in _records(records_dir):
        if not rec.get("daily"):
            continue
        for info in (rec.get("posts") or {}).values():
            try:
                if datetime.fromisoformat(info["published_at"]).astimezone(KST).date() == today:
                    return True
            except (KeyError, TypeError, ValueError):
                continue
    return False


def recent_shorts_topics(records_dir: Path | None, days: int = 30) -> list[str]:
    since = now_kst() - timedelta(days=days)
    topics = []
    for rec in _records(records_dir):
        when = rec.get("created_at") or ""
        try:
            created = datetime.fromisoformat(when)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if created >= since and (rec.get("topic") or rec.get("title")):
            topics.append(rec.get("topic") or rec.get("title"))
    return topics


def cardnews_topics(db_path: Path | None, days: int = 7) -> list[str]:
    """카드뉴스 이력 DB에서 최근 주제를 읽기만 한다 (쓰기 금지)."""
    if not db_path or not Path(db_path).exists():
        return []
    try:
        con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
        try:
            since = (datetime.now() - timedelta(days=days)).isoformat()
            rows = con.execute("SELECT DISTINCT topic FROM posts WHERE created_at >= ?", (since,)).fetchall()
        finally:
            con.close()
        return [r[0] for r in rows if r[0]]
    except sqlite3.Error as e:
        log.warning("카드뉴스 이력을 읽지 못했습니다: %s", e)
        return []


# ---------------------------------------------------------------- 트렌드 신호

def _rss_titles(url: str, limit: int = 12) -> list[str]:
    try:
        r = requests.get(url, headers=UA, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.text)
    except (requests.RequestException, ET.ParseError) as e:
        log.warning("RSS 실패 %s: %s", url, e)
        return []
    titles = []
    for item in root.iter("item"):
        t = re.sub(r"\s+", " ", item.findtext("title") or "").strip()
        if t:
            titles.append(t)
    return titles[:limit]


def collect_signals(queries: list[str]) -> list[str]:
    lines: list[str] = []
    for q in queries:
        url = f"https://news.google.com/rss/search?q={quote(q + ' when:3d')}&hl=ko&gl=KR&ceid=KR:ko"
        lines += [f"[뉴스·{q}] {t}" for t in _rss_titles(url, 8)]
    lines += [f"[경제 헤드라인] {t}" for t in _rss_titles(
        "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=ko&gl=KR&ceid=KR:ko", 15)]
    lines += [f"[실시간 검색어] {t}" for t in _rss_titles("https://trends.google.com/trending/rss?geo=KR", 20)]
    return lines


# ---------------------------------------------------------------- Claude

TOPIC_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
        "keyword": {"type": "string"},
        "angle": {"type": "string"},
        "why": {"type": "string"},
    },
    "required": ["topic", "keyword", "angle", "why"],
    "additionalProperties": False,
}

TOPIC_SYSTEM = """당신은 경제 정보 채널 '경제주막'의 쇼츠 기획자입니다. 20~40대 직장인이 '저장해 두고 싶은' 30초 경제 쇼츠 주제를 고릅니다.
- 오늘의 테마 안에서, 지금 사람들이 많이 찾는 키워드(뉴스·검색어 신호)를 하나 골라 구체적인 주제로 만드세요.
- 최근에 다룬 주제와 겹치지 않게 하세요.
- 특정 종목·코인 추천, 수익 보장, 근거 없는 루머, 정치적 편가르기, 사건·사고 피해자 소비는 피하세요."""

RESEARCH_SYSTEM = """당신은 경제 기자입니다. 주어진 주제로 30초 쇼츠 대본을 쓰기 위한 팩트 시트를 만듭니다.
웹 검색으로 최신 공식 자료(정부·한국은행·금융감독원·국세청 등)와 주요 언론을 확인하고,
핵심 사실 5~8개를 한국어 글머리표로 정리하세요. 각 항목에 숫자·기준 날짜·출처 이름을 붙이고,
확인되지 않은 내용은 쓰지 마세요. 마지막 줄에 '주의할 점' 한 줄을 덧붙이세요."""


def pick_topic(api_key: str, model: str, theme: tuple[str, str, list[str]], signals: list[str],
               avoid: list[str]) -> dict:
    name, desc, _ = theme
    user = (
        f"오늘 날짜: {now_kst():%Y-%m-%d}\n오늘의 테마: {name} — {desc}\n\n"
        "## 요즘 신호 (뉴스 제목·실시간 검색어)\n" + ("\n".join(signals[:120]) or "(수집 실패)") + "\n\n"
        "## 최근 다룬 주제 (중복 금지)\n" + ("\n".join(f"- {t}" for t in avoid) or "(없음)") + "\n\n"
        "오늘 만들 쇼츠 주제 하나를 고르세요. topic은 25자 이내, keyword는 핵심 검색어, "
        "angle은 어떤 관점으로 설명할지 한 문장, why는 고른 이유 한 줄."
    )
    return _call_json(api_key, model, TOPIC_SYSTEM, user, TOPIC_SCHEMA, effort="low")


def research(api_key: str, model: str, topic: dict) -> str:
    """웹 검색으로 팩트 시트를 만든다. 실패하면 빈 문자열."""
    client = anthropic.Anthropic(api_key=api_key)
    kw: dict = {"model": model, "max_tokens": 16000, "output_config": {"effort": "medium"}}
    if model in ("claude-opus-5-5", "claude-sonnet-5-5"):
        kw.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 6}]
    messages: list = [{"role": "user", "content": (
        f"주제: {topic['topic']}\n키워드: {topic['keyword']}\n관점: {topic['angle']}\n"
        f"오늘 날짜: {now_kst():%Y-%m-%d}\n이 주제의 팩트 시트를 만들어 주세요.")}]
    try:
        resp = None
        for _ in range(4):  # 서버 도구가 길어지면 pause_turn으로 끊겨 오므로 이어서 요청
            resp = client.beta.messages.create(**kw, system=RESEARCH_SYSTEM, messages=messages, tools=tools)
            if resp.stop_reason != "pause_turn":
                break
            messages = [*messages, {"role": "assistant", "content": resp.content}]
    except anthropic.APIError as e:
        log.warning("자료 조사 실패: %s", e)
        return ""
    if resp is None or resp.stop_reason == "refusal":
        return ""
    return "".join(b.text for b in resp.content if b.type == "text").strip()


# ---------------------------------------------------------------- 만들기

def new_id() -> str:
    return f"{now_kst():%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"


def build_post(script: dict, topic: dict, theme_name: str, ai_images: int, credits: list[str],
               files: dict[str, str]) -> dict:
    caption = (script.get("caption") or topic["topic"]).strip()
    tags = []
    for t in [*script.get("hashtags", []), "경제주막", "경제", "재테크"]:
        t = t.lstrip("#").strip()
        if t and t not in tags:
            tags.append(t)
    yt_desc = f"{caption}\n\n{DISCLAIMER}"
    if credits:
        yt_desc += "\n\n배경 영상 출처\n" + "\n".join(credits)
    return {
        "title": script.get("title") or topic["topic"],
        "youtube_description": yt_desc,
        "instagram_caption": f"{caption}\n\n{DISCLAIMER}",
        "threads_text": f"{caption}\n\n{DISCLAIMER}"[:500],
        "hashtags": tags[:12],
        "platforms": [p.strip() for p in _env("SHORTS_DAILY_PLATFORMS", "reels,threads,youtube").split(",")
                      if p.strip()],
        "youtube_privacy": _env("SHORTS_YOUTUBE_PRIVACY", "public").lower(),
        "youtube_synthetic": ai_images > 0,
        "files": files,
        "daily": True,
        "topic": topic["topic"],
        "keyword": topic["keyword"],
        "theme": theme_name,
    }


def make(out: Path, records_dir: Path | None, cardnews_db: Path | None, theme_name: str = "",
         topic_text: str = "") -> dict:
    out.mkdir(parents=True, exist_ok=True)
    api_key = _env("ANTHROPIC_API_KEY")
    if not api_key:
        raise WriterError("ANTHROPIC_API_KEY가 없습니다.")
    model = _env("SHORTS_MODEL", "claude-opus-5-5")
    today = now_kst().date()
    theme = next((t for t in themes() if t[0] == theme_name), None) or theme_for(today)
    log.info("오늘의 테마: %s", theme[0])

    if topic_text:
        topic = {"topic": topic_text, "keyword": topic_text, "angle": "핵심만 쉽게 설명", "why": "직접 지정"}
    else:
        signals = collect_signals(theme[2])
        log.info("신호 %d건 수집", len(signals))
        avoid = recent_shorts_topics(records_dir) + cardnews_topics(cardnews_db)
        topic = pick_topic(api_key, model, theme, signals, avoid)
    log.info("주제: %s (키워드 %s)", topic["topic"], topic["keyword"])

    facts = research(api_key, model, topic)
    log.info("팩트 시트 %d자", len(facts))
    script = write_script(
        api_key, model, f"{topic['topic']} — {topic['angle']}", "정보", "존댓말 (~해요)",
        int(_env("SHORTS_LENGTH", "30")),
        notes=(f"경제 정보 채널 '경제주막'의 쇼츠. 오늘의 테마: {theme[0]}. "
               "첫 장면은 질문이나 의외의 사실로 시작하고, 마지막 장면은 저장·팔로우를 권하는 한 문장으로 끝내 주세요."),
        facts=facts,
    )
    if not script["scenes"]:
        raise WriterError("대본이 비어 있습니다.")

    stock = {"pixabay": _env("PIXABAY_API_KEY")} if _env("PIXABAY_API_KEY") else {}
    opts = RenderOptions(
        bg_mode="ai_image",
        gemini_key=_env("SHORTS_GEMINI_API_KEY") or _env("GEMINI_API_KEY"),
        image_model=_env("SHORTS_IMAGE_MODEL", aiimage.DEFAULT_MODEL),
        image_size=_env("SHORTS_IMAGE_SIZE", aiimage.DEFAULT_SIZE),
        image_fallback="stock_video" if stock else "gradient",
        stock_keys=stock,
        voice=_env("SHORTS_VOICE", "ko-KR-SunHiNeural"),
        speed=int(_env("SHORTS_SPEED", "10")),
        output_dir=out / "render",
    )
    last = [0.0]

    def progress(p: float, msg: str) -> None:
        if p - last[0] >= 0.1 or p >= 1:
            log.info("%3d%% %s", int(p * 100), msg)
            last[0] = p

    res = render(script, opts, progress)

    video = out / "video.mp4"
    shutil.copyfile(res.video, video)
    files = {"youtube": "video.mp4", "meta": "video.mp4"}
    if video.stat().st_size > YOUTUBE_MAX_BYTES:
        transcode(res.video, video, YOUTUBE_MAX_BYTES, 12000)
    if video.stat().st_size > META_MAX_BYTES:
        transcode(video, out / "meta.mp4", META_MAX_BYTES, 8000)
        files["meta"] = "meta.mp4"

    post = build_post(script, topic, theme[0], res.ai_images, res.credits, files)
    vid = new_id()
    (out / "id.txt").write_text(vid, encoding="utf-8")
    (out / "post.json").write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "script.json").write_text(json.dumps({**script, "facts": facts, "topic": topic},
                                                ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"## 오늘의 쇼츠: {post['title']}", "",
             f"- 테마: {theme[0]}", f"- 주제: {topic['topic']} (키워드 {topic['keyword']})",
             f"- 길이: {res.duration:.1f}초 · AI 그림 {res.ai_images}/{len(script['scenes'])}장",
             "", "### 대본"] + [f"{i + 1}. {s['narration']}" for i, s in enumerate(script["scenes"])]
    if res.warnings:
        lines += ["", "### 경고"] + [f"- {w}" for w in res.warnings]
    if facts:
        lines += ["", "<details><summary>팩트 시트</summary>", "", facts, "", "</details>"]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("완성: %s (%.1f초, AI 그림 %d장)", post["title"], res.duration, res.ai_images)
    return post


# ---------------------------------------------------------------- 명령

def _hours_ok(spec: str, hour: int) -> bool:
    if not spec:
        return True
    lo, _, hi = spec.partition("-")
    return int(lo) <= hour <= int(hi or lo)


def wait_until(hhmm: str, max_wait_s: int = 3600, sleep=time.sleep) -> float:
    h, m = (int(x) for x in hhmm.split(":"))
    now = now_kst()
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    wait = (target - now).total_seconds()
    if 0 < wait <= max_wait_s:
        log.info("%s(KST)까지 %d초 기다립니다", hhmm, int(wait))
        sleep(wait)
        return wait
    return 0.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="shorts.daily", description="매일 자동 경제 쇼츠 만들기")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--records-dir", type=Path)
    c.add_argument("--hours", default="", help="이 KST 시간대(예: 6-11)가 아니면 건너뜀")
    m = sub.add_parser("make")
    m.add_argument("--out", type=Path, required=True)
    m.add_argument("--records-dir", type=Path)
    m.add_argument("--cardnews-db", type=Path)
    m.add_argument("--theme", default="")
    m.add_argument("--topic", default="")
    w = sub.add_parser("wait")
    w.add_argument("--until", default="07:10")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if a.cmd == "check":
        now = now_kst()
        if not _hours_ok(a.hours, now.hour):
            print(f"지금은 {now:%H:%M}(KST)이라 예약 시간대({a.hours}시) 밖입니다. 건너뜁니다.")
            return SKIP_CODE
        if published_today(a.records_dir, now.date()):
            print("오늘 자동 쇼츠를 이미 게시했습니다. 건너뜁니다.")
            return SKIP_CODE
        print(f"오늘의 테마: {theme_for(now.date())[0]}")
        return 0
    if a.cmd == "wait":
        wait_until(a.until)
        return 0
    try:
        make(a.out, a.records_dir, a.cardnews_db, a.theme, a.topic)
    except WriterError as e:
        print(f"만들기 실패: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
