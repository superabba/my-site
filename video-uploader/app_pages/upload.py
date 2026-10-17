import re
import shutil
import time
from pathlib import Path

import streamlit as st

from core import analyzer, config, media, transcribe
from core.config import PLATFORM_LABELS, PRIVACY_LABELS
from core.github_bot import GitHubBot, GitHubError, new_video_id

settings = config.load_settings()
PLATFORMS = list(PLATFORM_LABELS)


# ── 도우미 ──────────────────────────────────────────────────

def parse_hashtags(text: str) -> list[str]:
    seen, tags = set(), []
    for raw in re.split(r"[\s,]+", text or ""):
        tag = raw.strip().lstrip("#")
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
    return tags


def tag_line(tags: list[str], limit: int | None = None) -> str:
    return " ".join(f"#{t}" for t in (tags[:limit] if limit else tags))


def save_upload(uploaded) -> Path:
    for old in config.VIDEO_DIR.iterdir():
        try:
            old.unlink()
        except OSError:
            pass
    safe_name = re.sub(r"[^\w.\-]", "_", uploaded.name)
    target = config.VIDEO_DIR / safe_name
    uploaded.seek(0)
    with open(target, "wb") as f:
        shutil.copyfileobj(uploaded, f, length=8 * 1024 * 1024)
    return target


def on_video_change():
    # 사용자가 업로더에서 파일을 지웠을 때만 호출된다(페이지 이동 때는 호출되지 않음).
    if st.session_state.get("video_file") is None:
        st.session_state.video = None


def copy_desc_to_caption():
    st.session_state.ig_caption = st.session_state.yt_desc


def copy_caption_to_threads():
    st.session_state.threads_text = st.session_state.ig_caption[:500]


@st.cache_resource(max_entries=1, show_spinner=False)
def get_whisper(model_size: str):
    return transcribe.load_model(model_size)


def run_analysis(info: media.VideoInfo, hint: str, use_voice: bool) -> None:
    with st.status("영상 분석 중…", expanded=True) as status:
        try:
            st.write("영상에서 장면을 뽑는 중…")
            frames = media.extract_frames(info, config.FRAME_DIR)
        except Exception as e:
            status.update(label="분석 실패", state="error")
            st.error(f"영상을 읽지 못했습니다: {e}")
            return

        transcript = None
        if use_voice:
            st.write("음성을 글자로 바꾸는 중… (처음 한 번은 모델 다운로드로 몇 분 걸려요)")
            try:
                transcript = transcribe.transcribe(get_whisper(settings["whisper_model"]), info.path)
            except Exception as e:
                st.warning(f"음성 인식에 실패해 화면만 보고 작성합니다: {e}")

        st.write("Claude가 문구를 쓰는 중…")
        try:
            result = analyzer.analyze_video(
                settings["anthropic_api_key"], settings["claude_model"],
                frames, info.duration, transcript, hint.strip() or None,
            )
        except analyzer.AnalysisError as e:
            status.update(label="분석 실패", state="error")
            st.error(str(e))
            return
        status.update(label="분석 완료", state="complete")
    st.session_state.pending_fill = result
    st.rerun()


