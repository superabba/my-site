from functools import partial
from pathlib import Path

import streamlit as st

from shorts import config, music
from shorts.ffmpeg import FFmpegError, write_wav
from shorts.render import RenderOptions, render
from shorts.text import SUB_STYLES
from shorts.tts import VOICES, TTSError, synthesize
from shorts.visuals import IMAGE_EXTS, VIDEO_EXTS, media_kind, save_upload
from shorts.writer import (CATEGORIES, CHARS_PER_SEC, MOODS, TONES, WriterError, new_scene,
                           polish_text, split_text, suggest_topics, write_script)

ss = st.session_state
settings = ss.settings
api_key = config.anthropic_key(settings)
model = settings["model"]

ss.setdefault("script", None)
ss.setdefault("topics", [])
ss.setdefault("pending", None)
ss.setdefault("result", None)
ss.setdefault("music_seed", 0)
ss.setdefault("uploads", {})
ss.setdefault("voice_preview", None)
ss.setdefault("music_preview", None)

MEDIA_TYPES = sorted(e.lstrip(".") for e in IMAGE_EXTS | VIDEO_EXTS)
QUALITY = {"1080p (업로드용)": (1080, 1920), "720p": (720, 1280), "빠른 미리보기": (540, 960)}
BG_MODES = {
    "ai_image": "AI 그림 (나노바나나)",
    "gradient": "자동 배경",
    "my_media": "내 사진·영상",
    "stock_video": "스톡 영상",
    "stock_photo": "스톡 사진",
}
MUSIC_MODES = {"auto": "자동 생성", "file": "내 음악 파일", "none": "없음"}


# ---------------------------------------------------------------- 상태 도우미

def load_script(script: dict) -> None:
    ss.script = script
    ss.title_in = script["title"]
    ss.mood_in = script["mood"]
    ss.caption_in = script["caption"]
    ss.tags_in = " ".join(f"#{h}" for h in script["hashtags"])
    ss.result = None


def sync_script() -> None:
    """화면에서 고친 내용을 대본에 반영합니다."""
    sc = ss.script
    sc["title"] = ss.get("title_in", sc["title"]).strip()
    sc["mood"] = ss.get("mood_in", sc["mood"])
    sc["caption"] = ss.get("caption_in", sc["caption"]).strip()
    sc["hashtags"] = [t.lstrip("#") for t in ss.get("tags_in", "").split() if t.lstrip("#")]
    for scene in sc["scenes"]:
        sid = scene["id"]
        scene["narration"] = ss.get(f"nar_{sid}", scene["narration"]).strip()
        scene["headline"] = ss.get(f"head_{sid}", scene["headline"]).strip()
        scene["keywords"] = ss.get(f"kw_{sid}", scene["keywords"]).strip()


def move_scene(idx: int, step: int) -> None:
    scenes = ss.script["scenes"]
    j = idx + step
    if 0 <= j < len(scenes):
        scenes[idx], scenes[j] = scenes[j], scenes[idx]


def delete_scene(idx: int) -> None:
    ss.script["scenes"].pop(idx)


def add_scene() -> None:
    ss.script["scenes"].append(new_scene())


def request(kind: str, payload: str) -> None:
    ss.pending = (kind, payload)


def uploaded_path(file) -> str:
    """올린 파일을 cache/uploads에 저장하고 경로를 돌려줍니다 (같은 파일은 한 번만 저장)."""
    if file.file_id not in ss.uploads:
        ss.uploads[file.file_id] = str(save_upload(file.getvalue(), file.name, config.UPLOAD_DIR))
    return ss.uploads[file.file_id]


# ---------------------------------------------------------------- 1. 주제와 글

st.title("쇼츠 스튜디오", anchor=False)
st.caption("글을 넣으면 목소리·배경음악·자막이 들어간 세로 영상(릴스·쇼츠)을 만들어 줘요.")

