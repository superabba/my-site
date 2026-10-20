"""대본(장면 목록) + 음성 + 음악 + 배경을 합쳐 세로 mp4 영상을 만듭니다."""

from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageDraw

from . import aiimage
from . import music as music_mod
from .config import OUTPUT_DIR, WORK_DIR
from .ffmpeg import FFMPEG, SR, decode_audio, write_wav
from .text import (chunk_subtitles, find_font, headline_image, subtitle_image,
                   title_image, token_times)
from .tts import synthesize
from .visuals import ACCENTS, StockError, dim_mask, make_background, search_stock

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
XFADE = 0.3      # 장면 전환(겹쳐 사라지기) 시간
LEAD = 0.15      # 장면 시작 후 목소리가 나오기까지
POP = 0.12       # 자막이 톡 튀어나오는 시간


@dataclass
class RenderOptions:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    voice_on: bool = True
    voice: str = "ko-KR-SunHiNeural"
    speed: int = 0
    pitch: int = 0
    voice_volume: float = 1.0
    music_mode: str = "auto"          # auto | file | none
    music_file: str | None = None
    music_seed: int = 0
    music_volume: float = 0.25
    bg_mode: str = "gradient"         # gradient | ai_image | my_media | stock_video | stock_photo
    my_media: list[str] = field(default_factory=list)
    stock_keys: dict = field(default_factory=dict)   # {"pixabay": 키, "pexels": 키}
    gemini_key: str = ""                             # ai_image(나노바나나)용
    image_model: str = aiimage.DEFAULT_MODEL
    image_size: str = aiimage.DEFAULT_SIZE
    image_fallback: str = "stock_video"              # 그림을 못 만들면: stock_video | stock_photo | gradient
    output_dir: Path | None = None
    show_subtitles: bool = True
    sub_style: str = "outline"
    sub_max_chars: int = 12
    show_headline: bool = True
    show_title: bool = False
    show_progress: bool = True
    scene_gap: float = 0.25
    silent_cps: float = 5.0           # 목소리 없을 때 1초에 읽는 글자 수


@dataclass
class RenderResult:
    video: Path
    srt: Path
    info: Path
    duration: float
    warnings: list[str]
    credits: list[str]
    ai_images: int = 0  # 나노바나나로 그린 장면 수 (AI 합성 콘텐츠 표시에 씀)


ProgressFn = Callable[[float, str], None]


def _trim_silence(a: np.ndarray, thr: float = 0.004) -> tuple[np.ndarray, float]:
    loud = np.where(np.abs(a).max(axis=1) > thr)[0]
    if len(loud) == 0:
        return a, 0.0
    s = max(0, loud[0] - int(0.02 * SR))
    e = min(len(a), loud[-1] + int(0.08 * SR))
    return a[s:e], s / SR


