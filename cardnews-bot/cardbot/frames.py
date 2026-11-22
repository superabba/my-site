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
BLOG_GLOB = "blog_*.jpg"
BLOG_SIZE = 1080  # 네이버 블로그 피드 썸네일이 정사각형이라 대표 사진이 잘리지 않도록 1:1 로 만든다
SLIDE_NAME = re.compile(r"^(?:slide|frame)_(\d+)\.jpg$")


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


def blog_images_of(folder: Path) -> list[Path]:
    return sorted(folder.glob(BLOG_GLOB))


def square_fit(img, size: int = BLOG_SIZE):
    """정사각형(1:1)에 그림을 통째로 넣는다. 세로·가로 그림은 흐리게 확대한 같은 그림을 뒤에 깔아 빈 곳을 채운다 (잘리는 곳 없음)."""
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    img = ImageOps.exif_transpose(img).convert("RGB")
    if abs(img.width / img.height - 1) < 0.02:
        return img.resize((size, size), Image.LANCZOS)
    bg = ImageOps.fit(img, (size, size), Image.LANCZOS).filter(ImageFilter.GaussianBlur(size // 22))
    bg = ImageEnhance.Brightness(bg).enhance(0.72)
    fg = ImageOps.contain(img, (size, size), Image.LANCZOS)
    bg.paste(fg, ((size - fg.width) // 2, (size - fg.height) // 2))
    return bg


def make_blog_images(folder: Path) -> list[Path]:
    """블로그용 정사각형 이미지 blog_NN.jpg. 릴스 캡처(frame_*.jpg)가 있으면 그것을, 없으면 슬라이드를 정사각형에 맞춘다."""
    from PIL import Image

    src = frames_of(folder) or sorted(folder.glob("slide_*.jpg"))
    for old in folder.glob(BLOG_GLOB):
        old.unlink()
    out = []
    for i, p in enumerate(src, 1):
        dest = folder / f"blog_{i:02d}.jpg"
        with Image.open(p) as im:
            square_fit(im).save(dest, "JPEG", quality=90, optimize=True)
        out.append(dest)
    return out


def retarget(name: str, count: int) -> str:
    """slide_03.jpg·frame_03.jpg → blog_03.jpg (정사각형 블로그 이미지가 더 적으면 마지막 것)."""
    m = SLIDE_NAME.match(name.strip())
    if not m or count < 1:
        return name
    return f"blog_{min(int(m.group(1)), count):02d}.jpg"
