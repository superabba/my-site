"""ffmpeg(영상·소리 변환 도구) 호출을 모아둔 모듈. ffmpeg는 imageio-ffmpeg 패키지에 포함된 것을 씁니다."""

from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

import imageio_ffmpeg
import numpy as np

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
SR = 44100
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class FFmpegError(Exception):
    pass


def run(args: list[str]) -> None:
    proc = subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        creationflags=_NO_WINDOW,
    )
    if proc.returncode != 0:
        raise FFmpegError(proc.stderr.decode("utf-8", "replace")[-2000:])


def decode_audio(path: str | Path, sr: int = SR) -> np.ndarray:
    """오디오 파일을 (샘플 수, 2) float32 배열로 읽습니다."""
    proc = subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-f", "f32le", "-ac", "2", "-ar", str(sr), "-"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        creationflags=_NO_WINDOW,
    )
    if proc.returncode != 0:
        raise FFmpegError(proc.stderr.decode("utf-8", "replace")[-2000:])
    return np.frombuffer(proc.stdout, dtype=np.float32).reshape(-1, 2).copy()


def write_wav(path: str | Path, audio: np.ndarray, sr: int = SR) -> None:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


class VideoReader:
    """영상 파일을 정해진 크기·fps로 잘라 한 프레임씩 읽습니다. 짧으면 반복 재생합니다."""

    def __init__(self, path: str | Path, width: int, height: int, fps: int, duration: float):
        self.size = (width, height)
        self.frame_bytes = width * height * 3
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},fps={fps},format=rgb24"
        )
        self.proc = subprocess.Popen(
            [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1",
             "-i", str(path), "-t", f"{duration + 0.5:.3f}", "-an", "-vf", vf,
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
        self.last: bytes | None = None

    def read(self) -> bytes | None:
        if self.proc.stdout is not None:
            data = self.proc.stdout.read(self.frame_bytes)
            if len(data) == self.frame_bytes:
                self.last = data
        return self.last

    def close(self) -> None:
        try:
            if self.proc.stdout:
                self.proc.stdout.close()
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:
            pass