with st.container(border=True):
    st.subheader("1. 주제와 글", anchor=False)
    start = st.segmented_control(
        "어떻게 시작할까요?",
        ["AI 주제 추천", "주제 직접 입력", "내가 쓴 글로"],
        default="AI 주제 추천",
        required=True,
        key="start_mode", persist_state="session",
    )
    with st.container(horizontal=True, vertical_alignment="bottom"):
        category = st.selectbox("종류", list(CATEGORIES), key="category", persist_state="session")
        tone = st.selectbox("말투", list(TONES), key="tone", persist_state="session")
        length = st.selectbox("영상 길이", [15, 30, 45, 60], index=1, format_func=lambda s: f"약 {s}초", key="length", persist_state="session")
    needs_key = not api_key
    if needs_key and start != "내가 쓴 글로":
        st.info("AI 기능을 쓰려면 왼쪽 설정에 Anthropic API 키를 넣어 주세요. "
                "키 없이도 '내가 쓴 글로'는 바로 쓸 수 있어요.", icon=":material/key:")

    if start == "AI 주제 추천":
        with st.container(horizontal=True, vertical_alignment="bottom"):
            hint = st.text_input("관심사나 키워드 (선택)", key="hint", persist_state="session", placeholder="예: 자취 요리, 가을 산책, 직장인 재테크")
            if st.button("주제 추천받기", icon=":material/lightbulb:", type="primary", disabled=needs_key):
                with st.spinner("주제를 고르는 중이에요..."):
                    try:
                        ss.topics = suggest_topics(api_key, model, category, hint)
                    except WriterError as e:
                        st.error(str(e))
        for i, tp in enumerate(ss.topics):
            with st.container(border=True):
                st.markdown(f"**{tp['title']}**")
                st.caption(f"첫 문장 예시: {tp['hook']}  \n{tp['why']}")
                st.button("이 주제로 대본 쓰기", key=f"pick_{i}", icon=":material/edit_note:",
                          on_click=request, args=("topic", tp["title"]), disabled=needs_key)

    elif start == "주제 직접 입력":
        topic = st.text_input("주제", key="topic_in", persist_state="session", placeholder="예: 비 오는 날 혼자 카페에서 보낸 하루")
        if st.button("대본 쓰기", icon=":material/edit_note:", type="primary", disabled=needs_key or not topic.strip()):
            request("topic", topic.strip())

    else:
        my_text = st.text_area("내 글", key="my_text", persist_state="session", height=200,
                               placeholder="일기나 알리고 싶은 내용을 자유롭게 써 주세요.")
        my_title = st.text_input("제목 (선택)", key="my_title", persist_state="session")
        polish = st.toggle("AI로 쇼츠 대본처럼 다듬기", key="polish", persist_state="session", value=bool(api_key), disabled=needs_key,
                           help="끄면 쓴 글을 문장 단위로 그대로 나눠서 써요.")
        if st.button("장면 만들기", icon=":material/view_agenda:", type="primary", disabled=not my_text.strip()):
            if polish and api_key:
                request("polish", my_text.strip())
            else:
                load_script(split_text(my_text, my_title.strip()))

    notes = st.text_input("추가 요청 (선택)", key="notes", persist_state="session",
                          placeholder="예: 마지막에 질문으로 끝내줘, 숫자 정보를 꼭 넣어줘")

if ss.pending:
    kind, payload = ss.pending
    ss.pending = None
    with st.spinner("대본을 쓰는 중이에요... (보통 20~60초)"):
        try:
            if kind == "topic":
                script = write_script(api_key, model, payload, category, tone, length, notes)
            else:
                script = polish_text(api_key, model, payload, category, tone, length, notes)
            if not script["scenes"]:
                st.error("대본이 비어 있어요. 다시 시도해 주세요.")
            else:
                load_script(script)
                if kind == "polish" and ss.get("my_title", "").strip():
                    ss.title_in = ss.my_title.strip()
        except WriterError as e:
            st.error(str(e))

if ss.script is None:
    st.stop()

# ---------------------------------------------------------------- 2. 대본 확인·수정

