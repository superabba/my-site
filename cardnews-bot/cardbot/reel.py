"""카드뉴스 슬라이드로 인스타그램 릴스용 세로 영상(1080x1920, MP4) 만들기.

- 가운데에 카드뉴스 슬라이드, 위에 제목, 아래에 진행 바와 저장 유도 문구
- 슬라이드 글자 수에 맞춰 보여주는 시간을 정하고, 장면 사이는 짧게 겹쳐 전환
- 오디오: REELS_AUDIO에 저작권 문제없는 음원 파일을 지정하면 배경음으로 쓰고,
  없으면 무음 트랙을 넣는다 (API로는 인스타그램 음악 라이브러리를 쓸 수 없음)
- ffmpeg는 imageio-ffmpeg 패키지에 들어 있는 실행 파일을 쓴다 (별도 설치 불필요)
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from .llm import CardNews
from .render import THEMES, Fonts, draw_lines, ensure_fonts, fit, render_slide, text_w

log = logging.getLogger(__name__)

RW, RH = 1080, 1920
SLIDE_W = 900  # 오른쪽 릴스 버튼·아래쪽 캡션 영역을 피하도록 가운데에 작게 배치
SLIDE_H = SLIDE_W * 1350 // 1080
SLIDE_Y = 300
BAR_Y = SLIDE_Y + SLIDE_H + 50
FADE_S = 0.35


def slide_seconds(heading: str, body: str, kind: str) -> float:
    """읽는 데 필요한 시간 (한 장 3~6초)."""
    if kind == "cover":
        return 3.0
    chars = len((heading + body).replace("\n", "").replace(" ", ""))
    return max(3.0, min(6.0, 2.0 + chars / 14))


def _gradient(theme) -> Image.Image:
    top, bot = theme["bg1"], theme["bg2"]
    col = Image.new("RGB", (1, RH))
    for y in range(RH):
        t = y / (RH - 1)
        col.putpixel((0, y), tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    return col.resize((RW, RH))


def compose_frame(slide_img: Image.Image, title: str, fonts: Fonts, theme_name: str, handle: str) -> Image.Image:
    """애니메이션 없는 한 장면 (진행 바 제외)."""
    theme = THEMES.get(theme_name, THEMES["midnight"])
    frame = _gradient(theme)
    d = ImageDraw.Draw(frame)

    # 위: 제목 (최대 2줄)
    f, lines, lh = fit(d, title, fonts, "ExtraBold", 64, RW - 160, 170, min_size=40, spacing=1.25)
    draw_lines(d, (80, 90), lines[:2], f, lh, theme["fg"])

    # 가운데: 슬라이드 + 그림자
    slide = slide_img.convert("RGB").resize((SLIDE_W, SLIDE_H), Image.LANCZOS)
    x = (RW - SLIDE_W) // 2
    shadow = Image.new("L", (SLIDE_W + 80, SLIDE_H + 80), 0)
    ImageDraw.Draw(shadow).rounded_rectangle((40, 50, SLIDE_W + 40, SLIDE_H + 50), radius=28, fill=110)
    shadow = shadow.filter(ImageFilter.GaussianBlur(24))
    frame.paste((0, 0, 0), (x - 40, SLIDE_Y - 40), shadow)
    mask = Image.new("L", (SLIDE_W, SLIDE_H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, SLIDE_W, SLIDE_H), radius=28, fill=255)
    frame.paste(slide, (x, SLIDE_Y), mask)

    # 아래: 저장 유도 + 핸들
    cta = "저장해두고 필요할 때 꺼내 보세요"
    cf = fonts.get("Bold", 40)
    d.text(((RW - text_w(d, cta, cf)) / 2, BAR_Y + 50), cta, font=cf, fill=theme["accent"])
    if handle:
        hf = fonts.get("Regular", 32)
        h = f"@{handle.lstrip('@')}"
        d.text(((RW - text_w(d, h, hf)) / 2, BAR_Y + 115), h, font=hf, fill=theme["sub"])
    return frame


def _progress(frame: Image.Image, theme_name: str, ratio: float) -> Image.Image:
    theme = THEMES.get(theme_name, THEMES["midnight"])
    out = frame.copy()
    d = ImageDraw.Draw(out)
    x0, x1 = (RW - SLIDE_W) // 2, (RW + SLIDE_W) // 2
    d.rounded_rectangle((x0, BAR_Y, x1, BAR_Y + 10), radius=5, fill=theme["sub"])
    d.rounded_rectangle((x0, BAR_Y, x0 + max(10, int((x1 - x0) * ratio)), BAR_Y + 10), radius=5, fill=theme["accent"])
    return out


def render_reel(
    card: CardNews,
    slide_paths: list[Path],
    out_path: Path,
    fonts_dir: Path,
    theme: str,
    handle: str,
    audio: Path | None = None,
    fps: int = 30,
) -> tuple[Path, Path]:
    """릴스 영상과 커버 이미지를 만든다. (영상 경로, 커버 경로) 반환."""
    import imageio_ffmpeg

    fonts = Fonts(ensure_fonts(fonts_dir))
    title = card.slides[0].heading.replace("\n", " ").strip()
    images = [Image.open(p) for p in slide_paths]
    if card.slides and card.slides[0].kind == "cover":  # 영상에선 '넘겨보세요' 안내가 어색하므로 표지만 다시 그림
        images[0] = render_slide(card.slides[0], 1, len(card.slides), fonts, theme, handle, swipe_hint=False)
    scenes = [compose_frame(img, title, fonts, theme, handle) for img in images]
    durations = [
        slide_seconds(s.heading, s.body, s.kind) for s, _ in zip(card.slides, slide_paths)
    ]
    total = sum(durations)

    cover = out_path.with_name("reel_cover.jpg")
    _progress(scenes[0], theme, 0).save(cover, "JPEG", quality=92)

    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{RW}x{RH}", "-r", str(fps), "-i", "-",
    ]
    if audio and audio.exists():
        # 영상 길이에 맞춰 자르고 끝부분을 부드럽게 줄인다
        cmd += ["-stream_loop", "-1", "-i", str(audio),
                "-af", f"afade=t=out:st={max(0.0, total - 1.5):.2f}:d=1.5"]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    cmd += [
        "-map", "0:v", "-map", "1:a", "-shortest",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-profile:v", "high", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-movflags", "+faststart", str(out_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    fade_frames = int(FADE_S * fps)
    elapsed = 0.0
    try:
        for i, (scene, dur) in enumerate(zip(scenes, durations)):
            n = int(round(dur * fps))
            nxt = scenes[i + 1] if i + 1 < len(scenes) else None
            for k in range(n):
                img = scene
                if nxt is not None and k >= n - fade_frames:  # 다음 장면으로 교차 전환
                    img = Image.blend(scene, nxt, (k - (n - fade_frames) + 1) / (fade_frames + 1))
                ratio = (elapsed + k / fps) / total
                proc.stdin.write(_progress(img, theme, ratio).tobytes())
            elapsed += n / fps
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode(errors="replace")
    if proc.wait() != 0:
        raise RuntimeError(f"릴스 영상 인코딩 실패: {err[-500:]}")
    log.info("릴스 영상 생성: %s (%.1f초)", out_path.name, total)
    return out_path, cover
