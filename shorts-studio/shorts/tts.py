"""Microsoft Edge 무료 음성(edge-tts)으로 한국어 내레이션을 만들고, 단어별 시간도 받아옵니다."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import edge_tts

from .config import WORK_DIR

VOICES = {
    "여성 · 선희": "ko-KR-SunHiNeural",
    "남성 · 인준": "ko-KR-InJoonNeural",
    "남성 · 현수": "ko-KR-HyunsuMultilingualNeural",
}

TTS_DIR = WORK_DIR / "tts"
TTS_DIR.mkdir(parents=True, exist_ok=True)


class TTSError(Exception):
    pass


async def _synth(text: str, voice: str, rate: str, pitch: str, mp3_path: Path) -> list[dict]:
    comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, boundary="WordBoundary")
    words: list[dict] = []
    with open(mp3_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                # offset/duration 단위는 100나노초
                start = chunk["offset"] / 1e7
                words.append({"text": chunk["text"], "start": start, "end": start + chunk["duration"] / 1e7})
    return words


def synthesize(text: str, voice: str, speed: int = 0, pitch: int = 0) -> tuple[Path, list[dict]]:
    """text를 읽은 mp3 경로와 단어별 시간 목록을 돌려줍니다. 같은 요청은 캐시를 씁니다."""
    rate = f"{speed:+d}%"
    pitch_s = f"{pitch:+d}Hz"
    key = hashlib.sha1(f"{voice}|{rate}|{pitch_s}|{text}".encode("utf-8")).hexdigest()[:16]
    mp3_path = TTS_DIR / f"{key}.mp3"
    meta_path = TTS_DIR / f"{key}.json"
    if mp3_path.exists() and meta_path.exists() and mp3_path.stat().st_size > 0:
        return mp3_path, json.loads(meta_path.read_text(encoding="utf-8"))

    last_err: Exception | None = None
    for _ in range(3):
        try:
            words = asyncio.run(_synth(text, voice, rate, pitch_s, mp3_path))
            meta_path.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
            return mp3_path, words
        except Exception as e:  # 네트워크 오류 등은 몇 번 재시도
            last_err = e
    mp3_path.unlink(missing_ok=True)
    raise TTSError(f"음성을 만들지 못했어요(인터넷 연결 필요): {last_err}")
