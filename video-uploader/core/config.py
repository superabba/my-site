"""설정·업로드 기록 파일의 위치와 저장/불러오기."""

import json
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
VIDEO_DIR = DATA_DIR / "videos"
WORK_DIR = DATA_DIR / "work"
FRAME_DIR = DATA_DIR / "frames"
SETTINGS_FILE = DATA_DIR / "settings.json"
HISTORY_FILE = DATA_DIR / "history.json"

for _d in (DATA_DIR, VIDEO_DIR, WORK_DIR, FRAME_DIR):
    _d.mkdir(parents=True, exist_ok=True)

CLAUDE_MODELS = {
    "claude-opus-5-5": "Claude Opus 5.5 (기본·가장 정확)",
    "claude-sonnet-5-5": "Claude Sonnet 5.5 (빠르고 저렴)",
    "claude-haiku-5-5": "Claude Haiku 5.5 (가장 저렴)",
}

# 카드뉴스 봇(cardbot.videopost)의 플랫폼 이름
PLATFORM_LABELS = {"reels": "인스타 릴스", "threads": "스레드", "youtube": "유튜브 쇼츠"}
PRIVACY_LABELS = {"public": "공개", "unlisted": "일부 공개", "private": "비공개"}

DEFAULT_SETTINGS = {
    # Claude (영상 내용 분석)
    "anthropic_api_key": "",
    "claude_model": "claude-opus-5-5",
    "whisper_model": "small",
    # GitHub 카드뉴스 봇 저장소
    "github_repo": "superabba/my-site",
    "github_token": "",
    # 업로드 기본값
    "default_hashtags": "",
    "default_platforms": ["reels", "threads", "youtube"],
    "yt_privacy": "public",
}


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return settings


def save_settings(settings: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_history(history: list[dict]) -> None:
    HISTORY_FILE.write_text(json.dumps(history[:500], ensure_ascii=False, indent=2), encoding="utf-8")


def add_history(video_id: str, video_name: str, title: str, platforms: list[str], run_url: str = "") -> None:
    history = load_history()
    history.insert(0, {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "video_id": video_id,
        "video": video_name,
        "title": title,
        "platforms": platforms,
        "status": "진행 중",
        "results": {},
        "run_url": run_url,
    })
    _save_history(history)


def update_history(video_id: str, **fields) -> None:
    history = load_history()
    for entry in history:
        if entry.get("video_id") == video_id:
            entry.update(fields)
            break
    _save_history(history)


def status_from_record(record: dict) -> tuple[str, dict]:
    """봇의 결과 기록 → (상태, {플랫폼: 링크 또는 '실패: 사유'})."""
    results = {}
    for name in record.get("platforms", []):
        if name in record.get("posts", {}):
            info = record["posts"][name]
            results[name] = info.get("permalink") or f"게시됨 (ID {info.get('media_id')})"
        else:
            results[name] = "실패: " + record.get("errors", {}).get(name, "알 수 없는 오류")
    failed = [n for n in record.get("platforms", []) if n not in record.get("posts", {})]
    if not failed:
        return "완료", results
    if len(failed) == len(record.get("platforms", [])):
        return "실패", results
    return "일부 실패", results
