"""자막·제목 글씨를 이미지로 그리고, 음성 시간에 맞춰 자막을 나눕니다."""

from __future__ import annotations

import math
import os
import re
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import FONT_DIR, ROOT

SUB_STYLES = {
    "흰 글씨 + 검은 테두리": "outline",
    "반투명 상자": "box",
    "노란 강조": "yellow",
}


def find_font() -> str:
    """굵은 한글 글꼴을 찾습니다.
    순서: assets/fonts → SHORTS_FONT_DIR → 카드뉴스 봇의 Pretendard → 윈도우·맥·리눅스 기본 한글 글꼴."""
    dirs = [FONT_DIR]
    if os.environ.get("SHORTS_FONT_DIR"):
        dirs.append(Path(os.environ["SHORTS_FONT_DIR"]))
    dirs.append(ROOT.parent / "cardnews-bot" / "fonts")
    for d in dirs:
        fonts = sorted(p for p in d.glob("*") if p.suffix.lower() in (".ttf", ".otf", ".ttc")) if d.is_dir() else []
        for kw in ("black", "extrabold", "heavy", "bold"):
            for p in fonts:
                if kw in p.name.lower():
                    return str(p)
        if fonts:
            return str(fonts[0])
    for p in (r"C:\Windows\Fonts\malgunbd.ttf", r"C:\Windows\Fonts\malgun.ttf",
              "/System/Library/Fonts/AppleSDGothicNeo.ttc",
              "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
              "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"):
        if Path(p).exists():
            return p
    raise FileNotFoundError("한글 글꼴을 찾지 못했어요. assets/fonts 폴더에 .ttf 글꼴을 넣어 주세요.")


@lru_cache(maxsize=32)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            trial = f"{cur} {word}".strip()
            if font.getlength(trial) <= max_w:
                cur = trial
                continue
            if cur:
                lines.append(cur)
            cur = ""
            # 한 단어가 너무 길면 글자 단위로 자릅니다
            for ch in word:
                if font.getlength(cur + ch) > max_w and cur:
                    lines.append(cur)
                    cur = ch
                else:
                    cur += ch
        if cur:
            lines.append(cur)
    return lines or [""]