def run_publish(video: dict, targets: list[str], post: dict) -> None:
    """영상을 봇 저장소로 보내고, 워크플로가 게시를 마칠 때까지 지켜본다."""
    info: media.VideoInfo = video["info"]
    with st.status("카드뉴스 봇으로 보내는 중…", expanded=True) as status:
        step = st.empty()
        bar = st.progress(0.0)
        video_id = None
        try:
            bot = GitHubBot(settings["github_token"], settings["github_repo"])
            repo = bot.check()

            step.write("업로드할 영상 준비 중…")
            files = media.prepare_upload_files(
                info, targets, config.WORK_DIR,
                lambda label, p: bar.progress(p, text=f"{label} {p * 100:.0f}%"),
            )
            names: dict[str, Path] = {}
            post_files = {}
            if "youtube" in files:
                yt_name = "video" + files["youtube"].suffix.lower()
                names[yt_name] = files["youtube"]
                post_files["youtube"] = yt_name
            if "meta" in files:
                same = files.get("youtube") == files["meta"]
                meta_name = post_files["youtube"] if same else "meta.mp4"
                names.setdefault(meta_name, files["meta"])
                post_files["meta"] = meta_name
            post["files"] = post_files

            video_id = new_video_id()
            step.write("GitHub에 영상 올리는 중…")
            bot.upload(video_id, names, post,
                       lambda p: bar.progress(p, text=f"GitHub에 올리는 중 {p * 100:.0f}%"))
            try:
                bot.dispatch(video_id, repo["default_branch"])
            except GitHubError:
                bot.delete_video_branch(video_id)
                raise
            st.session_state.uploaded_keys.add(video["file_id"])

            step.write("봇이 실행되기를 기다리는 중…")
            bar.progress(0.0, text="실행 대기")
            run = bot.find_run(video_id)
            run_url = run["html_url"] if run else (
                f"https://github.com/{bot.repo}/actions/workflows/video-upload.yml")
            config.add_history(video_id, info.path.name, post["title"], targets, run_url)
            if not run:
                raise GitHubError("봇 실행을 찾지 못했습니다. 잠시 뒤 '업로드 기록'에서 결과를 확인해 주세요.")

            step.write(f"봇이 게시 중입니다. 이 창을 닫아도 게시는 계속됩니다. [실행 보기]({run_url})")
            started = time.time()
            while run.get("status") != "completed" and time.time() - started < 30 * 60:
                elapsed = int(time.time() - started)
                bar.progress(min(elapsed / 300, 0.95), text=f"게시 중… {elapsed // 60}분 {elapsed % 60}초 (보통 2~5분)")
                time.sleep(8)
                run = bot.get_run(run["id"])

            record = bot.read_record(video_id)
            if record:
                state, results = config.status_from_record(record)
            elif run.get("status") != "completed":
                state, results = "진행 중", {}
            else:
                state, results = "실패", {}
            config.update_history(video_id, status=state, results=results)
            st.session_state.last_result = {"status": state, "results": results, "run_url": run_url}
            status.update(label=f"게시 {state}", state="complete" if state == "완료" else "error")
        except (GitHubError, RuntimeError, OSError) as e:
            status.update(label="보내기 실패", state="error")
            st.session_state.last_result = {"status": "실패", "results": {}, "error": str(e), "run_url": ""}
            if video_id:
                config.update_history(video_id, status="실패", results={"오류": str(e)})
    st.rerun()


# ── 상태 초기화 ─────────────────────────────────────────────

st.session_state.setdefault("video", None)
st.session_state.setdefault("ai_summary", "")
st.session_state.setdefault("uploaded_keys", set())
st.session_state.setdefault("last_result", None)
for key in ("yt_title", "yt_desc", "ig_caption", "threads_text"):
    st.session_state.setdefault(key, "")
st.session_state.setdefault("hashtags", settings["default_hashtags"])

# AI 결과는 입력칸이 그려지기 전에 넣어야 한다.
if pending := st.session_state.pop("pending_fill", None):
    st.session_state.yt_title = pending["title"]
    st.session_state.yt_desc = pending["youtube_description"]
    st.session_state.ig_caption = pending["instagram_caption"]
    st.session_state.threads_text = pending["threads_text"][:500]
    merged = parse_hashtags(settings["default_hashtags"]) + pending["hashtags"]
    st.session_state.hashtags = tag_line(parse_hashtags(" ".join(merged)))
    st.session_state.ai_summary = pending["summary"]


# ── 1. 영상 선택 ────────────────────────────────────────────

