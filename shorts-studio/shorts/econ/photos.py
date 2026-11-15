"""장면 사진: 무료 FLUX(키 없음) → 실패하면 주제별 미리 만든 사진.

econ-video-studio의 방식을 가져왔다.
  1) 루틴이 쓴 영어 image_prompt 로 Pollinations(키 없는 FLUX)에 그림을 요청한다 (3번 재시도, 시드를 바꿔서).
  2) 그래도 안 되면 대본 내용에 맞는 주제별 사진(assets/econ_fallback/NN_*.jpg)을 쓴다.
     바로 앞 장면과 같은 사진이 연달아 나오지 않게 다음 후보로 넘긴다.
가로 사진은 세로 화면에 맞게 '흐린 확대 배경 + 선명한 원본' 으로 채운다 (늘려서 뭉개지지 않게).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import urllib.parse
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

log = logging.getLogger(__name__)

FALLBACK_DIR = Path(__file__).resolve().parents[2] / "assets" / "econ_fallback"
SIZE = (1080, 1920)

# 대체 사진은 파일 이름 "번호_주제묶음_설명.jpg" 의 주제묶음으로 나뉜다 (예: 17_price_grocery_cart_daylight.jpg).
# 묶음마다 키워드가 있어, 대본에서 일치한 키워드 길이의 합이 가장 큰 묶음의 사진을 고른다 (두 글자 이상만).
GROUPS: dict[str, list[str]] = {
    "price": ["물가", "인플레이션", "장바구니", "식자재", "마트", "체감물가", "소비자물가", "채소", "식품", "농산물", "가격"],
    "rate": ["연준", "파월", "FOMC", "빅스텝", "베이비스텝", "한국은행", "금통위", "기준금리", "통화정책", "한은", "금리", "중앙은행"],
    "realestate": ["부동산", "아파트", "집값", "전세", "월세", "매매", "청약", "주택", "재건축", "분양"],
    "tax": ["세금", "연말정산", "소득세", "세액", "공제", "환급", "절세", "세무", "국세", "납세", "종합소득세", "부가세"],
    "pension": ["연금", "퇴직", "노후", "국민연금", "연금저축", "IRP", "은퇴", "노령"],
    "consumption": ["소비", "카드", "쇼핑", "백화점", "구독", "할인", "혜택", "외식", "카페", "결제"],
    "jobs": ["월급", "연봉", "취업", "일자리", "고용", "실업", "직장", "임금", "최저임금", "채용", "퇴사"],
    "fx": ["환율", "달러", "원화", "엔화", "외환", "강달러", "원달러", "위안화", "유로", "환전"],
    "stock": ["주식", "증시", "코스피", "나스닥", "코스닥", "월가", "폭락", "랠리", "공매도", "배당", "ETF", "비트코인",
              "가상화폐", "암호화폐", "이더리움", "디지털자산"],
    "tech": ["반도체", "파운드리", "HBM", "엔비디아", "삼전", "하이닉스", "테크", "전기차", "배터리", "인공지능"],
    "export": ["수출", "무역", "관세", "항만", "컨테이너", "물류", "선박", "경상수지", "조선", "해운"],
    "loan": ["대출", "이자", "변동금리", "고정금리", "가계부채", "영끌", "은행", "적금", "예금", "저축", "주담대", "상환"],
    "energy": ["유가", "석유", "원유", "에너지", "정유", "전력", "중동", "금값", "금괴", "안전자산", "골드", "은값", "실버",
               "원자재", "가스", "태양광", "풍력"],
    "summary": ["순서로", "기억하세요", "요약", "정리", "팔로우", "저장", "핵심"],
}
GENERIC_GROUPS = ["summary", "stock", "rate"]  # 어느 주제에도 안 맞을 때 (분위기 중립)
# 예전 사진(01~15)은 이름에 묶음이 없어 번호로 묶음을 정한다
LEGACY_GROUP = {1: "fx", 2: "realestate", 3: "stock", 4: "price", 5: "tech", 6: "export", 7: "energy", 8: "energy",
                9: "stock", 10: "rate", 11: "rate", 12: "loan", 13: "summary", 14: "stock", 15: "summary"}
_NAME = re.compile(r"^(\d+)_([a-z]+)_")


def _group_of(path: Path) -> str | None:
    m = _NAME.match(path.name)
    if not m:
        return None
    n, token = int(m.group(1)), m.group(2)
    if n in LEGACY_GROUP and token not in GROUPS:
        return LEGACY_GROUP[n]
    return token if token in GROUPS else LEGACY_GROUP.get(n)


def pool() -> dict[str, list[Path]]:
    """묶음별 대체 사진 (파일 이름순)."""
    out: dict[str, list[Path]] = {}
    for p in sorted(FALLBACK_DIR.glob("*.jpg")) if FALLBACK_DIR.is_dir() else []:
        g = _group_of(p)
        if g:
            out.setdefault(g, []).append(p)
    return out


def fit_portrait(img: Image.Image, size: tuple[int, int] = SIZE) -> Image.Image:
    """세로 화면에 맞춘다. 세로 사진은 꽉 채워 자르고, 가로 사진은 흐린 배경 위에 원본을 얹는다."""
    img = ImageOps.exif_transpose(img).convert("RGB")
    w, h = size
    if img.width / img.height <= 0.8:
        return ImageOps.fit(img, size, Image.LANCZOS)
    bg = ImageOps.fit(img, size, Image.LANCZOS).filter(ImageFilter.GaussianBlur(40))
    bg = ImageEnhance.Brightness(bg).enhance(0.55)
    fg = img.resize((w, max(1, round(img.height * w / img.width))), Image.LANCZOS)
    bg.paste(fg, (0, (h - fg.height) // 2))
    return bg


def theme_scores(text: str) -> list[tuple[int, str]]:
    """[(점수, 묶음 이름)] 점수 높은 순. 점수 = 일치한 키워드(두 글자 이상) 길이의 합."""
    scored = []
    for group, keywords in GROUPS.items():
        sc = sum(len(k) for k in keywords if len(k) >= 2 and k in text)
        if sc:
            scored.append((sc, group))
    return sorted(scored, key=lambda x: -x[0])


def pick_fallback(text: str, prev: str | None = None, used: set | None = None) -> Path | None:
    """대본 내용에 맞는 묶음에서 사진을 고른다. 이 영상에서 안 쓴 사진을 먼저, 묶음 안에서는 대본 해시로 시작 위치를
    달리해 날마다 다른 사진이 나오게 한다. 다 썼으면 바로 앞 장면(prev)과만 안 겹치게. used/prev 는 파일 이름."""
    groups = pool()
    order = [g for _, g in theme_scores(text)] + [g for g in GENERIC_GROUPS]
    start = int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16)
    for avoid_used in (True, False):
        for g in order:
            files = groups.get(g, [])
            for k in range(len(files)):
                f = files[(start + k) % len(files)]
                if f.name != prev and not (avoid_used and f.name in (used or set())):
                    return f
    return next((f for fs in groups.values() for f in fs), None)


class FluxUnavailable(Exception):
    """서버가 요청 자체를 막았다 (402 결제·한도, 403, 429). 재시도해도 소용없으니 이번 영상에서는 FLUX 를 그만 쓴다."""


def fetch_flux(prompt: str, seed: int, tries: int = 3, timeout: int = 40) -> Image.Image | None:
    enc = urllib.parse.quote(prompt)
    for k in range(tries):
        url = (f"https://image.pollinations.ai/prompt/{enc}"
               f"?width=768&height=1344&model=flux&nologo=true&seed={seed + k}")
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200 and len(r.content) > 10000:
                img = Image.open(BytesIO(r.content)).convert("RGB")
                if img.height > 200:
                    img = img.crop((0, 0, img.width, img.height - 48))  # 아래 워터마크 띠 제거
                return fit_portrait(img)
            if r.status_code in (402, 403, 429):
                raise FluxUnavailable(f"HTTP {r.status_code}")
            log.warning("FLUX 응답 %s (시도 %d/%d)", r.status_code, k + 1, tries)
        except (requests.RequestException, OSError) as e:
            log.warning("FLUX 요청 실패 (시도 %d/%d): %s", k + 1, tries, str(e)[:120])
        if k + 1 < tries:
            time.sleep(2 * (k + 1))
    return None


def scene_prompt(sc: dict, visual_style: str = "") -> str:
    base = (sc.get("image_prompt") or sc.get("keywords") or sc.get("headline") or "").strip()
    style = visual_style.strip() or "cinematic documentary photograph, moody lighting"
    return f"{base}, {style}, vertical composition, no text, no letters, no logos, no real people, photorealistic 8k"


def prepare_media(script: dict, workdir: Path, use_flux: bool = True) -> dict:
    """각 장면에 sc['media'](사진 경로)를 채운다. 돌려주는 값: {"flux": n, "fallback": n, "gradient": n}"""
    workdir.mkdir(parents=True, exist_ok=True)
    stats = {"flux": 0, "fallback": 0, "gradient": 0}
    prev_num = None
    used: set = set()
    for i, sc in enumerate(script["scenes"], 1):
        if sc.get("media"):
            continue
        dest = workdir / f"scene_{i:02d}.jpg"
        text = " ".join([sc.get("narration", ""), sc.get("headline", ""), sc.get("keywords", "")])
        img = None
        if use_flux and (sc.get("image_prompt") or "").strip():
            seed = int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16) % 100000
            try:
                img = fetch_flux(scene_prompt(sc, script.get("visual_style", "")), seed)
            except FluxUnavailable as e:
                log.warning("FLUX 서버가 요청을 막아(%s) 남은 장면은 미리 만든 사진을 씁니다", e)
                use_flux = False
                img = None
            if img is not None:
                stats["flux"] += 1
                prev_num = None
        if img is None:
            fb = pick_fallback(text, prev_num, used)
            if fb is not None:
                img = fit_portrait(Image.open(fb))
                prev_num = fb.name
                used.add(fb.name)
                stats["fallback"] += 1
        if img is None:
            stats["gradient"] += 1  # media 를 비워 두면 render 가 자동 그라데이션 배경을 쓴다
            continue
        img.save(dest, "JPEG", quality=92)
        sc["media"] = str(dest)
    return stats
