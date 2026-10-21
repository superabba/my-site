"""Gemini 나노바나나(Nano Banana)로 장면마다 세로 배경 그림을 만든다.

- 모델 기본값: Nano Banana 2.1 (`gemini-nano-banana-2.1`), 9:16, 2K.
  이미지 생성은 Gemini API 무료 사용량이 없어 결제가 연결된 키가 필요하다 (2K 한 장 약 $0.05).
- 그림에는 글자를 넣지 않는다 (자막·큰 글씨는 영상에서 따로 얹는다). 실존 인물·로고도 그리지 않는다.
- 같은 요청은 cache/ai_images에 저장해 두고 다시 쓴다 (다시 만들 때 비용이 들지 않게).
"""

from __future__ import annotations

import base64
import hashlib
import logging
from pathlib import Path

from .config import CACHE_DIR

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-nano-banana-2.1"
DEFAULT_SIZE = "2K"
MODELS = {
    "gemini-nano-banana-2.1": "Nano Banana 2.1 (추천)",
    "gemini-3.1-flash-lite-image": "Nano Banana 2 Lite (가장 저렴, 1K)",
    "gemini-3-pro-image": "Nano Banana Pro (최고 품질, 비쌈)",
}
DEFAULT_STYLE = ("clean modern editorial illustration with soft 3D shapes, gentle gradients and warm cinematic "
                 "lighting, cohesive muted color palette, Korean everyday-life setting")

IMAGE_DIR = CACHE_DIR / "ai_images"
IMAGE_DIR.mkdir(parents=True, exist_ok=True)


class ImageGenError(Exception):
    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal  # 키·결제·권한 문제처럼 다음 장면도 실패할 오류


def build_prompt(scene: str, style: str = "") -> str:
    return (
        "Vertical 9:16 background artwork for a Korean YouTube Shorts video about money and the economy.\n"
        f"Visual style: {style or DEFAULT_STYLE}.\n"
        f"Scene to depict: {scene.strip()}\n"
        "Rules: absolutely no text, letters, numbers, captions, charts with labels, logos, watermarks or UI. "
        "Any paper, document, book, screen, sign or label in the scene must be completely blank. "
        "No real, famous or identifiable people and no brand names. "
        "Keep the top quarter and the bottom third calm and uncluttered because captions will be placed there. "
        "One clear focal subject in the middle."
    )


def _image_bytes(output_image) -> bytes:
    data = getattr(output_image, "data", None)
    if not data:
        raise ImageGenError("응답에 그림이 없습니다")
    if isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
        if raw[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"RIFF") or raw[:3] == b"\xff\xd8\xff":
            return raw
        return base64.b64decode(raw)
    return base64.b64decode(data)


def _classify(e: Exception) -> ImageGenError:
    text = str(e)
    low = text.lower()
    fatal = any(k in low for k in ("api key", "api_key", "permission", "billing", "not available", "free tier",
                                   "limit: 0", "unauthenticated", "403", "401"))
    hint = " (이미지 생성은 결제가 연결된 Gemini API 키가 필요해요)" if fatal else ""
    return ImageGenError(f"그림 생성 실패: {text[:300]}{hint}", fatal=fatal)


def generate_image(scene: str, api_key: str, style: str = "", model: str = DEFAULT_MODEL,
                   size: str = DEFAULT_SIZE, client=None) -> Path:
    """장면 설명으로 9:16 그림을 만들어 파일 경로를 돌려준다."""
    if not api_key and client is None:
        raise ImageGenError("Gemini API 키가 없어요.", fatal=True)
    prompt = build_prompt(scene, style)
    if model == "gemini-3.1-flash-lite-image":
        size = "1K"  # Lite는 1K만 지원
    key = hashlib.sha1(f"{model}|{size}|{prompt}".encode("utf-8")).hexdigest()[:20]
    cached = next(IMAGE_DIR.glob(f"{key}.*"), None)
    if cached and cached.stat().st_size > 0:
        return cached

    if client is None:
        from google import genai

        client = genai.Client(api_key=api_key)
    try:
        interaction = client.interactions.create(
            model=model,
            input=prompt,
            response_format={"type": "image", "aspect_ratio": "9:16", "image_size": size},
        )
    except Exception as e:  # SDK 오류 종류가 버전마다 달라 내용으로 판단한다
        raise _classify(e) from e
    raw = _image_bytes(getattr(interaction, "output_image", None))
    mime = getattr(getattr(interaction, "output_image", None), "mime_type", "") or ""
    ext = ".jpg" if "jpeg" in str(mime) or raw[:3] == b"\xff\xd8\xff" else ".png"
    path = IMAGE_DIR / f"{key}{ext}"
    path.write_bytes(raw)
    return path
