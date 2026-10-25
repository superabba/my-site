"""영상 정보 읽기, 분석용 프레임 추출, 업로드용 압축 (imageio-ffmpeg에 들어 있는 ffmpeg 사용)."""

import io
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import imageio_ffmpeg
from PIL import Image

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# 인스타 릴스·스레드는 공개 주소에서 영상을 가져가는데, 봇이 쓰는 jsDelivr가 20MB 넘는 파일을 거절한다.
META_MAX_BYTES = 19 * 1024 * 1024
# 유튜브용 원본은 GitHub에 파일로 올라가므로 GitHub 파일 한도(100MB) 아래로 맞춘다.
YOUTUBE_MAX_BYTES = 90 * 1024 * 1024


@dataclass
class VideoInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    size_bytes: int

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)

    @property
    def orientation(self) -> str:
        if self.height > self.width:
            return "세로"
        if self.height == self.width:
            return "정사각형"
        return "가로"

    def warnings(self) -> list[str]:
        """플랫폼 규격에 맞지 않을 수 있는 부분."""
        w = []
        if self.duration > 180:
            w.append("3분을 넘는 영상은 유튜브에서 쇼츠가 아닌 일반 동영상으로 올라갑니다.")
        if self.duration > 300:
            w.append("스레드는 5분까지만 올릴 수 있습니다.")
        if self.duration > 900:
            w.append("인스타그램 릴스는 15분까지만 올릴 수 있습니다.")
        if self.duration < 3:
            w.append("인스타그램 릴스는 3초 이상이어야 합니다.")
        if self.width > self.height:
            w.append("가로 영상입니다. 유튜브는 쇼츠로 분류하지 않고, 인스타그램은 위아래가 잘려 보일 수 있습니다.")
        if self.size_bytes > META_MAX_BYTES and self.duration > 120:
            w.append("인스타·스레드용 영상은 20MB 이하로 압축해서 올라가므로, 2분이 넘으면 화질이 떨어질 수 있습니다.")
        return w


def _ffmpeg() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def _grab_frame(path: Path, t: float, scale_box: int | None = None) -> bytes:
    """t초 지점의 프레임 1장을 JPEG 바이트로. ffmpeg가 회전 메타데이터를 반영한다."""
    cmd = [_ffmpeg(), "-loglevel", "error", "-ss", f"{max(t, 0):.2f}", "-i", str(path),
           "-frames:v", "1"]
    if scale_box:
        cmd += ["-vf", f"scale={scale_box}:{scale_box}:force_original_aspect_ratio=decrease"]
    cmd += ["-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
    result = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW, timeout=60)
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip() or "프레임 추출 실패")
    return result.stdout


def probe(path: Path) -> VideoInfo:
    path = Path(path)
    reader = imageio_ffmpeg.read_frames(str(path))
    try:
        meta = next(reader)
    finally:
        reader.close()
    duration = float(meta.get("duration") or 0)
    # 휴대폰 영상은 회전 정보가 따로 저장되므로, 실제로 보이는 크기는 프레임을 뽑아서 확인한다.
    with Image.open(io.BytesIO(_grab_frame(path, 0))) as img:
        width, height = img.size
    return VideoInfo(
        path=path,
        duration=duration,
        width=width,
        height=height,
        fps=float(meta.get("fps") or 0),
        size_bytes=path.stat().st_size,
    )


def extract_frames(info: VideoInfo, out_dir: Path, count: int = 8, prefix: str = "frame",
                   box: int = 1024) -> list[tuple[float, Path]]:
    """영상 전체에서 고르게 count장을 뽑는다. 반환: [(초, 이미지 경로), ...]"""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"{prefix}_*.jpg"):
        old.unlink()
    count = max(1, min(count, int(info.duration) + 1)) if info.duration else 1
    frames = []
    for i in range(count):
        t = info.duration * (i + 0.5) / count if info.duration else 0
        target = out_dir / f"{prefix}_{i:02d}.jpg"
        try:
            target.write_bytes(_grab_frame(info.path, t, scale_box=box))
        except RuntimeError:
            continue  # 손상된 구간은 건너뛴다
        frames.append((t, target))
    if not frames:
        raise RuntimeError("영상에서 프레임을 하나도 추출하지 못했습니다.")
    return frames


