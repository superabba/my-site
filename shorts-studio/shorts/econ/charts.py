"""장면 위에 얹는 인포그래픽 카드 (econ-video-studio의 draw_* 를 가져와 다듬음).

루틴이 장면마다 chart 를 직접 지정한다 (대본에서 숫자를 추정하지 않는다). 종류:
  gauge    {"value": 0~100, "label", "sub"}                         확률·비중
  compare  {"bars": [{"name", "value", "display"}, ...2~3개]}        같은 단위 숫자 비교 (길이는 실제 값 비율)
  calendar {"date", "event", "forecast"}                            일정
  flow     {"nodes": [{"title", "desc"}, ...3개], "cta"}             요약 3단계
  impact   {"steps": [{"title", "desc"}, ...3개]}                    원인→파급→결과
  stat     {"left_label", "left", "right_label", "right"}           핵심 수치/훅
공통: "badge"(카드 위 작은 문구, 선택)
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from ..text import find_font

CHART_TYPES = ("gauge", "compare", "calendar", "flow", "impact", "stat")
CARD_TOP, CARD_BOTTOM = 820, 1360   # 1080x1920 기준 카드 위치
CARD_H = CARD_BOTTOM - CARD_TOP
MARGIN = 64
ACCENTS = {"gauge": "#38bdf8", "compare": "#ef4444", "calendar": "#fbbf24", "flow": "#10b981",
           "impact": "#ef4444", "stat": "#f59e0b"}

_fonts: dict[int, ImageFont.FreeTypeFont] = {}


def get_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    if size not in _fonts:
        try:
            _fonts[size] = ImageFont.truetype(find_font(), size)
        except Exception:
            _fonts[size] = ImageFont.load_default()
    return _fonts[size]


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color.lstrip("#")[j:j + 2], 16) for j in (0, 2, 4))  # type: ignore[return-value]


def clip(text: str, n: int) -> str:
    text = str(text).strip()
    if len(text) <= n:
        return text
    cut = text[:n]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,·") + "…"


def normalize_chart(chart: dict) -> dict:
    """루틴이 쓴 chart 를 그리기 좋은 형태로 바꾼다 (검증은 content.validate)."""
    t = chart["type"]
    out: dict = {"type": t, "badge": clip(chart.get("badge") or "", 28), "accent": ACCENTS[t]}
    if t == "gauge":
        out.update(value=max(0.0, min(100.0, float(chart["value"]))), label=clip(chart.get("label", ""), 18),
                   sub=clip(chart.get("sub", ""), 22))
    elif t == "compare":
        bars = chart["bars"][:3]
        vmax = max(float(b["value"]) for b in bars) or 1.0
        palette = ["#64748b", "#ef4444", "#f97316"]
        out["bars"] = [{"name": clip(b.get("name", ""), 14), "val": max(8, round(float(b["value"]) / vmax * 100)),
                        "display": clip(b.get("display") or f"{b['value']:g}", 16), "color": palette[i]}
                       for i, b in enumerate(bars)]
    elif t == "calendar":
        out.update(date=clip(chart["date"], 10), event=clip(chart["event"], 16), forecast=clip(chart["forecast"], 20))
    elif t == "flow":
        out.update(nodes=[{"step": f"STEP {i + 1:02d}", "title": clip(n["title"], 14), "desc": clip(n.get("desc", ""), 22)}
                          for i, n in enumerate(chart["nodes"][:3])],
                   cta=clip(chart.get("cta") or "저장해 두고 꼭 체크하세요!", 24))
    elif t == "impact":
        colors, levels = ["#38bdf8", "#f59e0b", "#ef4444"], [0.45, 0.72, 0.95]
        titles = ["1단계 원인", "2단계 파급", "3단계 결과"]
        out["steps"] = [{"title": titles[i], "desc": clip(s["desc"] if "desc" in s else s.get("title", ""), 13),
                         "level": levels[i], "color": colors[i]} for i, s in enumerate(chart["steps"][:3])]
    elif t == "stat":
        out.update(left_label=clip(chart.get("left_label", "핵심 수치"), 12), left_val=clip(chart["left"], 11),
                   right_label=clip(chart.get("right_label", "체감 포인트"), 12), right_val=clip(chart["right"], 11))
    return out


def _gauge(draw, c, ease, x1, y1, x2, y2):
    cur = c["value"] * ease
    cx, cy, r = x1 + 250, (y1 + y2) // 2, 165
    bbox = (cx - r, cy - r, cx + r, cy + r)
    draw.arc(bbox, start=135, end=405, fill=(51, 65, 85, 220), width=36)
    draw.arc(bbox, start=135, end=135 + max(6, int(270 * cur / 100.0)), fill=(56, 189, 248, 255), width=36)
    f_num = get_font(74)
    txt = f"{cur:.0f}%"
    b = draw.textbbox((0, 0), txt, font=f_num)
    draw.text((cx - (b[2] - b[0]) // 2, cy - 48), txt, font=f_num, fill=(255, 255, 255, 255))
    tx = cx + r + 60
    draw.text((tx, cy - 100), c["label"], font=get_font(38), fill=(255, 255, 255, 255))
    draw.text((tx, cy - 30), c["sub"], font=get_font(32, False), fill=(148, 163, 184, 255))


def _compare(draw, c, ease, x1, y1, x2, y2):
    f_name, f_disp = get_font(34), get_font(32)
    for i, b in enumerate(c["bars"]):
        ry = y1 + 75 + i * 135
        draw.text((x1 + 48, ry), b["name"], font=f_name, fill=(226, 232, 240, 255))
        cur_w = max(40, int(((x2 - x1) - 100) * (b["val"] / 100.0) * ease))
        rgb = _rgb(b["color"])
        draw.rounded_rectangle((x1 + 48, ry + 52, x2 - 52, ry + 104), radius=14, fill=(30, 41, 59, 190))
        draw.rounded_rectangle((x1 + 48, ry + 52, x1 + 48 + cur_w, ry + 104), radius=14, fill=(*rgb, 240))
        draw.text((x1 + 68, ry + 56), b["display"], font=f_disp, fill=(255, 255, 255, 255))


def _calendar(draw, c, ease, x1, y1, x2, y2):
    cx, cy = x1 + 230, (y1 + y2) // 2
    draw.rounded_rectangle((cx - 150, cy - 155, cx + 150, cy + 155), radius=28, fill=(30, 41, 59, 240),
                           outline=(251, 191, 36, 220), width=3)
    draw.rounded_rectangle((cx - 150, cy - 155, cx + 150, cy - 65), radius=28, fill=(245, 158, 11, 255))
    draw.text((cx - 88, cy - 130), "D-DAY 일정", font=get_font(30), fill=(15, 23, 42, 255))
    f_day = get_font(54 if len(c["date"]) > 6 else 62)
    db = draw.textbbox((0, 0), c["date"], font=f_day)
    draw.text((cx - (db[2] - db[0]) // 2, cy - 10), c["date"], font=f_day, fill=(255, 255, 255, 255))
    rx = cx + 195
    draw.text((rx, cy - 120), c["event"], font=get_font(40), fill=(255, 255, 255, 255))
    draw.rounded_rectangle((rx, cy - 35, x2 - 45, cy + 55), radius=18, fill=(239, 68, 68, int(60 * ease)),
                           outline=(239, 68, 68, 220), width=2)
    draw.text((rx + 24, cy - 12), c["forecast"], font=get_font(32), fill=(252, 165, 165, 255))
    draw.text((rx, cy + 82), "핵심 일정과 방향 주목", font=get_font(28, False), fill=(148, 163, 184, 255))


def _flow(draw, c, ease, x1, y1, x2, y2):
    f_step, f_title, f_desc = get_font(26), get_font(36), get_font(30, False)
    for i, n in enumerate(c["nodes"]):
        ry = y1 + 45 + i * 132
        alpha = 235 if ease >= i * 0.25 else 90
        draw.rounded_rectangle((x1 + 44, ry, x2 - 44, ry + 112), radius=20, fill=(15, 23, 42, alpha),
                               outline=(16, 185, 129, alpha), width=2)
        draw.rounded_rectangle((x1 + 64, ry + 24, x1 + 210, ry + 86), radius=14, fill=(16, 185, 129, alpha))
        draw.text((x1 + 84, ry + 38), n["step"], font=f_step, fill=(6, 12, 24, 255))
        draw.text((x1 + 236, ry + 16), n["title"], font=f_title, fill=(255, 255, 255, alpha))
        draw.text((x1 + 236, ry + 64), n["desc"], font=f_desc, fill=(167, 243, 208, alpha))
    draw.text((x1 + 180, y2 - 58), f"★ {c['cta']}", font=get_font(30), fill=(251, 191, 36, 255))


def _impact(draw, c, ease, x1, y1, x2, y2):
    f_t, f_d = get_font(34), get_font(26, False)
    col_w = (x2 - x1 - 120) // 3
    base_y = y2 - 75
    for i, s in enumerate(c["steps"]):
        bx = x1 + 48 + i * (col_w + 16)
        bh = int(240 * s["level"] * ease)
        by = base_y - bh
        rgb = _rgb(s["color"])
        draw.rounded_rectangle((bx, by, bx + col_w, base_y), radius=20, fill=(*rgb, 75), outline=(*rgb, 240), width=3)
        draw.text((bx + 20, y1 + 55), s["title"], font=f_t, fill=(*rgb, 255))
        draw.text((bx + 20, y1 + 105), s["desc"], font=f_d, fill=(203, 213, 225, 255))
        draw.text((bx + 24, by + 18), f"STEP {i + 1}", font=f_t, fill=(255, 255, 255, 255))


def _stat(draw, c, ease, x1, y1, x2, y2):
    f_lbl, f_val = get_font(30, False), get_font(40)
    mid_x, mid_y = (x1 + x2) // 2, (y1 + y2) // 2
    draw.rounded_rectangle((x1 + 36, y1 + 70, mid_x - 42, y2 - 70), radius=24, fill=(239, 68, 68, int(45 * ease)),
                           outline=(239, 68, 68, 200), width=3)
    draw.text((x1 + 60, y1 + 125), c["left_label"], font=f_lbl, fill=(252, 165, 165, 255))
    draw.text((x1 + 60, y1 + 195), c["left_val"], font=f_val, fill=(255, 255, 255, 255))
    draw.rounded_rectangle((mid_x + 42, y1 + 70, x2 - 36, y2 - 70), radius=24, fill=(245, 158, 11, int(45 * ease)),
                           outline=(245, 158, 11, 200), width=3)
    draw.text((mid_x + 66, y1 + 125), c["right_label"], font=f_lbl, fill=(253, 230, 138, 255))
    draw.text((mid_x + 66, y1 + 195), c["right_val"], font=f_val, fill=(255, 255, 255, 255))
    draw.ellipse((mid_x - 38, mid_y - 38, mid_x + 38, mid_y + 38), fill=(245, 158, 11, 255))
    draw.text((mid_x - 20, mid_y - 26), "→", font=get_font(40), fill=(10, 18, 34, 255))


_DRAW = {"gauge": _gauge, "compare": _compare, "calendar": _calendar, "flow": _flow, "impact": _impact,
         "stat": _stat}


def chart_card(chart: dict, progress: float, width: int = 1080, height: int = 1920) -> Image.Image:
    """카드 한 장(RGBA)을 그린다. progress 0→1 은 등장 애니메이션. 붙일 위치는 card_center_y()."""
    spec = chart if "accent" in chart else normalize_chart(chart)
    p = max(0.0, min(1.0, progress))
    ease = 1.0 - (1.0 - p) ** 3
    img = Image.new("RGBA", (width, CARD_H + 80), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img, "RGBA")
    if spec.get("badge"):
        f = get_font(26)
        bw = draw.textbbox((0, 0), spec["badge"], font=f)
        w = bw[2] - bw[0] + 44
        draw.rounded_rectangle((MARGIN, 0, MARGIN + w, 50), radius=25, fill=(15, 23, 42, 220),
                               outline=(56, 189, 248, 180), width=2)
        draw.text((MARGIN + 22, 8), spec["badge"], font=f, fill=(56, 189, 248, 255))
    y1, y2 = 70 if spec.get("badge") else 14, (70 if spec.get("badge") else 14) + CARD_H
    draw.rounded_rectangle((MARGIN, y1, width - MARGIN, y2), radius=36, fill=(10, 18, 34, 212),
                           outline=(148, 163, 184, 95), width=2)
    _DRAW[spec["type"]](draw, spec, ease, MARGIN, y1, width - MARGIN, y2)
    return img


CARD_IMG_H = CARD_H + 80
CARD_CENTER_Y = (CARD_TOP - 70 + CARD_IMG_H / 2) / 1920  # 카드 상자가 820~1360 에 오도록 (화면 높이 대비)
