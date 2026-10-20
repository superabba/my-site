"""장면 배경: 자동 그라데이션, 사진(천천히 확대·이동), 영상, 그리고 Pixabay·Pexels 무료 스톡 검색."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageOps

from .config import STOCK_DIR
from .ffmpeg import VideoReader

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}

# 분위기별 배경 색 (흰 글씨가 잘 보이도록 어두운 편)
PALETTES = {
    "calm": [(22, 48, 92), (42, 92, 140), (88, 140, 170)],
    "emotional": [(58, 28, 88), (150, 70, 110), (215, 125, 105)],
    "upbeat": [(200, 60, 70), (230, 110, 60), (120, 40, 120)],
    "energetic": [(70, 20, 160), (140, 40, 220), (20, 120, 200)],
    "mysterious": [(10, 24, 32), (28, 58, 72), (60, 50, 110)],
}
ACCENTS = {
    "calm": (255, 214, 102),
    "emotional": (255, 160, 170),
    "upbeat": (255, 225, 77),
    "energetic": (0, 230, 200),
    "mysterious": (170, 140, 255),
}


def media_kind(path: str | Path | None) -> str | None:
    if not path:
        return None
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


class Background:
    def open(self) -> None: ...
    def frame(self, t: float) -> Image.Image: raise NotImplementedError
    def close(self) -> None: ...
    dim = 1.0  # 글씨가 잘 보이도록 어둡게 덮는 정도


class GradientBackground(Background):
    """천천히 움직이는 그라데이션과 은은한 빛망울."""

    dim = 0.35

    def __init__(self, size: tuple[int, int], mood: str, scene_index: int, t_offset: float):
        self.size = size
        cols = PALETTES.get(mood, PALETTES["calm"])
        r = scene_index % len(cols)
        self.colors = np.array(cols[r:] + cols[:r], np.float32)
        self.t_offset = t_offset
        self.lw, self.lh = max(2, size[0] // 4), max(2, size[1] // 4)
        y, x = np.mgrid[0:self.lh, 0:self.lw].astype(np.float32)
        self.x, self.y = x / self.lw, y / self.lh
        rng = np.random.default_rng(scene_index + 7)
        self.blobs = rng.random((6, 4)).astype(np.float32)

    def frame(self, t: float) -> Image.Image:
        g = t + self.t_offset
        ang = 1.1 + 0.35 * math.sin(g * 0.25)
        u = self.x * math.cos(ang) + self.y * math.sin(ang)
        u = (u - u.min()) / (u.max() - u.min() + 1e-6)
        u = np.clip(u + 0.06 * np.sin(self.y * 6 + g * 0.8), 0, 1)
        c0, c1, c2 = self.colors
        a = np.clip(u * 2, 0, 1)[..., None]
        b = np.clip(u * 2 - 1, 0, 1)[..., None]
        img = c0 * (1 - a) + c1 * a
        img = img * (1 - b) + c2 * b
        light = np.zeros_like(u)
        aspect = self.lh / self.lw
        for bx, by, bs, bp in self.blobs:
            cx = (bx + 0.08 * math.sin(g * 0.3 + bp * 6)) % 1.0
            cy = (by + 0.05 * math.cos(g * 0.25 + bp * 6)) % 1.0
            rad = 0.08 + 0.12 * bs
            d2 = (self.x - cx) ** 2 + ((self.y - cy) * aspect) ** 2
            light += np.exp(-d2 / (rad * rad)) * (0.16 + 0.08 * math.sin(g * 0.7 + bp * 9))
        img = img + light[..., None] * 170
        small = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")
        return small.resize(self.size, Image.BILINEAR)


class ImageBackground(Background):
    """사진을 천천히 확대/축소하며 이동(켄 번즈 효과)."""

    ZOOM = 1.12

    def __init__(self, path: str | Path, size: tuple[int, int], duration: float, scene_index: int):
        self.path, self.size, self.duration = path, size, max(duration, 0.1)
        self.zoom_in = scene_index % 2 == 0
        self.pan = [(0.6, 0.0), (-0.6, 0.3), (0.0, -0.6), (0.5, 0.5)][scene_index % 4]
        self.base: Image.Image | None = None

    def open(self) -> None:
        img = ImageOps.exif_transpose(Image.open(self.path)).convert("RGB")
        bw, bh = int(self.size[0] * self.ZOOM), int(self.size[1] * self.ZOOM)
        self.base = ImageOps.fit(img, (bw, bh), Image.LANCZOS)

    def frame(self, t: float) -> Image.Image:
        assert self.base is not None
        p = min(1.0, max(0.0, t / self.duration))
        p = p * p * (3 - 2 * p)  # 부드러운 가감속
        z = 1.0 + (self.ZOOM - 1.0) * (p if self.zoom_in else 1 - p)
        bw, bh = self.base.size
        w, h = bw / z, bh / z
        cx = bw / 2 + self.pan[0] * (bw - w) / 2 * (p - 0.5)
        cy = bh / 2 + self.pan[1] * (bh - h) / 2 * (p - 0.5)
        x0 = min(max(cx - w / 2, 0), bw - w)
        y0 = min(max(cy - h / 2, 0), bh - h)
        return self.base.resize(self.size, Image.BILINEAR, box=(x0, y0, x0 + w, y0 + h))

    def close(self) -> None:
        self.base = None


class VideoBackground(Background):
    def __init__(self, path: str | Path, size: tuple[int, int], fps: int, duration: float):
        self.path, self.size, self.fps, self.duration = path, size, fps, duration
        self.reader: VideoReader | None = None
        self.fallback = Image.new("RGB", size, (20, 20, 20))

    def open(self) -> None:
        self.reader = VideoReader(self.path, self.size[0], self.size[1], self.fps, self.duration)

    def frame(self, t: float) -> Image.Image:
        data = self.reader.read() if self.reader else None
        if data is None:
            return self.fallback
        return Image.frombytes("RGB", self.size, data)

    def close(self) -> None:
        if self.reader:
            self.reader.close()
            self.reader = None


def make_background(media: str | None, size, fps, duration, mood, scene_index, t_offset) -> Background:
    kind = media_kind(media)
    if kind == "image":
        return ImageBackground(media, size, duration, scene_index)
    if kind == "video":
        return VideoBackground(media, size, fps, duration)
    return GradientBackground(size, mood, scene_index, t_offset)


def dim_mask(size: tuple[int, int], strength: float) -> Image.Image:
    """위·아래를 더 어둡게 하는 반투명 마스크 (자막이 잘 보이게)."""
    w, h = size
    y = np.linspace(0, 1, h)
    alpha = np.interp(y, [0, 0.28, 0.5, 0.7, 1.0], [150, 70, 55, 120, 175]) * strength
    col = np.clip(alpha, 0, 255).astype(np.uint8)[:, None]
    return Image.fromarray(np.repeat(col, w, axis=1), "L")


# ---------------------------------------------------------------- 무료 스톡 검색 (Pixabay / Pexels)

class StockError(Exception):
    pass


@dataclass
class StockItem:
    path: Path
    credit: str  # 출처 표시용: "Pixabay · 작가 · 페이지 주소"


def _has_hangul(text: str) -> bool:
    return bool(re.search(r"[가-힣]", text))


def _download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    tmp.replace(dest)
    return dest


def _get_json(provider: str, url: str, params: dict, headers: dict | None = None) -> dict:
    """검색 결과는 24시간 동안 저장해 두고 다시 씁니다 (Pixabay 이용 조건)."""
    digest = hashlib.sha1((url + json.dumps(params, sort_keys=True)).encode("utf-8")).hexdigest()[:16]
    cache = STOCK_DIR / f"search_{digest}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 24 * 3600:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    try:
        r = requests.get(url, params=params, headers=headers or {}, timeout=20)
    except requests.RequestException as e:
        raise StockError(f"{provider} 연결 실패: {e}")
    if r.status_code in (401, 403) or (r.status_code == 400 and "key" in r.text.lower()):
        raise StockError(f"{provider} API 키가 올바르지 않아요.")
    if r.status_code == 429:
        raise StockError(f"{provider} 사용 한도를 넘었어요. 잠시 후 다시 시도해 주세요.")
    if r.status_code != 200:
        raise StockError(f"{provider} 오류 ({r.status_code})")
    data = r.json()
    cache.write_text(json.dumps(data), encoding="utf-8")
    return data


def _pixabay_params(query: str, key: str) -> dict:
    return {"key": key, "q": query[:100], "lang": "ko" if _has_hangul(query) else "en",
            "safesearch": "true", "per_page": 20}


def _pixabay_video(query: str, key: str, min_duration: float, used: set) -> StockItem | None:
    params = _pixabay_params(query, key) | {"video_type": "film"}
    hits = _get_json("Pixabay", "https://pixabay.com/api/videos/", params).get("hits", [])

    def is_portrait(h: dict) -> bool:
        m = h.get("videos", {}).get("medium") or {}
        return (m.get("height") or 0) > (m.get("width") or 0)

    # 검색어와 가장 잘 맞는 상위 결과 안에서만 세로·긴 영상을 먼저 (관련 없는 세로 영상이 끌려오지 않게)
    top = sorted(hits[:5], key=lambda h: (not is_portrait(h), (h.get("duration") or 0) < min_duration))
    hits = top + hits[5:]
    for h in hits:
        if ("pixabay", h["id"]) in used:
            continue
        videos = h.get("videos", {})
        large, medium = videos.get("large") or {}, videos.get("medium") or {}
        # 가로 영상은 가운데를 세로로 잘라 쓰므로, 너무 크지 않으면 고화질(large)을 받습니다
        if large.get("url") and 0 < (large.get("size") or 0) <= 30_000_000:
            pick, label = large, "large"
        elif medium.get("url"):
            pick, label = medium, "medium"
        else:
            continue
        used.add(("pixabay", h["id"]))
        path = _download(pick["url"], STOCK_DIR / f"pixabay_v{h['id']}_{label}.mp4")
        return StockItem(path, f"Pixabay · {h.get('user', '')} · {h.get('pageURL', '')}")
    return None


def _pixabay_photo(query: str, key: str, used: set) -> StockItem | None:
    params = _pixabay_params(query, key) | {"image_type": "photo", "orientation": "vertical"}
    for h in _get_json("Pixabay", "https://pixabay.com/api/", params).get("hits", []):
        if ("pixabay", h["id"]) in used:
            continue
        url = h.get("fullHDURL") or h.get("largeImageURL")
        if not url:
            continue
        used.add(("pixabay", h["id"]))
        path = _download(url, STOCK_DIR / f"pixabay_p{h['id']}.jpg")
        return StockItem(path, f"Pixabay · {h.get('user', '')} · {h.get('pageURL', '')}")
    return None


def _pexels_params(query: str) -> dict:
    return {"query": query, "orientation": "portrait", "per_page": 15,
            "locale": "ko-KR" if _has_hangul(query) else "en-US"}


def _pexels_video(query: str, key: str, min_duration: float, used: set) -> StockItem | None:
    data = _get_json("Pexels", "https://api.pexels.com/videos/search", _pexels_params(query),
                     {"Authorization": key})
    videos = data.get("videos", [])
    videos.sort(key=lambda v: (v.get("duration") or 0) < min_duration)
    for v in videos:
        if ("pexels", v["id"]) in used:
            continue
        files = [f for f in v.get("video_files", [])
                 if f.get("file_type") == "video/mp4" and (f.get("height") or 0) >= (f.get("width") or 1)
                 and (f.get("height") or 0) >= 960]
        if not files:
            continue
        best = min(files, key=lambda f: abs((f.get("height") or 0) - 1920))
        used.add(("pexels", v["id"]))
        path = _download(best["link"], STOCK_DIR / f"pexels_v{v['id']}_{best['id']}.mp4")
        return StockItem(path, f"Pexels · {(v.get('user') or {}).get('name', '')} · {v.get('url', '')}")
    return None


def _pexels_photo(query: str, key: str, used: set) -> StockItem | None:
    data = _get_json("Pexels", "https://api.pexels.com/v1/search", _pexels_params(query),
                     {"Authorization": key})
    for ph in data.get("photos", []):
        if ("pexels", ph["id"]) in used:
            continue
        used.add(("pexels", ph["id"]))
        path = _download(ph["src"]["original"] + "?auto=compress&cs=tinysrgb&h=2200",
                         STOCK_DIR / f"pexels_p{ph['id']}.jpg")
        return StockItem(path, f"Pexels · {ph.get('photographer', '')} · {ph.get('url', '')}")
    return None


def search_stock(kind: str, query: str, keys: dict, min_duration: float, used: set) -> StockItem | None:
    """kind는 'video' 또는 'photo'. keys에 있는 서비스(Pixabay 먼저, 다음 Pexels)로 차례로 찾습니다."""
    providers = [(name, keys.get(name)) for name in ("pixabay", "pexels") if keys.get(name)]
    errors: list[StockError] = []
    for name, key in providers:
        try:
            if name == "pixabay":
                item = (_pixabay_video(query, key, min_duration, used) if kind == "video"
                        else _pixabay_photo(query, key, used))
            else:
                item = (_pexels_video(query, key, min_duration, used) if kind == "video"
                        else _pexels_photo(query, key, used))
            if item:
                return item
        except StockError as e:
            errors.append(e)
    if providers and len(errors) == len(providers):
        raise errors[0]
    return None


def save_upload(data: bytes, name: str, upload_dir: Path) -> Path:
    digest = hashlib.sha1(data).hexdigest()[:16]
    path = upload_dir / f"{digest}{Path(name).suffix.lower()}"
    if not path.exists():
        path.write_bytes(data)
    return path