def blog_candidate_count(duration: float) -> int:
    """블로그 사진을 고를 후보 장면 수. 짧은 영상은 8장, 긴 영상은 약 10초마다 (최대 24장)."""
    if duration <= 60:
        return 8
    return min(24, max(10, int(duration // 10) + 1))


# ── 업로드용 파일 준비 ───────────────────────────────────────

def transcode(info: VideoInfo, out_path: Path, max_bytes: int, max_video_kbps: int,
              progress_cb: Callable[[float], None] | None = None) -> Path:
    """H.264/AAC MP4로 다시 인코딩해 max_bytes 아래로 맞춘다 (세로 1080, 가로 1920 너비까지)."""
    duration = max(info.duration, 1.0)
    audio_kbps = 128
    target = int(max_bytes * 8 / 1000 / duration * 0.9) - audio_kbps
    video_kbps = max(300, min(max_video_kbps, target))
    scale = "scale=w='trunc(if(gt(iw,ih),min(1920,iw),min(1080,iw))/2)*2':h=-2"
    for _attempt in range(3):
        cmd = [
            _ffmpeg(), "-y", "-loglevel", "error", "-i", str(info.path),
            "-vf", scale, "-fpsmax", "60",
            "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-b:v", f"{video_kbps}k", "-maxrate", f"{int(video_kbps * 1.5)}k", "-bufsize", f"{video_kbps * 2}k",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(out_path),
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=_NO_WINDOW, text=True, encoding="utf-8", errors="replace")
        for line in proc.stdout:
            if line.startswith("out_time_us=") and progress_cb:
                try:
                    progress_cb(min(int(line.split("=", 1)[1]) / 1e6 / duration, 1.0))
                except ValueError:
                    pass
        err = proc.stderr.read()
        if proc.wait() != 0:
            raise RuntimeError(f"영상 압축 실패: {err.strip()[-500:]}")
        if out_path.stat().st_size <= max_bytes:
            if progress_cb:
                progress_cb(1.0)
            return out_path
        video_kbps = max(200, int(video_kbps * 0.75))
    raise RuntimeError(f"영상을 {max_bytes // (1024 * 1024)}MB 이하로 줄이지 못했습니다. 영상 길이를 줄여 주세요.")


def blog_photo_times(duration: float, count: int) -> list[float]:
    """블로그에 넣을 사진을 뽑을 시점 (영상 전체에 고르게)."""
    if count <= 0:
        return []
    return [duration * (i + 0.5) / count for i in range(count)] if duration else [0.0]


def _save_frame(path: Path, t: float, target: Path, width: int) -> bool:
    cmd = [_ffmpeg(), "-y", "-loglevel", "error", "-ss", f"{max(t, 0):.2f}", "-i", str(path),
           "-frames:v", "1", "-vf", f"scale=w='min({width},iw)':h=-2", "-q:v", "2", str(target)]
    result = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW, timeout=60)
    return result.returncode == 0 and target.exists()


def extract_blog_photos(info: VideoInfo, out_dir: Path, times: list[float], prefix: str = "blog",
                        width: int = 1080) -> list[Path | None]:
    """times의 장면을 사진으로 (너비 최대 width, 고화질 JPEG). 반환 순서 = times 순서 = [사진 1], [사진 2] …

    뽑지 못한 장면은 조금 앞에서 다시 뽑아 보고, 그래도 안 되면 None (번호가 밀리지 않게 자리는 남긴다).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"{prefix}_*.jpg"):
        old.unlink()
    photos: list[Path | None] = []
    for i, t in enumerate(times, start=1):
        target = out_dir / f"{prefix}_{i:02d}.jpg"
        ok = _save_frame(info.path, t, target, width) or _save_frame(info.path, max(t - 1.0, 0), target, width)
        photos.append(target if ok else None)
    return photos


def prepare_upload_files(info: VideoInfo, platforms: list[str], work_dir: Path,
                         progress_cb: Callable[[str, float], None] | None = None,
                         need_full_video: bool = False) -> dict[str, Path]:
    """봇에 보낼 파일을 만든다. 반환: {"meta": 인스타·스레드용, "youtube": 유튜브·블로그용 원본 화질} (필요한 것만).

    need_full_video: 유튜브를 고르지 않아도 원본 화질 영상이 필요할 때 (네이버 블로그 글에 영상을 넣을 때).
    원본이 이미 조건(MP4, 크기)에 맞으면 다시 인코딩하지 않는다. 두 용도가 같은 파일이면 하나만 올라간다.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    for old in work_dir.glob("*.mp4"):
        old.unlink()
    say = progress_cb or (lambda _label, _p: None)
    files: dict[str, Path] = {}
    is_mp4 = info.path.suffix.lower() == ".mp4"

    if "youtube" in platforms or need_full_video:
        if info.size_bytes <= YOUTUBE_MAX_BYTES:
            files["youtube"] = info.path
        else:
            files["youtube"] = transcode(info, work_dir / "video.mp4", YOUTUBE_MAX_BYTES, 12000,
                                         lambda p: say("유튜브용 영상 압축 중", p))

    if {"reels", "threads"} & set(platforms):
        yt = files.get("youtube")
        if is_mp4 and info.size_bytes <= META_MAX_BYTES:
            files["meta"] = info.path
        elif yt and yt.suffix.lower() == ".mp4" and yt.stat().st_size <= META_MAX_BYTES:
            files["meta"] = yt
        else:
            files["meta"] = transcode(info, work_dir / "meta.mp4", META_MAX_BYTES, 8000,
                                      lambda p: say("인스타·스레드용 영상 압축 중 (20MB 이하)", p))
    return files