def _safe_name(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", title).strip("_")
    return name[:40] or "shorts"


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _with_alpha(img: Image.Image, alpha: float) -> Image.Image:
    if alpha >= 0.999:
        return img
    out = img.copy()
    out.putalpha(img.getchannel("A").point(lambda a: int(a * alpha)))
    return out


def _paste_center(frame: Image.Image, img: Image.Image, cy: float, scale: float = 1.0, alpha: float = 1.0) -> None:
    if scale != 1.0:
        w, h = max(1, int(img.width * scale)), max(1, int(img.height * scale))
        img = img.resize((w, h), Image.BILINEAR)
    img = _with_alpha(img, alpha)
    x = (frame.width - img.width) // 2
    y = int(cy - img.height / 2)
    frame.paste(img, (x, y), img)


def render(script: dict, opts: RenderOptions, progress: ProgressFn | None = None) -> RenderResult:
    report = progress or (lambda p, msg: None)
    warnings: list[str] = []
    scenes = [s for s in script["scenes"] if s.get("narration", "").strip() or s.get("headline", "").strip()]
    if not scenes:
        raise ValueError("장면이 하나도 없어요. 대본을 먼저 만들어 주세요.")
    size = (opts.width, opts.height)
    mood = script.get("mood", "calm")
    font = find_font()

    # 1) 장면별 목소리와 길이 -------------------------------------------------
    plans = []
    for i, sc in enumerate(scenes):
        report(0.02 + 0.23 * i / len(scenes), f"목소리 만드는 중 ({i + 1}/{len(scenes)})")
        text = sc.get("narration", "").strip()
        voice, words = None, []
        if opts.voice_on and text:
            mp3, words = synthesize(text, opts.voice, opts.speed, opts.pitch)
            voice, cut = _trim_silence(decode_audio(mp3))
            words = [{**w, "start": w["start"] - cut, "end": w["end"] - cut} for w in words]
            dur = LEAD + len(voice) / SR + opts.scene_gap
        elif text:
            chars = len(text.replace(" ", ""))
            dur = float(np.clip(chars / opts.silent_cps + 1.0, 2.0, 12.0))
        else:
            dur = 2.0
        dur = max(dur, 1.2)
        plans.append({"scene": sc, "voice": voice, "words": words, "dur": dur})

    t = 0.0
    for p in plans:
        p["start"] = t
        t += p["dur"]
    total = t

    # 2) 배경 고르기 ---------------------------------------------------------
    report(0.26, "배경 준비 중")
    used: set = set()
    credits: list[str] = []
    stock_ok = bool(opts.stock_keys)
    ai_mode = opts.bg_mode == "ai_image"
    stock_kind = None
    if opts.bg_mode.startswith("stock"):
        stock_kind = "video" if opts.bg_mode == "stock_video" else "photo"
    elif ai_mode and opts.image_fallback.startswith("stock"):
        stock_kind = "video" if opts.image_fallback == "stock_video" else "photo"
    if opts.bg_mode.startswith("stock") and not stock_ok:
        warnings.append("스톡 검색용 API 키(Pixabay 등)가 없어 자동 배경으로 대신했어요.")

    # 나노바나나 그림은 장면마다 오래 걸려서 한꺼번에(동시에 3장씩) 만든다
    ai_images: dict[int, str] = {}
    if ai_mode:
        if not opts.gemini_key:
            warnings.append("Gemini API 키가 없어 AI 그림 대신 다른 배경을 썼어요.")
        else:
            todo = [(i, p["scene"]) for i, p in enumerate(plans) if not p["scene"].get("media")]
            report(0.27, f"장면 그림 그리는 중 (0/{len(todo)})")
            style = script.get("visual_style", "")
            done = 0
            fatal_seen = False

            def draw(item):
                i, sc = item
                desc = sc.get("image_prompt") or sc.get("narration") or sc.get("headline") or script.get("title", "")
                return i, aiimage.generate_image(desc, opts.gemini_key, style, opts.image_model, opts.image_size)

            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(draw, item) for item in todo]
                for fut in as_completed(futures):
                    done += 1
                    report(0.27 + 0.1 * done / max(len(todo), 1), f"장면 그림 그리는 중 ({done}/{len(todo)})")
                    try:
                        i, path = fut.result()
                        ai_images[i] = str(path)
                    except aiimage.ImageGenError as e:
                        if not (e.fatal and fatal_seen):  # 같은 키 문제는 한 번만 알린다
                            warnings.append(f"{e} 그 장면은 다른 배경을 썼어요.")
                        fatal_seen = fatal_seen or e.fatal

    my_idx = 0
    for i, p in enumerate(plans):
        sc = p["scene"]
        media = sc.get("media")
        if not media and opts.bg_mode == "my_media" and opts.my_media:
            media = opts.my_media[my_idx % len(opts.my_media)]
            my_idx += 1
        if not media and i in ai_images:
            media = ai_images[i]
        if not media and stock_kind and stock_ok:
            report(0.37, f"스톡 영상 찾는 중 ({i + 1}/{len(plans)})")
            query = sc.get("keywords") or sc.get("headline") or sc.get("narration", "")[:30]
            try:
                found = search_stock(stock_kind, query, opts.stock_keys, p["dur"], used)
                if found:
                    media = str(found.path)
                    credits.append(f"장면 {i + 1}: {found.credit}")
                else:
                    warnings.append(f"장면 {i + 1}: '{query}' 검색 결과가 없어 자동 배경을 썼어요.")
            except StockError as e:
                warnings.append(f"{e} 이후 장면은 자동 배경을 써요.")
                stock_ok = False
            except Exception as e:  # 다운로드 실패 등
                warnings.append(f"장면 {i + 1}: 스톡 다운로드 실패({e}). 자동 배경을 썼어요.")
        p["bg"] = make_background(media, size, opts.fps, p["dur"], mood, i, p["start"])
        p["media"] = media

    # 3) 소리 합치기 ----------------------------------------------------------
    report(0.37, "배경음악과 목소리 섞는 중")
    n = int(np.ceil(total * SR))
    voice_track = np.zeros((n, 2), np.float32)
    for p in plans:
        if p["voice"] is not None:
            s = int((p["start"] + LEAD) * SR)
            seg = p["voice"][: max(0, n - s)]
            voice_track[s:s + len(seg)] += seg
    bgm = None
    if opts.music_mode == "auto":
        bgm = music_mod.generate_bgm(mood, total, seed=opts.music_seed)
    elif opts.music_mode == "file" and opts.music_file:
        bgm = music_mod.load_music_file(opts.music_file, total)
    music_gain = opts.music_volume if opts.voice_on else min(1.0, opts.music_volume * 2.2)
    audio = music_mod.mix(voice_track, bgm, opts.voice_volume, music_gain)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    wav_path = WORK_DIR / f"audio_{stamp}.wav"
    write_wav(wav_path, audio)

    # 4) 글씨 이미지 미리 그리기 ------------------------------------------------
    report(0.4, "자막 준비 중")
    accent = ACCENTS.get(mood, ACCENTS["calm"])
    srt_lines = []
    for p in plans:
        sc = p["scene"]
        text = sc.get("narration", "").strip()
        sub_end = p["dur"] - opts.scene_gap * 0.5
        tokens = token_times(text, p["words"], sub_end, lead=LEAD) if text else []
        p["subs"] = chunk_subtitles(tokens, opts.sub_max_chars, p["dur"]) if tokens else []
        if p["subs"] and p is plans[0]:
            p["subs"][0]["start"] = -1.0  # 첫 자막은 첫 프레임부터
        for c in p["subs"]:
            c["img"] = subtitle_image(c["text"], opts.sub_style, font, opts.width) if opts.show_subtitles else None
            srt_lines.append((max(0.0, p["start"] + c["start"]), p["start"] + min(c["end"], sub_end), c["text"]))
        head = sc.get("headline", "").strip()
        p["head_img"] = headline_image(head, accent, font, opts.width) if (head and opts.show_headline) else None
    title_img = title_image(script.get("title", ""), font, opts.width) if (opts.show_title and script.get("title")) else None

    masks = {}
    for p in plans:
        key = round(p["bg"].dim, 2)
        if key not in masks:
            masks[key] = dim_mask(size, key)
    black = Image.new("RGB", size, (0, 0, 0))

    # 5) 영상 만들기 ---------------------------------------------------------
    title = script.get("title") or "shorts"
    out_dir = opts.output_dir or OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{stamp}_{_safe_name(title)}.mp4"
    log_path = WORK_DIR / f"ffmpeg_{stamp}.log"
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{opts.width}x{opts.height}",
             "-r", str(opts.fps), "-i", "-", "-i", str(wav_path),
             "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest",
             "-movflags", "+faststart", str(out_path)],
            stdin=subprocess.PIPE, stderr=log, creationflags=_NO_WINDOW,
        )
        n_frames = int(np.ceil(total * opts.fps))
        bar_h = max(4, opts.height // 240)
        si = -1
        prev_frame: Image.Image | None = None
        last_frame: Image.Image | None = None
        try:
            for k in range(n_frames):
                T = k / opts.fps
                while si + 1 < len(plans) and T >= plans[si + 1]["start"]:
                    if si >= 0:
                        plans[si]["bg"].close()
                    si += 1
                    plans[si]["bg"].open()
                    prev_frame = last_frame
                p = plans[si]
                lt = T - p["start"]

                frame = p["bg"].frame(lt)
                if frame.size != size:
                    frame = frame.resize(size, Image.BILINEAR)
                frame.paste(black, (0, 0), masks[round(p["bg"].dim, 2)])

                if title_img is not None:
                    _paste_center(frame, title_img, opts.height * 0.085)
                if p["head_img"] is not None:
                    # 첫 장면은 첫 프레임(썸네일)부터 글씨가 보이도록 애니메이션 없이 바로 표시
                    a = 1.0 if si == 0 else min(1.0, lt / 0.35)
                    ease = 1 - (1 - a) ** 3
                    cy = opts.height * 0.2 + (1 - ease) * opts.height * 0.02
                    _paste_center(frame, p["head_img"], cy, alpha=ease)
                for c in p["subs"]:
                    if c["img"] is not None and c["start"] <= lt < c["end"]:
                        a = min(1.0, (lt - c["start"]) / POP)
                        _paste_center(frame, c["img"], opts.height * 0.66, scale=0.9 + 0.1 * a, alpha=0.4 + 0.6 * a)
                        break
                if opts.show_progress:
                    d = ImageDraw.Draw(frame)
                    d.rectangle((0, 0, opts.width, bar_h), fill=(0, 0, 0))
                    d.rectangle((0, 0, int(opts.width * (T + 1 / opts.fps) / total), bar_h), fill=accent)
                if prev_frame is not None and lt < XFADE:
                    frame = Image.blend(prev_frame, frame, lt / XFADE)

                proc.stdin.write(frame.tobytes())
                last_frame = frame
                if k % 10 == 0:
                    report(0.42 + 0.56 * k / n_frames, f"영상 만드는 중 ({int(T)}초 / {int(total)}초)")
            proc.stdin.close()
            code = proc.wait()
        except (BrokenPipeError, OSError):
            code = -1
        finally:
            # 중간에 멈추거나(화면에서 다시 누름 등) 실패해도 ffmpeg와 임시 파일이 남지 않게 정리
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for p in plans:
                p["bg"].close()
            wav_path.unlink(missing_ok=True)
            if proc.returncode != 0:
                out_path.unlink(missing_ok=True)
    if code != 0:
        err = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
        raise RuntimeError(f"영상 인코딩에 실패했어요.\n{err}")
    log_path.unlink(missing_ok=True)

    # 6) 자막 파일과 게시용 글 저장 -----------------------------------------------
    srt_path = out_path.with_suffix(".srt")
    srt_path.write_text(
        "\n".join(f"{i + 1}\n{_srt_time(s)} --> {_srt_time(e)}\n{txt}\n" for i, (s, e, txt) in enumerate(srt_lines)),
        encoding="utf-8",
    )
    info_path = out_path.with_suffix(".txt")
    tags = " ".join(f"#{h}" for h in script.get("hashtags", []))
    info_path.write_text(
        f"제목: {title}\n\n설명:\n{script.get('caption', '')}\n\n{tags}\n\n"
        "대본:\n" + "\n".join(p["scene"].get("narration", "") for p in plans)
        + ("\n\n배경 출처:\n" + "\n".join(credits) if credits else ""),
        encoding="utf-8",
    )
    saved = {**script, "ai_images": len(ai_images)}
    out_path.with_suffix(".json").write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    report(1.0, "완성!")
    return RenderResult(out_path, srt_path, info_path, total, warnings, credits, len(ai_images))