with st.container(border=True):
    st.subheader("1. 영상 선택", icon=":material/movie:")
    uploaded = st.file_uploader(
        "올릴 영상 파일 (mp4, mov)", type=["mp4", "mov", "m4v"],
        max_upload_size=2048, key="video_file", on_change=on_video_change,
    )
    current = st.session_state.video
    if uploaded is not None and (current is None or current["file_id"] != uploaded.file_id):
        with st.spinner("영상 정보를 읽는 중…"):
            try:
                path = save_upload(uploaded)
                st.session_state.video = {"file_id": uploaded.file_id, "info": media.probe(path)}
                # 새 영상이면 이전 영상용 문구를 비운다.
                for key in ("yt_title", "yt_desc", "ig_caption", "threads_text", "ai_summary"):
                    st.session_state[key] = ""
                st.session_state.hashtags = settings["default_hashtags"]
                st.session_state.last_result = None
            except Exception as e:
                st.session_state.video = None
                st.error(f"영상을 읽지 못했습니다: {e}")

    video = st.session_state.video
    if video:
        info: media.VideoInfo = video["info"]
        col_video, col_info = st.columns([1, 2])
        with col_video:
            st.video(str(info.path))
        with col_info:
            st.markdown(f"**{info.path.name}**")
            st.caption(
                f"{info.duration:.1f}초 · {info.width}×{info.height} ({info.orientation}) · {info.size_mb:.1f}MB"
            )
            warnings = info.warnings()
            for w in warnings:
                st.warning(w, icon=":material/warning:")
            if not warnings:
                st.success("쇼츠·릴스·스레드 규격에 맞는 영상입니다.", icon=":material/check_circle:")


# ── 2. 게시 내용 ────────────────────────────────────────────

with st.container(border=True):
    st.subheader("2. 게시 내용", icon=":material/edit_note:")

    with st.expander("AI로 자동 작성", icon=":material/auto_awesome:", expanded=not st.session_state.yt_title):
        hint = st.text_input(
            "영상에 대한 메모 (선택)", key="ai_hint",
            placeholder="예: 성수동 신상 카페 소개, 밝고 경쾌한 말투로",
            help="영상만으로 알 수 없는 정보(장소·제품명 등)나 원하는 분위기를 적으면 반영합니다.",
        )
        voice_ok = transcribe.available()
        use_voice = st.toggle(
            "영상 속 말소리도 듣고 작성", value=voice_ok, disabled=not voice_ok,
            help="말소리 인식 기능이 설치돼 있어야 합니다. 프로그램 폴더의 install_voice.bat 으로 설치하세요.",
        )
        has_key = bool(settings["anthropic_api_key"])
        if not has_key:
            st.caption("계정 설정에서 Claude API 키를 넣으면 사용할 수 있습니다. 키가 없어도 아래 칸에 직접 쓰면 됩니다.")
        elif not video:
            st.caption("먼저 영상을 선택해 주세요.")
        if st.button("AI로 내용 채우기", icon=":material/auto_awesome:", disabled=not (has_key and video)):
            run_analysis(video["info"], hint, use_voice)

    if st.session_state.ai_summary:
        st.caption(f":material/smart_toy: AI가 파악한 영상 내용: {st.session_state.ai_summary}")

    st.text_input("유튜브 제목", key="yt_title", max_chars=100, persist_state="session")
    st.text_area("유튜브 설명", key="yt_desc", height=110, persist_state="session")
    st.text_area("인스타그램 캡션", key="ig_caption", height=130, max_chars=2200, persist_state="session")
    st.text_area("스레드 글", key="threads_text", height=90, max_chars=500, persist_state="session")
    with st.container(horizontal=True):
        st.button("유튜브 설명 → 인스타 캡션", icon=":material/content_copy:", type="tertiary",
                  on_click=copy_desc_to_caption)
        st.button("인스타 캡션 → 스레드 글", icon=":material/content_copy:", type="tertiary",
                  on_click=copy_caption_to_threads)
    st.text_input(
        "해시태그 (유튜브·인스타 공통)", key="hashtags", persist_state="session",
        placeholder="#일상 #브이로그  (띄어쓰기로 구분)",
    )


# ── 3. 업로드 ──────────────────────────────────────────────

bot_ready = bool(settings["github_token"] and settings["github_repo"])

