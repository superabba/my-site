"""영상 속 음성을 글자로 바꾼다 (선택 기능: faster-whisper가 설치된 경우에만)."""

import importlib.util
from pathlib import Path


def available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def load_model(model_size: str = "small"):
    """처음 한 번은 모델 파일을 내려받는다 (small 약 500MB)."""
    from faster_whisper import WhisperModel

    return WhisperModel(model_size, device="cpu", compute_type="int8")


def transcribe(model, video_path: Path) -> str:
    segments, _info = model.transcribe(str(video_path), vad_filter=True, beam_size=5)
    lines = [seg.text.strip() for seg in segments if seg.text.strip()]
    return "\n".join(lines)