script = ss.script
ss.setdefault("title_in", script["title"])
ss.setdefault("mood_in", script["mood"])
ss.setdefault("caption_in", script["caption"])
ss.setdefault("tags_in", " ".join(f"#{h}" for h in script["hashtags"]))
with st.container(border=True):
    st.subheader("2. 대본 확인·수정", anchor=False)
    with st.container(horizontal=True, vertical_alignment="bottom"):
        st.text_input("영상 제목", key="title_in", persist_state="session")
        st.selectbox("분위기 (배경음악·색)", list(MOODS), format_func=MOODS.get, key="mood_in", persist_state="session")

    total_chars = 0
    for idx, scene in enumerate(script["scenes"]):
        sid = scene["id"]
        for field, prefix in (("narration", "nar"), ("headline", "head"), ("keywords", "kw")):
            ss.setdefault(f"{prefix}_{sid}", scene[field])
        with st.container(border=True):
            with st.container(horizontal=True, vertical_alignment="center"):
                st.markdown(f"**장면 {idx + 1}**")
                st.space("stretch")
                st.button(":material/arrow_upward:", key=f"up_{sid}", help="위로", type="tertiary",
                          on_click=move_scene, args=(idx, -1), disabled=idx == 0)
                st.button(":material/arrow_downward:", key=f"down_{sid}", help="아래로", type="tertiary",
                          on_click=move_scene, args=(idx, 1), disabled=idx == len(script["scenes"]) - 1)
                st.button(":material/delete:", key=f"del_{sid}", help="장면 지우기", type="tertiary",
                          on_click=delete_scene, args=(idx,))
            nar = st.text_area("내레이션·자막", key=f"nar_{sid}", persist_state="session", height=80)
            total_chars += len(nar.replace(" ", ""))
            with st.container(horizontal=True):
                st.text_input("화면 큰 글씨 (선택)", key=f"head_{sid}", persist_state="session")
                st.text_input("배경 검색어 (스톡용)", key=f"kw_{sid}", persist_state="session", help="Pexels 스톡 검색에 쓰여요. 영어가 결과가 많아요.")
            up = st.file_uploader("이 장면 배경으로 쓸 사진·영상 (선택)", type=MEDIA_TYPES, key=f"media_{sid}")
            scene["media"] = uploaded_path(up) if up else None
            if scene["media"] and media_kind(scene["media"]) == "image":
                st.image(scene["media"], width=120, alt=f"장면 {idx + 1} 배경 사진")

    with st.container(horizontal=True, vertical_alignment="center"):
        st.button("장면 추가", icon=":material/add:", on_click=add_scene)
        est = total_chars / CHARS_PER_SEC
        st.caption(f"장면 {len(script['scenes'])}개 · 목소리 기준 예상 길이 약 {est:.0f}초")

    st.text_area("게시글 설명 (업로드할 때 복사해서 쓰세요)", key="caption_in", persist_state="session", height=80)
    st.text_input("해시태그", key="tags_in", persist_state="session", placeholder="#일기 #브이로그")
sync_script()

# ---------------------------------------------------------------- 3. 소리와 화면

