"""Claude 루틴이 써 둔 원고(cardnews-bot/content/<폴더>/)를 읽는 도구. LLM(API) 호출은 없다.

원고 폴더(루틴이 올림):
  topic.json  주제 (TopicIdea)           card.json  슬라이드·캡션·해시태그·스레드 글·출처 (CardNews)
  reel.json   릴스 연출 (style, reason, lines[{slide,text}])      blog.json  블로그 글 (BlogPost, 기존 형식 그대로)
  research.md (선택) 조사 메모
Pipeline.ingest 가 이 폴더를 기존 초안 폴더(카드 이미지·릴스·블로그 파일)로 바꾼다.
"""

from __future__ import annotations

from pathlib import Path

from . import lemoreel
from .config import ROOT

CONTENT_ROOT = ROOT / "content"
SOURCE_FILE = "source.txt"  # 초안 폴더에 남기는 원본 원고 폴더 이름 (같은 원고를 두 번 만들지 않게)


class FixedWriter:
    """LLM 대신 reel.json에 써 둔 연출·내레이션을 돌려주는 작가. lemoreel.plan_reel이 부르는 _parse만 흉내 낸다."""

    def __init__(self, plan: lemoreel.ReelPlan):
        self.plan = plan

    def _parse(self, system: str, user: str, schema):
        if schema is lemoreel.ReelPlan:
            return self.plan
        raise RuntimeError(f"루틴 방식에는 LLM이 없습니다 (요청한 형식: {getattr(schema, '__name__', schema)})")


def read_reel_plan(path: Path) -> lemoreel.ReelPlan:
    import json

    raw = json.loads(path.read_text(encoding="utf-8"))
    return lemoreel.ReelPlan(
        style=raw["style"], reason=raw.get("reason", ""), card_mid=raw.get("card_mid", ""), card_last=raw.get("card_last", ""),
        lines=[lemoreel.NarrationLine(slide=int(x["slide"]), text=str(x["text"])) for x in raw["lines"]],
    )


def is_content_dir(d: Path) -> bool:
    return d.is_dir() and (d / "card.json").exists() and (d / "topic.json").exists()


def content_dirs(root: Path = CONTENT_ROOT) -> list[Path]:
    return [d for d in sorted(root.iterdir()) if is_content_dir(d)] if root.exists() else []


def choose_style(want: str, recent: tuple[str, ...], topic, card) -> str:
    """원고가 고른 스타일을 쓰되, 직전 영상들과 같거나 비트 스타일(컷 구성이 다름)이면 규칙으로 다른 스타일을 고른다.
    비트가 아닌 스타일은 슬라이드마다 내레이션 한 줄을 똑같이 쓰므로 스타일만 바꿔도 안전하다."""
    beats = tuple(n for n, st in lemoreel.STYLES.items() if st.beats)
    if want in lemoreel.STYLES and want not in tuple(recent) + beats:
        return want
    return lemoreel.heuristic_style(topic, card, avoid=tuple(recent) + beats)
