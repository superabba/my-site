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


BLOG_W = 1080
BLOG_H = 1350  # 4:5 - 영상 캡처(1080x1920)에서 글자가 있는 부분만 가로 꽉 차게 잘라 읽기 쉽게
HERO_OUTSIDE_MAX = 0.22  # 정사각형 창 밖에 남는 글자·그림 비율이 이 이하일 때만 정사각형으로 자른다


def _row_energy(img):
    """줄(가로 한 줄)마다 그림이 얼마나 '있는지' - 이웃 픽셀 차이의 합. 빈 배경은 0 에 가깝다."""
    import numpy as np
    from PIL import ImageOps

    g = np.asarray(ImageOps.grayscale(img.resize((270, max(1, round(270 * img.height / img.width))))), dtype=np.float32)
    dx = np.abs(np.diff(g, axis=1)).sum(axis=1)
    dy = np.abs(np.diff(g, axis=0)).sum(axis=1)
    e = dx[:-1] + dy
    e = np.append(e, e[-1:])
    return e / max(1, g.shape[1])


def _best_window(img, h_px: int):
    """세로 길이 h_px 짜리 창을 위아래로 밀어 글자·그림이 가장 많이 들어오는 자리를 찾는다. (top, 창 밖 비율) 반환."""
    import numpy as np

    e = _row_energy(img)
    n = len(e)
    win = max(1, round(h_px * n / img.height))
    if win >= n:
        return 0, 0.0
    c = np.concatenate([[0.0], np.cumsum(e)])
    sums = c[win:] - c[:-win]
    k = int(np.argmax(sums))
    total = float(c[-1]) or 1.0
    return round(k * img.height / n), 1.0 - float(sums[k]) / total


def content_crop(img, width: int = BLOG_W, height: int = BLOG_H):
    """세로로 긴 캡처를 width x height 로 자른다 (가로는 그대로, 글자가 모인 세로 구간만). 가로·정사각형 그림은 그대로 맞춘다."""
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(img).convert("RGB")
    if img.height / img.width <= height / width + 0.02:
        return square_fit(img, width) if abs(img.width / img.height - 1) > 0.02 and img.width >= img.height else img.resize((width, round(width * img.height / img.width)), Image.LANCZOS)
    scaled = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    top, _ = _best_window(scaled, height)
    return scaled.crop((0, top, width, top + height))


def hero_crop(img, size: int = BLOG_W):
    """블로그 첫 사진: 네이버 피드가 1:1 로 잘라 보여 주므로 정사각형. 글자가 모인 구간이 정사각형 안에 들어오면 그 구간을 가로 꽉 차게 자르고,
    아니면 (잘릴 수 있으면) 그림 전체를 정사각형 안에 넣는다."""
    from PIL import ImageOps

    img = ImageOps.exif_transpose(img).convert("RGB")
    if abs(img.width / img.height - 1) < 0.02:
        return square_fit(img, size)
    if img.height > img.width:
        scaled = img.resize((size, round(img.height * size / img.width)))
        top, outside = _best_window(scaled, size)
        if outside <= HERO_OUTSIDE_MAX:
            return scaled.crop((0, top, size, top + size))
    return square_fit(img, size)


def make_blog_images(folder: Path) -> list[Path]:
    """블로그용 이미지 blog_NN.jpg. 릴스 캡처(frame_*.jpg)가 있으면 그것을, 없으면 슬라이드를 쓴다.
    첫 장은 1:1(네이버 피드 썸네일이 안 잘리게), 나머지는 글자 있는 부분만 자른 4:5."""
    from PIL import Image

    src = frames_of(folder) or sorted(folder.glob("slide_*.jpg"))
    for old in folder.glob(BLOG_GLOB):
        old.unlink()
    out = []
    for i, p in enumerate(src, 1):
        dest = folder / f"blog_{i:02d}.jpg"
        with Image.open(p) as im:
            (hero_crop(im) if i == 1 else content_crop(im)).save(dest, "JPEG", quality=90, optimize=True)
        out.append(dest)
    return out


def retarget(name: str, count: int) -> str:
    """slide_03.jpg·frame_03.jpg → blog_03.jpg (정사각형 블로그 이미지가 더 적으면 마지막 것)."""
    m = SLIDE_NAME.match(name.strip())
    if not m or count < 1:
        return name
    return f"blog_{min(int(m.group(1)), count):02d}.jpg"
