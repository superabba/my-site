"""환경변수(.env) 기반 설정."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """아주 단순한 .env 로더 (이미 설정된 환경변수는 덮어쓰지 않음)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value[:1] in {'"', "'"}:
            value = value[1:].split(value[0], 1)[0]
        else:
            value = re.split(r"\s+#", value, 1)[0].strip()  # 인라인 주석 제거
        os.environ.setdefault(key.strip(), value)


def _env(name: str, default: str = "") -> str:
    # 빈 문자열도 '미설정'으로 취급 (GitHub Actions에서 없는 변수는 ""로 들어옴)
    return os.environ.get(name, "").strip() or default


def _bool(name: str, default: bool = False) -> bool:
    v = _env(name)
    if not v:
        return default
    return v.lower() in {"1", "true", "yes", "on", "y"}


@dataclass
class Settings:
    # Claude
    anthropic_model: str = "claude-opus-5"
    effort: str = "high"

    # 콘텐츠 방향
    niche: str = "20~40대가 관심 갖는 생활·경제·테크 정보"
    tone: str = "친근하고 명확한 존댓말"
    brand_handle: str = ""
    slides_min: int = 5
    slides_max: int = 8
    theme: str = "midnight"

    # 트렌드 소스
    geo: str = "KR"
    youtube_api_key: str = ""
    extra_rss: list[str] = field(default_factory=list)

    # 이미지 호스팅 (Instagram/Threads API는 공개 URL의 이미지만 받음)
    hosting: str = "github"  # github | s3 | local
    github_token: str = ""
    github_repo: str = ""  # owner/repo
    github_branch: str = "main"
    github_dir: str = "cardnews"
    s3_bucket: str = ""
    s3_endpoint: str = ""
    s3_prefix: str = "cardnews"
    public_base_url: str = ""
    public_dir: str = ""

    # 플랫폼
    platforms: list[str] = field(default_factory=lambda: ["instagram", "threads"])
    ig_user_id: str = ""
    ig_access_token: str = ""
    ig_graph_base: str = "https://graph.instagram.com/v23.0"
    threads_user_id: str = ""
    threads_access_token: str = ""
    threads_graph_base: str = "https://graph.threads.net/v1.0"

    # 자동화
    auto_publish: bool = False
    interval_minutes: int = 240
    posts_per_run: int = 1

    # 경로
    output_dir: Path = ROOT / "output"
    data_dir: Path = ROOT / "data"
    fonts_dir: Path = ROOT / "fonts"

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(ROOT / ".env")
        s = cls()
        s.anthropic_model = _env("ANTHROPIC_MODEL", s.anthropic_model)
        s.effort = _env("CARD_EFFORT", s.effort)
        s.niche = _env("CARD_NICHE", s.niche)
        s.tone = _env("CARD_TONE", s.tone)
        s.brand_handle = _env("BRAND_HANDLE")
        s.slides_min = int(_env("SLIDES_MIN", str(s.slides_min)))
        s.slides_max = min(10, int(_env("SLIDES_MAX", str(s.slides_max))))
        s.theme = _env("CARD_THEME", s.theme)
        s.geo = _env("TREND_GEO", s.geo)
        s.youtube_api_key = _env("YOUTUBE_API_KEY")
        s.extra_rss = [u for u in _env("EXTRA_RSS").split(",") if u.strip()]
        s.hosting = _env("IMAGE_HOSTING", s.hosting).lower()
        s.github_token = _env("GITHUB_TOKEN")
        s.github_repo = _env("GITHUB_REPO")
        s.github_branch = _env("GITHUB_BRANCH", s.github_branch)
        s.github_dir = _env("GITHUB_DIR", s.github_dir)
        s.s3_bucket = _env("S3_BUCKET")
        s.s3_endpoint = _env("S3_ENDPOINT")
        s.s3_prefix = _env("S3_PREFIX", s.s3_prefix)
        s.public_base_url = _env("PUBLIC_BASE_URL").rstrip("/")
        s.public_dir = _env("PUBLIC_DIR")
        plats = _env("PLATFORMS")
        if plats:
            s.platforms = [p.strip().lower() for p in plats.split(",") if p.strip()]
        s.ig_user_id = _env("IG_USER_ID")
        s.ig_access_token = _env("IG_ACCESS_TOKEN")
        s.ig_graph_base = _env("IG_GRAPH_BASE", s.ig_graph_base).rstrip("/")
        s.threads_user_id = _env("THREADS_USER_ID")
        s.threads_access_token = _env("THREADS_ACCESS_TOKEN")
        s.threads_graph_base = _env("THREADS_GRAPH_BASE", s.threads_graph_base).rstrip("/")
        s.auto_publish = _bool("AUTO_PUBLISH", False)
        s.interval_minutes = int(_env("INTERVAL_MINUTES", str(s.interval_minutes)))
        s.posts_per_run = int(_env("POSTS_PER_RUN", str(s.posts_per_run)))
        s.output_dir = Path(_env("CARDBOT_OUTPUT_DIR", str(s.output_dir)))
        s.data_dir = Path(_env("CARDBOT_DATA_DIR", str(s.data_dir)))
        for d in (s.output_dir, s.data_dir, s.fonts_dir):
            d.mkdir(parents=True, exist_ok=True)
        return s
