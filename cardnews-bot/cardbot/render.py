"""카드뉴스 슬라이드 이미지 렌더링 (1080x1350, 인스타그램 4:5)."""

from __future__ import annotations

import logging
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

from .llm import CardNews, Slide

log = logging.getLogger(__name__)

W, H = 1080, 1350
PAD = 96

FONT_URL = (
    "https://raw.githubusercontent.com/orioncactus/pretendard/v1.3.9/"
    "packages/pretendard/dist/public/static/Pretendard-{w}.otf"
)
WEIGHTS = ("Regular", "Bold", "ExtraBold")
SYSTEM_FALLBACKS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "C:/Windows/Fonts/malgunbd.ttf",
]

THEMES: dict[str, dict[str, tuple[int, int, int]]] = {
    "midnight": {"bg1": (17, 24, 39), "bg2": (30, 41, 82), "fg": (248, 250, 252), "sub": (165, 180, 204), "accent": (250, 204, 21)},
    "cream": {"bg1": (250, 246, 238), "bg2": (243, 234, 218), "fg": (28, 25, 23), "sub": (100, 90, 80), "accent": (234, 88, 12)},
    "mint": {"bg1": (236, 253, 245), "bg2": (209, 250, 229), "fg": (6, 46, 38), "sub": (55, 100, 88), "accent": (5, 150, 105)},
    "coral": {"bg1": (255, 241, 236), "bg2": (255, 222, 211), "fg": (60, 20, 20), "sub": (130, 80, 70), "accent": (225, 29, 72)},
}


# ---------- 폰트 ----------


def ensure_fonts(fonts_dir: Path) -> dict[str, Path | None]:
    """Pretendard(OFL)를 내려받아 두고, 실패하면 시스템 한글 폰트를 찾는다."""
    fonts_dir.mkdir(parents=True, exist_ok=True)
    found: dict[str, Path | None] = {}
    for w in WEIGHTS:
        p = fonts_dir / f"Pretendard-{w}.otf"
        if not p.exists():
            try:
                r = requests.get(FONT_URL.format(w=w), timeout=30)
                r.raise_for_status()
                p.write_bytes(r.content)
            except Exception as e:
                log.warning("폰트 다운로드 실패(%s): %s", w, e)
        found[w] = p if p.exists() else None
    if not all(found.values()):
        sys_font = next((Path(f) for f in SYSTEM_FALLBACKS if Path(f).exists()), None)
        if sys_font is None:
            log.warning("한글 폰트를 찾지 못했습니다. fonts/ 폴더에 Pretendard-*.otf를 넣어주세요.")
        for w in WEIGHTS:
            found[w] = found[w] or sys_font
    return found


class Fonts:
    def __init__(self, paths: dict[str, Path | None]):
        self.paths = paths
        self._cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

    def get(self, weight: str, size: int):
        key = (weight, size)
        if key not in self._cache:
            p = self.paths.get(weight)
            self._cache[key] = (
                ImageFont.truetype(str(p), size) if p else ImageFont.load_default(size=size)
            )
        return self._cache[key]


# ---------- 텍스트 배치 ----------


def text_w(draw: ImageDraw.ImageDraw, s: str, font) -> float:
    return draw.textlength(s, font=font)


def wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    """공백 기준 줄바꿈, 한 단어가 너무 길면 글자 단위로 자른다 (한글 대응)."""
    lines: list[str] = []
    for para in text.replace("\\n", "\n").split("\n"):
        para = para.strip()
        if not para:
            lines.append("")
            continue
        cur = ""
        for word in para.split(" "):
            cand = f"{cur} {word}" if cur else word
            if text_w(draw, cand, font) <= max_w:
                cur = cand
                continue
            if cur:
                lines.append(cur)
                cur = ""
            while text_w(draw, word, font) > max_w:  # 긴 단어는 글자 단위로
                i = 1
                while i < len(word) and text_w(draw, word[: i + 1], font) <= max_w:
                    i += 1
                lines.append(word[:i])
                word = word[i:]
            cur = word
        if cur:
            lines.append(cur)
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def fit(draw, text, fonts: Fonts, weight, size, max_w, max_h, min_size=28, spacing=1.35):
    """상자에 들어갈 때까지 글자 크기를 줄인다."""
    while True:
        font = fonts.get(weight, size)
        lines = wrap(draw, text, font, max_w)
        lh = int(size * spacing)
        if len(lines) * lh <= max_h or size <= min_size:
            return font, lines, lh
        size -= 4


def draw_lines(draw, xy, lines, font, lh, fill):
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += lh
    return y


# ---------- 슬라이드 ----------


