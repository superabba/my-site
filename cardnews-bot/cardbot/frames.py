"""릴스 영상에서 캡처한 장면 이미지 (frame_01.jpg …).

스레드 캐러셀과 네이버 블로그 글에는 카드 슬라이드 대신 릴스 영상의 화면(자막·연출이 입혀진 장면)을 쓴다.
published.json 의 image_urls 가 이 캡처의 공개 주소가 되므로, 그 주소를 읽는 쪽(스레드 게시, 블로그 파일, 네이버 루틴)은
바꾸지 않아도 캡처를 쓴다. 릴스 영상이 없으면 예전처럼 슬라이드(slide_*.jpg)를 쓴다.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

FRAME_GLOB = "frame_*.jpg"
SLIDE_NAME = re.compile(r"^slide_(\d+)\.jpg$")


def _ffmpeg() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _duration(reel: Path) -> float:
    r = subprocess.run([_ffmpeg(), "-i", str(reel)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    if not m:
        raise RuntimeError("릴스 영상 길이를 읽지 못했습니다")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def frame_times(folder: Path, reel: Path) -> list[float]:
    """캡처할 시각(초). 릴스 계획(reel_plan.json)의 frames 를 쓰고, 없으면 영상을 슬라이드 수만큼 고르게 나눈 가운데."""
    plan = folder / "reel_plan.json"
    if plan.exists():
        try:
            times = json.loads(plan.read_text(encoding="utf-8")).get("frames") or []
            if times:
                return [float(t) for t in times]
        except (OSError, ValueError):
            pass
    n = len(list(folder.glob("slide_*.jpg"))) or 6
    d = _duration(reel)
    return [round(0.6 + (d - 1.4) * (i + 0.5) / n, 2) for i in range(n)]


def capture_frames(folder: Path) -> list[Path]:
    """릴스 영상에서 장면 이미지를 캡처해 frame_01.jpg … 로 저장한다. 영상이 없으면 빈 목록."""
    reel = folder / "reel.mp4"
    if not reel.exists():
        return []
    times = frame_times(folder, reel)
    for old in folder.glob(FRAME_GLOB):
        old.unlink()
    out: list[Path] = []
    for i, t in enumerate(times, 1):
        dest = folder / f"frame_{i:02d}.jpg"
        r = subprocess.run(
            [_ffmpeg(), "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(reel), "-frames:v", "1", "-q:v", "2",
             str(dest)], capture_output=True, text=True)
        if r.returncode == 0 and dest.exists() and dest.stat().st_size > 2000:
            out.append(dest)
        else:
            log.warning("장면 %d 캡처 실패(%.1f초): %s", i, t, r.stderr[-200:])
    if len(out) < 2:  # 스레드 캐러셀은 2장 이상이어야 한다 — 부족하면 캡처를 버리고 슬라이드를 쓴다
        for f in out:
            f.unlink()
        return []
    return out


def frames_of(folder: Path) -> list[Path]:
    return sorted(folder.glob(FRAME_GLOB))


def retarget(name: str, count: int) -> str:
    """slide_03.jpg → frame_03.jpg (캡처가 슬라이드보다 적으면 마지막 캡처)."""
    m = SLIDE_NAME.match(name.strip())
    if not m or count < 1:
        return name
    return f"frame_{min(int(m.group(1)), count):02d}.jpg"
