"""카드뉴스 슬라이드로 인스타그램 릴스용 세로 영상(1080x1920, MP4) 만들기.

- 가운데에 카드뉴스 슬라이드, 위에 제목, 아래에 진행 바와 저장 유도 문구
- 글자 수에 맞춰 한 장 2.5~4초(전체 약 20~25초) 보여주고, 장면 사이는 짧게 겹쳐 전환
- 오디오 (API로는 인스타그램 음악 라이브러리를 쓸 수 없으므로 직접 넣는다)
  REELS_AUDIO=auto(기본): 잔잔한 배경음악을 게시물마다 새로 합성 (music.py)
  REELS_AUDIO=음원 파일 또는 폴더: 그 음원(폴더면 그중 하나)을 사용
  REELS_AUDIO=none: 무음
- ffmpeg는 imageio-ffmpeg 패키지에 들어 있는 실행 파일을 쓴다 (별도 설치 불필요)
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from .config import ROOT
from .llm import CardNews
from .music import pick_user_track, write_track
from .render import THEMES, Fonts, draw_lines, ensure_fonts, fit, render_slide, text_w

log = logging.getLogger(__name__)

RW, RH = 1080, 1920
SLIDE_W = 900  # 오른쪽 릴스 버튼·아래쪽 캡션 영역을 피하도록 가운데에 작게 배치
SLIDE_H = SLIDE_W * 1350 // 1080
SLIDE_Y = 300
BAR_Y = SLIDE_Y + SLIDE_H + 50
FADE_S = 0.25  # 장면 전환(겹침) 시간


def slide_seconds(slide) -> float:
    """읽기 적당한 노출 시간.

    제목·강조 문구는 확실히 읽고 본문은 훑어볼 수 있을 만큼(1초 + 글자 수/35),
    한 장 2.5~4초. 표지는 2.5초, 마지막 저장 유도 장은 2초.
    """
    if slide.kind == "cover":
        return 2.5
    if slide.kind == "cta":
        return 2.0
    chars = len((slide.heading + slide.body + slide.highlight).replace("\n", "").replace(" ", ""))
    return round(max(2.5, min(4.0, 1.0 + chars / 35)), 2)


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


def _audio_input(audio, out_path: Path, total: float) -> tuple[Path | None, str]:
    """(음원 경로, ffmpeg 오디오 필터). 무음이면 (None, "")."""
    mode = str(audio or "auto").strip()
    fade_out = f"afade=t=out:st={max(0.0, total - 1.8):.2f}:d=1.8"
    if mode.lower() in {"none", "off", "silent", "false"}:
        return None, ""
    if mode.lower() not in {"auto", "generated"}:
        src = Path(mode) if Path(mode).is_absolute() else ROOT / mode
        track = pick_user_track(src, out_path.parent.name)
        if track:
            log.info("릴스 배경음: %s", track.name)
            return track, f"afade=t=in:d=0.5,{fade_out}"
        log.warning("REELS_AUDIO 음원을 찾지 못해 배경음악을 합성합니다: %s", src)
    wav = write_track(out_path.with_name("reel_music.wav"), total + 1, out_path.parent.name)
    # 은은한 공간감(에코) + 고음 살짝 깎기 + 자연스러운 시작·끝
    return wav, f"lowpass=f=6000,aecho=0.8:0.5:70|150:0.22|0.12,afade=t=in:d=1.2,{fade_out},volume=2.0"


def render_reel(
    card: CardNews,
    slide_paths: list[Path],
    out_path: Path,
    fonts_dir: Path,
    theme: str,
    handle: str,
    audio: str | Path | None = "auto",
    fps: int = 30,
    seconds_per_slide: float | None = None,
) -> tuple[Path, Path]:
    """릴스 영상과 커버 이미지를 만든다. (영상 경로, 커버 경로) 반환."""
    import imageio_ffmpeg

    fonts = Fonts(ensure_fonts(fonts_dir))
    title = card.slides[0].heading.replace("\n", " ").strip()
    images = [Image.open(p) for p in slide_paths]
    if card.slides and card.slides[0].kind == "cover":  # 영상에선 '넘겨보세요' 안내가 어색하므로 표지만 다시 그림
        images[0] = render_slide(card.slides[0], 1, len(card.slides), fonts, theme, handle, swipe_hint=False)
    scenes = [compose_frame(img, title, fonts, theme, handle) for img in images]
    # seconds_per_slide를 주면 모든 장을 같은 시간으로, 아니면 글자 수 기준
    durations = [seconds_per_slide or slide_seconds(sl) for sl, _ in zip(card.slides, scenes)]
    total = sum(durations)

    cover = out_path.with_name("reel_cover.jpg")
    _progress(scenes[0], theme, 0).save(cover, "JPEG", quality=92)

    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{RW}x{RH}", "-r", str(fps), "-i", "-",
    ]
    track, afilter = _audio_input(audio, out_path, total)
    if track:
        # 영상 길이에 맞춰 자르고(-shortest) 앞뒤를 부드럽게
        cmd += ["-stream_loop", "-1", "-i", str(track), "-af", afilter]
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
    out_path.with_name("reel_music.wav").unlink(missing_ok=True)  # 합성한 임시 음원은 남기지 않음
    if proc.wait() != 0:
        raise RuntimeError(f"릴스 영상 인코딩 실패: {err[-500:]}")
    log.info("릴스 영상 생성: %s (%.1f초)", out_path.name, total)
    return out_path, cover