with st.container(border=True):
    st.subheader("3. 소리와 화면", anchor=False)

    st.markdown("**목소리**")
    voice_on = st.toggle("내레이션 목소리 넣기", value=True, key="voice_on", persist_state="session")
    with st.container(horizontal=True, vertical_alignment="bottom"):
        voice_label = st.selectbox("목소리", list(VOICES), key="voice_label", persist_state="session", disabled=not voice_on)
        speed = st.slider("말 빠르기", -30, 50, 10, step=5, format="%d%%", key="speed", persist_state="session", disabled=not voice_on)
        if st.button("미리 듣기", icon=":material/play_arrow:", key="voice_try", disabled=not voice_on):
            first = next((ss.get(f"nar_{s['id']}", "") for s in script["scenes"] if ss.get(f"nar_{s['id']}")), "")
            try:
                with st.spinner("목소리 만드는 중..."):
                    ss.voice_preview = str(synthesize(first or "안녕하세요. 이 목소리로 읽어 드릴게요.",
                                                      VOICES[voice_label], speed)[0])
            except TTSError as e:
                st.error(str(e))
    if voice_on and ss.voice_preview:
        st.audio(ss.voice_preview, format="audio/mpeg", alt="목소리 미리 듣기")

    st.markdown("**배경음악**")
    music_mode = st.segmented_control("배경음악", list(MUSIC_MODES), format_func=MUSIC_MODES.get,
                                      default="auto", required=True, key="music_mode", persist_state="session", label_visibility="collapsed")
    music_file = None
    if music_mode == "auto":
        with st.container(horizontal=True, vertical_alignment="center"):
            st.caption(f"'{MOODS[ss.get('mood_in', script['mood'])]}' 분위기에 맞춰 저작권 걱정 없는 음악을 만들어요. (곡 번호 {ss.music_seed})")
            st.space("stretch")
            if st.button("다른 곡", icon=":material/shuffle:"):
                ss.music_seed += 1
                ss.music_preview = None
            if st.button("미리 듣기", icon=":material/play_arrow:", key="music_try"):
                with st.spinner("음악 만드는 중..."):
                    clip = music.generate_bgm(ss.get("mood_in", script["mood"]), 15.0, seed=ss.music_seed)
                    path = config.WORK_DIR / f"preview_{ss.music_seed}.wav"
                    write_wav(path, clip)
                    ss.music_preview = str(path)
        if ss.music_preview:
            st.audio(ss.music_preview, alt="배경음악 미리 듣기")
    elif music_mode == "file":
        up = st.file_uploader("음악 파일 올리기 (mp3 등)", type=[e.lstrip(".") for e in music.MUSIC_EXTS], key="music_up")
        if up:
            dest = config.MUSIC_DIR / up.name
            if not dest.exists():
                dest.write_bytes(up.getvalue())
        files = music.list_music_files()
        if files:
            names = [f.name for f in files]
            default_idx = names.index(up.name) if up and up.name in names else 0
            music_file = str(files[st.selectbox("사용할 음악", range(len(files)), index=default_idx,
                                                format_func=lambda i: names[i], key="music_pick", persist_state="session")])
        else:
            st.caption("assets/music 폴더에 넣어둔 음악이 없어요. 위에서 올려 주세요. "
                       "유튜브 오디오 보관함 같은 무료 음원을 추천해요.")
    music_vol = st.slider("음악 크기", 0, 100, 30, step=5, format="%d%%", key="music_vol", persist_state="session",
                          disabled=music_mode == "none")

    st.markdown("**배경 화면**")
    bg_mode = st.segmented_control("배경 화면", list(BG_MODES), format_func=BG_MODES.get,
                                   default="ai_image" if config.gemini_key(settings) else "gradient",
                                   required=True, key="bg_mode", persist_state="session", label_visibility="collapsed")
    my_media: list[str] = []
    if bg_mode == "ai_image":
        if not config.gemini_key(settings):
            st.warning("AI 그림에는 결제가 연결된 Gemini API 키가 필요해요. 왼쪽 설정에 넣어 주세요. "
                       "키가 없으면 스톡 영상이나 자동 배경으로 만들어요.", icon=":material/key:")
        else:
            st.caption("장면마다 내레이션에 맞는 세로 그림을 나노바나나로 그려서 천천히 움직이는 배경으로 써요. "
                       "한 장에 약 70원(2K)이 들고, 같은 장면을 다시 만들면 저장해 둔 그림을 써요. "
                       "그림을 못 만든 장면은 스톡 영상(키가 있으면)이나 자동 배경으로 채워요.")
    elif bg_mode == "my_media":
        ups = st.file_uploader("사진·영상 여러 개 올리기 (장면 순서대로 돌아가며 써요)", type=MEDIA_TYPES,
                               accept_multiple_files=True, key="my_media_up")
        my_media = [uploaded_path(f) for f in ups or []]
    elif bg_mode.startswith("stock"):
        if not config.stock_keys(settings):
            st.warning("스톡 검색에는 무료 Pixabay API 키가 필요해요. pixabay.com 에 가입한 뒤 "
                       "pixabay.com/api/docs 페이지에서 키를 복사해 왼쪽 설정에 넣어 주세요. "
                       "키가 없으면 자동 배경으로 만들어요.", icon=":material/key:")
        else:
            st.caption("장면마다 '배경 검색어'로 무료 스톡 영상·사진을 찾아 넣어요. "
                       "가로 영상은 가운데를 세로로 잘라서 써요.")
    st.caption("장면별로 따로 올린 사진·영상이 있으면 그게 먼저 쓰여요.")

    st.markdown("**글씨와 화질**")
    with st.container(horizontal=True, vertical_alignment="bottom"):
        sub_on = st.toggle("자막", value=True, key="sub_on", persist_state="session")
        head_on = st.toggle("화면 큰 글씨", value=True, key="head_on", persist_state="session")
        title_on = st.toggle("맨 위 제목", value=False, key="title_on", persist_state="session")
        prog_on = st.toggle("진행 막대", value=True, key="prog_on", persist_state="session")
    with st.container(horizontal=True, vertical_alignment="bottom"):
        sub_style = st.selectbox("자막 모양", list(SUB_STYLES), key="sub_style", persist_state="session", disabled=not sub_on)
        quality = st.selectbox("화질", list(QUALITY), key="quality", persist_state="session")