with st.container(border=True):
    st.subheader("3. 업로드", icon=":material/rocket_launch:")
    if bot_ready:
        st.badge(f"카드뉴스 봇 · {settings['github_repo']}", icon=":material/check:", color="green")
    else:
        st.badge("카드뉴스 봇 연결 안 됨", icon=":material/link_off:", color="gray")
    st.caption("카드뉴스와 다른 '영상 전용 계정'으로 올라가고, 카드뉴스 자동 발행 일정에도 영향이 없습니다.")

    targets = st.pills(
        "올릴 곳", PLATFORMS, selection_mode="multi", format_func=PLATFORM_LABELS.get,
        default=[p for p in settings["default_platforms"] if p in PLATFORMS], key="targets",
    ) or []

    privacy = settings["yt_privacy"]
    if "youtube" in targets:
        privacy = st.segmented_control(
            "유튜브 공개 범위", list(PRIVACY_LABELS), format_func=PRIVACY_LABELS.get,
            default=settings["yt_privacy"], required=True, key="yt_privacy",
        )

    tags = parse_hashtags(st.session_state.hashtags)
    ig_full = f"{st.session_state.ig_caption.strip()}\n\n{tag_line(tags, 30)}".strip()
    threads_final = (st.session_state.threads_text.strip() or st.session_state.ig_caption.strip())[:500]

    problems = []
    if not video:
        problems.append("영상을 선택해 주세요.")
    if not targets:
        problems.append("올릴 곳을 하나 이상 고르세요.")
    if not bot_ready:
        problems.append("계정 설정에서 카드뉴스 봇(GitHub) 연결을 먼저 해 주세요.")
    if "youtube" in targets and not st.session_state.yt_title.strip():
        problems.append("유튜브 제목을 입력해 주세요.")
    if "reels" in targets:
        if not st.session_state.ig_caption.strip() and not tags:
            problems.append("인스타그램 캡션을 입력해 주세요.")
        if len(ig_full) > 2200:
            problems.append(f"인스타그램 캡션이 해시태그 포함 {len(ig_full)}자입니다. 2,200자 이하로 줄여 주세요.")
        if len(tags) > 30:
            st.caption("인스타그램은 해시태그를 30개까지만 허용해서 앞의 30개만 올립니다.")
    if "threads" in targets and not threads_final:
        problems.append("스레드 글(또는 인스타 캡션)을 입력해 주세요.")

    if video and targets:
        with st.expander("실제로 올라갈 문구 미리보기", icon=":material/visibility:"):
            if "youtube" in targets:
                st.markdown("**유튜브 쇼츠**")
                desc = f"{st.session_state.yt_desc.strip()}\n\n{tag_line(tags)}".strip()
                st.text(f"[제목] {st.session_state.yt_title.strip()}\n\n{desc}\n\n(#Shorts는 자동으로 붙습니다)")
            if "reels" in targets:
                st.markdown("**인스타 릴스**")
                st.text(ig_full)
            if "threads" in targets:
                st.markdown("**스레드**")
                st.text(threads_final)

    allow = True
    if video and video["file_id"] in st.session_state.uploaded_keys:
        st.warning("이 영상은 이미 봇으로 보냈습니다.", icon=":material/content_copy:")
        allow = st.checkbox("같은 영상을 한 번 더 올리기", key="allow_again")

    for p in problems:
        st.caption(f":material/error: {p}")

    if st.button("업로드 시작", type="primary", icon=":material/upload:", width="stretch",
                 disabled=bool(problems) or not allow):
        title = st.session_state.yt_title.strip() or (st.session_state.ig_caption.strip().split("\n")[0][:100])
        run_publish(video, targets, {
            "title": title,
            "youtube_description": st.session_state.yt_desc.strip(),
            "instagram_caption": st.session_state.ig_caption.strip(),
            "threads_text": threads_final,
            "hashtags": tags,
            "platforms": targets,
            "youtube_privacy": privacy,
            "source_name": video["info"].path.name,
        })

    result = st.session_state.last_result
    if result:
        if result.get("error"):
            st.error(f"보내기 실패: {result['error']}", icon=":material/error:")
        elif result["status"] == "진행 중":
            st.info("봇이 아직 게시 중입니다. '업로드 기록'에서 결과를 확인해 주세요.", icon=":material/pending:")
        elif not result["results"]:
            st.error("봇 실행이 실패했습니다. 실행 기록에서 원인을 확인해 주세요.", icon=":material/error:")
        for platform, detail in result["results"].items():
            label = PLATFORM_LABELS.get(platform, platform)
            if detail.startswith("실패"):
                st.error(f"{label}: {detail}", icon=":material/error:")
            else:
                st.success(f"{label}: {detail}", icon=":material/check_circle:")
        if result.get("run_url"):
            st.caption(f"[GitHub 실행 기록 보기]({result['run_url']})")
