"""같은 계정에 올라가는 다른 저장소가 최근에 쓴 릴스 스타일 읽기 (스타일이 같은 날 겹치지 않게).

카드뉴스(my-site)와 청년 카드뉴스(economypub-youth)는 같은 계정에 올라간다. 각자 '직전 두 편'과 다른 스타일을 고르지만 서로의
스타일은 몰랐다. 상대 저장소(공개)의 최신 릴스 계획(reel_plan.json 의 style)을 읽어 그 스타일도 피한다.
읽지 못하면(네트워크 차단 등) 조용히 건너뛴다 — 이 확인은 있으면 좋은 것이지 필수가 아니다.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

YOUTH_REPO = "https://github.com/superabba/economypub-youth.git"   # 폴더가 맨 위에 있는 drafts 브랜치
CARDNEWS_REPO = "https://github.com/superabba/my-site.git"          # output/ 아래에 폴더가 있는 cardbot-data 브랜치


REF = "refs/stylefeed/other"


def _git(*args: str, timeout: int = 40) -> str:
    r = subprocess.run(["git", "-c", "core.quotePath=false", *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-200:])
    return r.stdout


def latest_style(url: str, branch: str, prefix: str = "", max_age_days: int = 2, now: datetime | None = None,
                 exclude: str = r"^oneoff-") -> str | None:
    """url 저장소 branch 의 prefix 아래 폴더 중 이름 날짜가 가장 최근인 폴더의 릴스 스타일. 못 읽으면 None.
    폴더 이름은 YYYYMMDD-… 로 시작한다. max_age_days 보다 오래된 것은 무시한다."""
    try:
        # FETCH_HEAD 를 건드리지 않게 전용 ref 로 받는다 (워크플로의 다른 git 단계에 영향 없음)
        _git("fetch", "-q", "--depth=1", url, f"+{branch}:{REF}")
        names = [n for n in _git("ls-tree", "--full-tree", "--name-only", REF, *([prefix] if prefix else [])).split("\n") if n.strip()]
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        log.info("상대 저장소 스타일을 읽지 못했습니다(건너뜀): %s", e)
        return None
    today = (now or datetime.now(timezone.utc)).date()
    folders = []
    for n in names:
        base = n.rstrip("/").rsplit("/", 1)[-1]
        m = re.match(r"(\d{4})(\d{2})(\d{2})-", base)
        if not m or re.search(exclude, base):
            continue
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc).date()
        except ValueError:
            continue
        if (today - d) <= timedelta(days=max_age_days):
            folders.append((base, n))
    for base, path in sorted(folders, reverse=True):
        try:
            plan = json.loads(_git("show", f"{REF}:{path}/reel_plan.json"))
            style = str(plan.get("style") or "")
            if style:
                return style
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
            continue
    return None


def other_repo_style(this_repo: str, **kw) -> str | None:
    """this_repo: 'cardnews'(my-site) 또는 'youth'. 상대 저장소의 최신 스타일."""
    if this_repo == "youth":
        return latest_style(CARDNEWS_REPO, "cardbot-data", "output/", **kw)
    return latest_style(YOUTH_REPO, "drafts", "", **kw)


def with_cross_avoid(avoid: tuple[str, ...], cross: str | None, usable: tuple[str, ...]) -> tuple[str, ...]:
    """내 최근 스타일(avoid)에 상대의 최신 스타일(cross)을 더해 피한다. 그러면 쓸 스타일(usable)이 하나도 안 남을 때는
    상대 것은 포기한다 (스타일이 적을 때 둘 다 피하려다 막히지 않게)."""
    if not cross or cross in avoid:
        return tuple(avoid)
    combined = tuple(avoid) + (cross,)
    return combined if any(u not in combined for u in usable) else tuple(avoid)
