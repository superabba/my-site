"""경로와 사용자 설정(API 키 등)을 관리합니다."""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# GitHub Actions(매일 자동 발행)에서는 환경변수로 작업 폴더를 바꿀 수 있다
OUTPUT_DIR = Path(os.environ.get("SHORTS_OUTPUT_DIR") or ROOT / "output")
CACHE_DIR = Path(os.environ.get("SHORTS_CACHE_DIR") or ROOT / "cache")
UPLOAD_DIR = CACHE_DIR / "uploads"
STOCK_DIR = CACHE_DIR / "stock"
WORK_DIR = CACHE_DIR / "work"
MUSIC_DIR = ROOT / "assets" / "music"
FONT_DIR = ROOT / "assets" / "fonts"
SETTINGS_FILE = ROOT / "settings.json"

for _d in (OUTPUT_DIR, UPLOAD_DIR, STOCK_DIR, WORK_DIR, MUSIC_DIR, FONT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

MODELS = {
    "Claude Sonnet 5.5 (기본·빠르고 저렴)": "claude-sonnet-5-5",
    "Claude Opus 5.5 (가장 좋은 품질)": "claude-opus-5-5",
}

DEFAULT_SETTINGS = {
    "anthropic_api_key": "",
    "pixabay_api_key": "",
    "pexels_api_key": "",
    "gemini_api_key": "",
    "image_model": "gemini-nano-banana-2.1",
    "image_size": "2K",
    "model": "claude-sonnet-5-5",
    # 발행: 카드뉴스 봇 저장소 (video-uploader와 같은 저장소·토큰)
    "github_repo": "superabba/my-site",
    "github_token": "",
    "default_platforms": ["reels", "threads", "youtube"],
    "yt_privacy": "public",
}


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return settings


def save_settings(settings: dict) -> None:
    SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def anthropic_key(settings: dict) -> str:
    return settings.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")


def gemini_key(settings: dict) -> str:
    return settings.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY", "")


def stock_keys(settings: dict) -> dict:
    """스톡 검색에 쓸 수 있는 키들. 빈 값은 뺍니다."""
    keys = {
        "pixabay": settings.get("pixabay_api_key") or os.environ.get("PIXABAY_API_KEY", ""),
        "pexels": settings.get("pexels_api_key") or os.environ.get("PEXELS_API_KEY", ""),
    }
    return {k: v for k, v in keys.items() if v}
