"""Claude 루틴이 쓴 쇼츠 원고(shorts.json) 읽기·검사.

content/<이름>/shorts.json
  {
    "theme": "경제 뉴스 쉽게 풀기",
    "topic": {"topic": "...", "keyword": "...", "angle": "...", "why": "..."},
    "title": "영상 제목(30자 이내)", "mood": "calm|emotional|upbeat|energetic|mysterious",
    "caption": "게시 설명 1~2문장", "hashtags": ["단어", ...5~8개],
    "visual_style": "영어 화풍 한 줄",
    "scenes": [{"narration", "headline"(4~14자), "keywords"(영어), "image_prompt"(영어), "chart": {...}|null}, ...],
    "facts": "조사 자료(팩트 시트) 요약",
    "sources": ["https://...", ...2개 이상],
    "blog": {...선택: 네이버 블로그 글 blog.json 형식...}
  }
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .charts import CHART_TYPES

MOODS = ("calm", "emotional", "upbeat", "energetic", "mysterious")
SCENES_MIN, SCENES_MAX = 5, 9
NARRATION_MIN, NARRATION_MAX = 8, 70
HEADLINE_MIN, HEADLINE_MAX = 3, 16
TOTAL_CHARS_MAX = 330   # 30초 안팎 (공백 제외 약 10자/초)
SOURCE_FILE = "shorts.json"
HANGUL = re.compile(r"[가-힣]")
EMOJI = re.compile("[\U0001F000-\U0001FFFF☀-➿#@*]")


def load(path: Path) -> dict:
    p = path / SOURCE_FILE if path.is_dir() else path
    return json.loads(p.read_text(encoding="utf-8"))


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _chart_errors(i: int, c: dict) -> list[str]:
    e: list[str] = []
    t = c.get("type")
    if t not in CHART_TYPES:
        return [f"장면 {i}: chart.type 은 {', '.join(CHART_TYPES)} 중 하나여야 합니다 ({t!r})"]
    if t == "gauge":
        if not _num(c.get("value")) or not 0 <= c["value"] <= 100:
            e.append(f"장면 {i}: gauge.value 는 0~100 숫자")
        if not c.get("label"):
            e.append(f"장면 {i}: gauge.label 필요")
    elif t == "compare":
        bars = c.get("bars") or []
        if not 2 <= len(bars) <= 3:
            e.append(f"장면 {i}: compare.bars 는 2~3개")
        for b in bars:
            if not _num(b.get("value")) or b["value"] <= 0:
                e.append(f"장면 {i}: compare 막대 value 는 0보다 큰 숫자")
                break
    elif t == "calendar":
        for k in ("date", "event", "forecast"):
            if not str(c.get(k, "")).strip():
                e.append(f"장면 {i}: calendar.{k} 필요")
    elif t == "flow":
        if len(c.get("nodes") or []) != 3 or any(not n.get("title") for n in c["nodes"]):
            e.append(f"장면 {i}: flow.nodes 는 title 이 있는 3개")
    elif t == "impact":
        if len(c.get("steps") or []) != 3 or any(not (s.get("desc") or s.get("title")) for s in c["steps"]):
            e.append(f"장면 {i}: impact.steps 는 desc 가 있는 3개")
    elif t == "stat":
        for k in ("left", "right"):
            if not str(c.get(k, "")).strip():
                e.append(f"장면 {i}: stat.{k} 필요")
    return e


def validate(d: dict) -> list[str]:
    """고쳐야 할 점 목록 (비어 있으면 통과)."""
    e: list[str] = []
    topic = d.get("topic") or {}
    for k in ("topic", "keyword"):
        if not str(topic.get(k, "")).strip():
            e.append(f"topic.{k} 필요")
    title = str(d.get("title", "")).strip()
    if not title or len(title) > 30:
        e.append("title 은 1~30자")
    if d.get("mood") not in MOODS:
        e.append(f"mood 는 {', '.join(MOODS)} 중 하나")
    if not str(d.get("caption", "")).strip():
        e.append("caption 필요")
    tags = d.get("hashtags") or []
    if not 5 <= len(tags) <= 10:
        e.append("hashtags 는 5~10개")
    if not str(d.get("visual_style", "")).strip():
        e.append("visual_style 필요 (영어 한 줄)")
    srcs = [s for s in d.get("sources") or [] if str(s).startswith("http")]
    if len(srcs) < 2:
        e.append("sources 는 http 링크 2개 이상")
    if len(str(d.get("facts", "")).strip()) < 100:
        e.append("facts(조사 자료)가 너무 짧습니다")
    scenes = d.get("scenes") or []
    if not SCENES_MIN <= len(scenes) <= SCENES_MAX:
        e.append(f"scenes 는 {SCENES_MIN}~{SCENES_MAX}개 (현재 {len(scenes)})")
    total = 0
    for i, s in enumerate(scenes, 1):
        n = str(s.get("narration", "")).strip()
        total += len(n.replace(" ", ""))
        if not NARRATION_MIN <= len(n) <= NARRATION_MAX:
            e.append(f"장면 {i}: narration {NARRATION_MIN}~{NARRATION_MAX}자 (현재 {len(n)})")
        if EMOJI.search(n):
            e.append(f"장면 {i}: narration 에 이모지·특수기호(#@*) 금지")
        h = str(s.get("headline", "")).strip()
        if not HEADLINE_MIN <= len(h) <= HEADLINE_MAX:
            e.append(f"장면 {i}: headline {HEADLINE_MIN}~{HEADLINE_MAX}자 (현재 {len(h)})")
        ip = str(s.get("image_prompt", "")).strip()
        if len(ip) < 20 or HANGUL.search(ip):
            e.append(f"장면 {i}: image_prompt 는 영어 20자 이상")
        if s.get("chart"):
            e += _chart_errors(i, s["chart"])
    if total > TOTAL_CHARS_MAX:
        e.append(f"대본이 너무 깁니다: 공백 제외 {total}자 (최대 {TOTAL_CHARS_MAX}자, 30초 안팎)")
    charts = [s for s in scenes if s.get("chart")]
    if not charts:
        e.append("차트가 있는 장면이 하나도 없습니다 (2~4장면에 chart 를 넣어 주세요)")
    if scenes and scenes[0].get("chart") is None and scenes[-1].get("chart") is None and len(charts) < 2:
        e.append("차트는 최소 2장면에 넣어 주세요")
    return e


def content_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / SOURCE_FILE).exists()) if root.is_dir() else []


def main(argv: list[str] | None = None) -> int:
    """python -m shorts.econ.content <폴더>... : shorts.json 검사 (통과 0, 실패 1)"""
    import sys
    paths = [Path(a) for a in (argv if argv is not None else sys.argv[1:])]
    bad = 0
    for p in paths:
        try:
            errs = validate(load(p))
        except (OSError, ValueError) as e:
            errs = [f"읽을 수 없습니다: {e}"]
        print(f"{p.name}: " + ("통과" if not errs else f"{len(errs)}건 고쳐야 합니다"))
        for e in errs:
            print("  -", e)
        bad += bool(errs)
    return 1 if bad or not paths else 0


if __name__ == "__main__":
    raise SystemExit(main())