def _background(theme) -> Image.Image:
    top, bot = theme["bg1"], theme["bg2"]
    grad = Image.new("RGB", (1, H))
    for y in range(H):
        t = y / (H - 1)
        grad.putpixel((0, y), tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    return grad.resize((W, H))


def _footer(draw, fonts, theme, idx, total, handle):
    y = H - PAD
    f = fonts.get("Regular", 30)
    if handle:
        draw.text((PAD, y - 30), f"@{handle.lstrip('@')}", font=f, fill=theme["sub"])
    label = f"{idx}/{total}"
    draw.text((W - PAD - text_w(draw, label, f), y - 30), label, font=f, fill=theme["sub"])
    # 진행 바
    bar_w = W - 2 * PAD
    draw.rectangle((PAD, y + 18, PAD + bar_w, y + 24), fill=_mix(theme["bg2"], theme["sub"], 0.35))
    draw.rectangle((PAD, y + 18, PAD + int(bar_w * idx / total), y + 24), fill=theme["accent"])


def _mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def render_slide(
    slide: Slide, idx: int, total: int, fonts: Fonts, theme_name: str, handle: str, swipe_hint: bool = True
) -> Image.Image:
    theme = THEMES.get(theme_name, THEMES["midnight"])
    img = _background(theme)
    d = ImageDraw.Draw(img)
    max_w = W - 2 * PAD
    bottom_limit = H - PAD - 90

    if slide.kind == "cover":
        # 블록별로 먼저 배치를 계산한 뒤 세로 가운데 정렬
        blocks = []  # (font, lines, line_height, color, gap_after)
        if slide.highlight:
            blocks.append((*fit(d, slide.highlight, fonts, "ExtraBold", 150, max_w, 360, spacing=1.15), theme["accent"], 30))
        blocks.append((*fit(d, slide.heading, fonts, "ExtraBold", 96, max_w, 420, spacing=1.25), theme["fg"], 40))
        if slide.body:
            blocks.append((*fit(d, slide.body, fonts, "Regular", 42, max_w, 240), theme["sub"], 0))
        total_h = sum(len(ls) * lh + gap for _, ls, lh, _, gap in blocks)
        y = max(PAD + 60, (bottom_limit - total_h) // 2)
        for f, lines, lh, color, gap in blocks:
            y = draw_lines(d, (PAD, y), lines, f, lh, color) + gap
        d.rectangle((PAD, PAD + 40, PAD + 120, PAD + 52), fill=theme["accent"])
        if swipe_hint:  # 캐러셀용 안내 (릴스 영상에서는 생략)
            hint = "옆으로 넘겨보세요 →"
            hf = fonts.get("Bold", 34)
            d.text((W - PAD - text_w(d, hint, hf), bottom_limit - 10), hint, font=hf, fill=theme["accent"])
    else:
        # 상단 번호 배지
        badge = {"summary": "요약", "cta": "SAVE"}.get(slide.kind, f"{idx - 1:02d}")
        bf = fonts.get("ExtraBold", 40)
        bw = text_w(d, badge, bf) + 48
        d.rounded_rectangle((PAD, PAD + 40, PAD + bw, PAD + 108), radius=34, fill=theme["accent"])
        d.text((PAD + 24, PAD + 50), badge, font=bf, fill=theme["bg1"])
        y = PAD + 170
        f, lines, lh = fit(d, slide.heading, fonts, "ExtraBold", 76, max_w, 300, spacing=1.25)
        y = draw_lines(d, (PAD, y), lines, f, lh, theme["fg"]) + 36
        if slide.highlight:
            hf, hl, hlh = fit(d, slide.highlight, fonts, "Bold", 56, max_w - 60, 200, spacing=1.25)
            box_h = len(hl) * hlh + 44
            d.rounded_rectangle((PAD, y, W - PAD, y + box_h), radius=24, fill=_mix(theme["bg2"], theme["accent"], 0.18))
            d.rectangle((PAD, y, PAD + 10, y + box_h), fill=theme["accent"])
            draw_lines(d, (PAD + 40, y + 22), hl, hf, hlh, theme["accent"])
            y += box_h + 44
        if slide.body:
            f, lines, lh = fit(d, slide.body, fonts, "Regular", 46, max_w, bottom_limit - y, spacing=1.5)
            draw_lines(d, (PAD, y), lines, f, lh, theme["fg"])

    _footer(d, fonts, theme, idx, total, handle)
    return img


def render_card(card: CardNews, out_dir: Path, fonts_dir: Path, theme: str, handle: str) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    fonts = Fonts(ensure_fonts(fonts_dir))
    paths = []
    total = len(card.slides)
    for i, slide in enumerate(card.slides, start=1):
        img = render_slide(slide, i, total, fonts, theme, handle)
        p = out_dir / f"slide_{i:02d}.jpg"
        # 인스타그램 API는 JPEG를 권장
        img.save(p, "JPEG", quality=92, optimize=True)
        paths.append(p)
    return paths