def render_text(
    text: str,
    font_path: str,
    size: int,
    max_w: int,
    fill=(255, 255, 255),
    stroke: int = 0,
    stroke_fill=(0, 0, 0),
    box=None,
    box_pad: float = 0.35,
    shadow: bool = True,
    line_gap: float = 0.22,
) -> Image.Image:
    """가운데 정렬된 여러 줄 글씨를 투명 배경 RGBA 이미지로 그립니다."""
    font = _font(font_path, size)
    lines = _wrap(text, font, max_w)
    asc, desc = font.getmetrics()
    line_h = asc + desc
    gap = int(size * line_gap)
    widths = [font.getlength(l) for l in lines]
    text_w = int(max(widths)) + stroke * 2
    text_h = line_h * len(lines) + gap * (len(lines) - 1) + stroke * 2
    pad = int(size * box_pad) if box else int(size * 0.25) + stroke
    W, H = text_w + pad * 2, text_h + pad * 2
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    if box:
        bd = ImageDraw.Draw(img)
        bd.rounded_rectangle((0, 0, W - 1, H - 1), radius=int(size * 0.32), fill=box)

    def draw_lines(d: ImageDraw.ImageDraw, color, dx=0, dy=0, with_stroke=True):
        y = pad + stroke
        for line, lw in zip(lines, widths):
            x = (W - lw) / 2
            d.text((x + dx, y + dy), line, font=font, fill=color,
                   stroke_width=stroke if with_stroke else 0, stroke_fill=stroke_fill)
            y += line_h + gap

    if shadow and not box:
        sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw_lines(ImageDraw.Draw(sh), (0, 0, 0, 150), dy=int(size * 0.06))
        sh = sh.filter(ImageFilter.GaussianBlur(max(2, size // 14)))
        img = Image.alpha_composite(img, sh)
    draw_lines(ImageDraw.Draw(img), fill)
    return img


def subtitle_image(text: str, style: str, font_path: str, frame_w: int) -> Image.Image:
    size = int(frame_w * 0.074)
    max_w = int(frame_w * 0.84)
    if style == "box":
        return render_text(text, font_path, size, max_w, box=(0, 0, 0, 165), shadow=False)
    color = (255, 225, 77) if style == "yellow" else (255, 255, 255)
    return render_text(text, font_path, size, max_w, fill=color, stroke=max(3, size // 11))


def headline_image(text: str, accent, font_path: str, frame_w: int) -> Image.Image:
    size = int(frame_w * 0.078)
    lum = 0.299 * accent[0] + 0.587 * accent[1] + 0.114 * accent[2]
    fill = (20, 20, 20) if lum > 150 else (255, 255, 255)
    return render_text(text, font_path, size, int(frame_w * 0.8), fill=fill,
                       box=(*accent, 238), box_pad=0.3, shadow=False)


def title_image(text: str, font_path: str, frame_w: int) -> Image.Image:
    size = int(frame_w * 0.04)
    return render_text(text, font_path, size, int(frame_w * 0.8), fill=(255, 255, 255, 230),
                       stroke=max(2, size // 12), shadow=True)


# ---------------------------------------------------------------- 자막 타이밍

_END_PUNCT = re.compile(r"[.,!?…~]$")
_SENT_END = re.compile(r"[.!?…]$")


def _clean(text: str) -> str:
    """쇼츠 자막 관례대로 끝의 마침표·쉼표는 뺍니다 (물음표·느낌표는 유지)."""
    return re.sub(r"[.,…]+$", "", text.strip())


def token_times(narration: str, words: list[dict], total: float, lead: float = 0.0) -> list[dict]:
    """띄어쓰기 단위(어절)마다 시작·끝 시간을 구합니다. 음성 정보가 없으면 글자 수 비율로 나눕니다."""
    tokens, pos, p = [], [], 0
    for tok in narration.split():
        idx = narration.find(tok, p)
        tokens.append(tok)
        pos.append(idx)
        p = idx + len(tok)
    if not tokens:
        return []

    starts: list[float | None] = [None] * len(tokens)
    ends: list[float | None] = [None] * len(tokens)
    p = 0
    for w in words:
        wt = w["text"].strip()
        if not wt:
            continue
        idx = narration.find(wt, p)
        if idx < 0 or idx - p > 40:
            continue
        p = idx + len(wt)
        for i, tp in enumerate(pos):
            if tp <= idx < tp + len(tokens[i]):
                starts[i] = w["start"] + lead if starts[i] is None else starts[i]
                ends[i] = w["end"] + lead
                break

    if all(s is None for s in starts):
        # 글자 수 비율로 시간 배분
        lens = [len(t) + 1 for t in tokens]
        acc, out = lead, []
        span = max(0.1, total - lead - 0.2)
        for t, l in zip(tokens, lens):
            d = span * l / sum(lens)
            out.append({"text": t, "start": acc, "end": acc + d})
            acc += d
        return out

    # 빠진 어절은 앞뒤 값으로 메웁니다
    for i in range(len(tokens)):
        if starts[i] is None:
            prev = next((ends[j] for j in range(i - 1, -1, -1) if ends[j] is not None), lead)
            starts[i] = prev
        if ends[i] is None:
            nxt = next((starts[j] for j in range(i + 1, len(tokens)) if starts[j] is not None), None)
            ends[i] = nxt if nxt is not None else min(total, starts[i] + 0.4)
    return [{"text": t, "start": s, "end": e} for t, s, e in zip(tokens, starts, ends)]


def _split_balanced(sent: list[dict], max_chars: int) -> list[list[dict]]:
    """한 문장을 k개 묶음으로 나누되, 묶음 길이가 최대한 고르도록(한 단어만 남지 않도록) 나눕니다."""
    lens = [len(t["text"]) for t in sent]
    n = len(sent)
    total = sum(lens) + n - 1
    if total <= max_chars or n == 1:
        return [sent]
    k = min(n, math.ceil(total / max_chars))
    target = total / k

    def glen(i: int, j: int) -> int:
        return sum(lens[i:j]) + (j - i - 1)

    inf = float("inf")
    best = [[inf] * (k + 1) for _ in range(n + 1)]
    back = [[0] * (k + 1) for _ in range(n + 1)]
    best[0][0] = 0.0
    for g in range(1, k + 1):
        for j in range(g, n + 1):
            for i in range(g - 1, j):
                if best[i][g - 1] == inf:
                    continue
                length = glen(i, j)
                cost = (length - target) ** 2 + 40 * max(0, length - max_chars)
                if j < n and _END_PUNCT.search(sent[j - 1]["text"]):
                    cost -= 4  # 쉼표 뒤에서 끊기면 자연스러움
                if best[i][g - 1] + cost < best[j][g]:
                    best[j][g] = best[i][g - 1] + cost
                    back[j][g] = i
    groups, j = [], n
    for g in range(k, 0, -1):
        i = back[j][g]
        groups.append(sent[i:j])
        j = i
    return groups[::-1]


def chunk_subtitles(tokens: list[dict], max_chars: int, scene_end: float) -> list[dict]:
    """어절들을 한 화면에 보여줄 짧은 자막 묶음으로 나눕니다.
    문장이 끝나면 항상 끊고, 긴 문장은 비슷한 길이로 고르게 나눕니다."""
    sentences: list[list[dict]] = []
    cur: list[dict] = []
    for tok in tokens:
        cur.append(tok)
        if _SENT_END.search(tok["text"]):
            sentences.append(cur)
            cur = []
    if cur:
        sentences.append(cur)

    chunks: list[list[dict]] = []
    for sent in sentences:
        chunks.extend(_split_balanced(sent, max_chars))
    out = []
    for i, c in enumerate(chunks):
        start = c[0]["start"]
        end = chunks[i + 1][0]["start"] if i + 1 < len(chunks) else scene_end
        out.append({"text": _clean(" ".join(t["text"] for t in c)), "start": start, "end": end})
    return out