# ---------------------------------------------------------------- 4. 영상 만들기

with st.container(border=True):
    st.subheader("4. 영상 만들기", anchor=False)
    if st.button("영상 만들기", type="primary", icon=":material/movie:", width="stretch"):
        sync_script()
        w, h = QUALITY[quality]
        opts = RenderOptions(
            width=w, height=h,
            voice_on=voice_on, voice=VOICES[voice_label], speed=speed,
            music_mode=music_mode, music_file=music_file, music_seed=ss.music_seed,
            music_volume=music_vol / 100 * 0.8,
            bg_mode=bg_mode, my_media=my_media, stock_keys=config.stock_keys(settings),
            gemini_key=config.gemini_key(settings), image_model=settings["image_model"],
            image_size=settings["image_size"],
            show_subtitles=sub_on, sub_style=SUB_STYLES[sub_style],
            show_headline=head_on, show_title=title_on, show_progress=prog_on,
        )
        bar = st.progress(0.0, text="준비 중")
        try:
            res = render(script, opts, lambda p, msg: bar.progress(min(1.0, max(0.0, p)), text=msg))
            ss.result = {"video": str(res.video), "srt": str(res.srt), "info": str(res.info),
                         "duration": res.duration, "warnings": res.warnings, "credits": res.credits}
        except (TTSError, FFmpegError, RuntimeError, ValueError, FileNotFoundError, OSError) as e:
            st.error(f"영상을 만들지 못했어요: {e}")

    result = ss.result
    if result:
        for wmsg in result["warnings"]:
            st.warning(wmsg)
        st.success(f"완성! 길이 {result['duration']:.1f}초 · output 폴더에 저장했어요.", icon=":material/check_circle:")
        left, right = st.columns([1, 1])
        with left:
            st.video(result["video"], alt=f"{script['title']} 완성 영상")
        with right:
            video = Path(result["video"])
            st.download_button("영상 받기 (mp4)", partial(video.read_bytes), file_name=video.name,
                               mime="video/mp4", icon=":material/download:", type="primary", width="stretch")
            st.download_button("자막 파일 (srt)", partial(Path(result["srt"]).read_bytes),
                               file_name=Path(result["srt"]).name, icon=":material/subtitles:", width="stretch")
            if st.button("경제주막 계정에 발행하러 가기", icon=":material/send:", width="stretch"):
                ss.publish_target = result["video"]
                st.switch_page("app_pages/publish.py")
            st.markdown("**업로드용 설명**")
            tags = " ".join(f"#{t}" for t in script["hashtags"])
            st.code(f"{script['title']}\n\n{script['caption']}\n\n{tags}".strip(), language=None, wrap_lines=True)
            if result.get("credits"):
                with st.expander("배경 영상·사진 출처"):
                    st.caption("무료 스톡 서비스에서 가져왔어요. 설명란에 출처를 적어 주면 좋아요.")
                    st.code("\n".join(result["credits"]), language=None, wrap_lines=True)
