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

# 올릴 곳. reels·threads·youtube는 봇의 cardbot.videopost, naver_blog는 bot/naver_blog.py(Uplika)가 올린다.
PLATFORM_LABELS = {"reels": "인스타 릴스", "threads": "스레드", "youtube": "유튜브 쇼츠", "naver_blog": "네이버 블로그"}
PRIVACY_LABELS = {"public": "공개", "unlisted": "일부 공개", "private": "비공개"}
NAVER_OPEN_LABELS = {"public": "전체 공개", "neighbor": "이웃 공개", "mutual": "서로이웃 공개", "private": "비공개"}
# 슈퍼아빠 블로그(@zeusvigor) 카테고리 (2026-10-25 Uplika에서 확인). 블로그에서 바꾸면 설정 화면에서 직접 입력
NAVER_CATEGORIES = [
    "가족기록", "육아, 같이 성장하기", "육아정보", "나들이, 가볼만한곳", "맛집, 카페", "해외여행",
    "일상 / 유용한 정보", "운동일지", "나팔꽃 키우기", "코딩", "시작",
]

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
    "default_platforms": ["reels", "threads", "youtube", "naver_blog"],
    "yt_privacy": "public",
    # 네이버 블로그 (슈퍼아빠, Uplika로 발행)
    "naver_category": "가족기록",
    "naver_open_type": "public",
    "naver_photo_count": 4,  # AI 없이 쓸 때 영상에서 고르게 뽑을 사진 수 (AI 초안은 영상 길이에 맞게 고름)
    "naver_include_video": True,  # 글에 영상도 넣기
    "naver_web_search": True,  # AI 초안을 쓸 때 주제 관련 정보를 웹에서 찾아 보강
}


def clear_working_files() -> int:
    """이 프로그램이 만든 작업용 사본(올린 영상 복사본, 압축본, AI용 장면 사진)을 지운다.

    브라우저로 고른 영상은 처리용으로 data/videos에 복사되는데, GitHub로 보낸 뒤에는 필요 없다.
    사용자의 원본 파일은 이 폴더들 밖에 있으므로 건드리지 않는다. 지운 파일 수를 돌려준다.
    """
    removed = 0
    for folder in (VIDEO_DIR, WORK_DIR, FRAME_DIR):
        for f in folder.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass  # 다른 곳에서 열려 있으면 다음 기회에 지운다
    return removed


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
    names = list(record.get("platforms", []))
    failed = []
    for name in names:
        if name in record.get("posts", {}):
            info = record["posts"][name]
            results[name] = info.get("permalink") or f"게시됨 (ID {info.get('media_id')})"
        else:
            results[name] = "실패: " + record.get("errors", {}).get(name, "알 수 없는 오류")
            failed.append(name)
    blog = record.get("naver_blog")
    if blog:  # 네이버 블로그는 bot/naver_blog.py가 따로 기록한다
        names.append("naver_blog")
        if blog.get("status") == "published":
            results["naver_blog"] = blog.get("permalink") or "발행됨"
        elif blog.get("status") == "failed":
            results["naver_blog"] = "실패: " + (blog.get("error") or "알 수 없는 오류")
            failed.append("naver_blog")
        else:
            results["naver_blog"] = ("Uplika에 전달됨 — PC의 Edge(Uplika 확장)가 켜져 있으면 곧 블로그에 올라갑니다"
                                     " (Uplika 대시보드에서 확인)")
    if not failed:
        return "완료", results
    if len(failed) == len(names):
        return "실패", results
    return "일부 실패", results
