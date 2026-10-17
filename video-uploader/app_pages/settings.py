import streamlit as st

from core import accounts, config, transcribe
from core.config import PLATFORM_LABELS, PRIVACY_LABELS
from core.github_bot import GitHubBot, GitHubError, repo_problem, token_problem

settings = config.load_settings()


def save(**changes) -> None:
    settings.update(changes)
    config.save_settings(settings)


st.caption("입력한 키와 토큰은 이 컴퓨터의 data 폴더에만 저장됩니다. data 폴더는 다른 사람과 공유하지 마세요.")


# ── 카드뉴스 봇 (GitHub) ────────────────────────────────────

with st.container(border=True):
    st.subheader("카드뉴스 봇 연결 (GitHub)", icon=":material/hub:")
    st.caption(
        "영상은 GitHub의 카드뉴스 봇 저장소로 보내지고, 봇이 아래 '영상 전용 계정'(VIDEO_* Secrets)으로 게시합니다. "
        "카드뉴스 계정으로는 올라가지 않습니다."
    )
    if settings["github_token"] and (saved_problem := token_problem(settings["github_token"])):
        st.warning(f"저장된 GitHub 토큰이 올바르지 않습니다. {saved_problem}", icon=":material/warning:")
    with st.form("github_form", border=False):
        repo = st.text_input("저장소", value=settings["github_repo"], placeholder="owner/repo")
        token = st.text_input(
            "GitHub 토큰", value=settings["github_token"], type="password", placeholder="github_pat_...",
            help="fine-grained 토큰: 저장소는 위 저장소 하나만, 권한은 Contents와 Actions를 'Read and write'로.",
        )
        submitted = st.form_submit_button("저장하고 연결 확인", icon=":material/check:")
    if submitted and (problem := token_problem(token) or repo_problem(repo)):
        st.error(f"저장하지 않았습니다. {problem}", icon=":material/block:")
    elif submitted:
        save(github_repo=repo.strip(), github_token=token.strip())
        try:
            info = GitHubBot(token, repo).check()
            st.success(f"연결됨: {repo.strip()} (기본 브랜치 {info['default_branch']}, 영상 업로드 워크플로 확인)",
                       icon=":material/check_circle:")
            if info["private"]:
                st.warning("비공개 저장소입니다. 인스타·스레드가 영상을 가져가지 못하므로 공개 저장소여야 합니다.",
                           icon=":material/warning:")
        except GitHubError as e:
            st.error(str(e))
    with st.expander("GitHub 토큰 만드는 법", icon=":material/key:"):
        st.markdown(
            "1. [새 fine-grained 토큰 만들기](https://github.com/settings/personal-access-tokens/new) 열기\n"
            "2. **Token name**: `video-uploader`, **Expiration**: 원하는 기간 (만료되면 새로 만들어 넣기)\n"
            "3. **Repository access** → *Only select repositories* → 카드뉴스 봇 저장소 하나만 선택\n"
            "4. **Permissions → Repository permissions**: **Contents** = Read and write, **Actions** = Read and write\n"
            "5. **Generate token** → 나온 토큰(`github_pat_...`)을 위 칸에 붙여 넣기"
        )


# ── 영상 전용 계정 (VIDEO_* Secrets) ─────────────────────────

secrets_url = f"https://github.com/{settings['github_repo']}/settings/secrets/actions"
variables_url = f"https://github.com/{settings['github_repo']}/settings/variables/actions"

with st.container(border=True):
    st.subheader("영상 전용 계정", icon=":material/switch_account:")
    st.caption(
        "영상은 카드뉴스와 다른 계정으로 올라갑니다. 계정 정보는 GitHub 저장소의 "
        f"[Secrets]({secrets_url})에 아래 이름으로 넣습니다. 없는 플랫폼은 카드뉴스 계정으로 대신 올리지 않고 실패로 표시됩니다."
    )
    st.table([
        {"플랫폼": "인스타 릴스", "Secrets": "VIDEO_IG_USER_ID, VIDEO_IG_ACCESS_TOKEN"},
        {"플랫폼": "스레드", "Secrets": "VIDEO_THREADS_USER_ID, VIDEO_THREADS_ACCESS_TOKEN"},
        {"플랫폼": "유튜브 쇼츠", "Secrets": "VIDEO_YOUTUBE_REFRESH_TOKEN (+ Variables: VIDEO_YOUTUBE_CHANNEL_ID)"},
    ])

    with st.expander("인스타·스레드 토큰으로 계정 ID 확인", icon=":material/badge:"):
        st.caption("Meta 개발자 앱에서 영상용 계정으로 받은 토큰을 넣으면, Secrets에 넣을 계정 ID와 아이디(@)를 보여 줍니다. "
                   "토큰은 저장하지 않습니다.")
        with st.form("meta_check", border=False):
            platform = st.segmented_control("플랫폼", ["인스타그램", "스레드"], default="인스타그램", required=True)
            meta_token = st.text_input("액세스 토큰", type="password")
            checked = st.form_submit_button("계정 확인", icon=":material/search:")
        if checked and meta_token.strip().startswith("github_pat_"):
            st.error("GitHub 토큰입니다. 이 값은 맨 위 '카드뉴스 봇 연결'의 GitHub 토큰 칸에 넣어 주세요.",
                     icon=":material/block:")
        elif checked:
            try:
                if platform == "인스타그램":
                    acc = accounts.instagram_account(meta_token)
                    id_name, token_name = "VIDEO_IG_USER_ID", "VIDEO_IG_ACCESS_TOKEN"
                else:
                    acc = accounts.threads_account(meta_token)
                    id_name, token_name = "VIDEO_THREADS_USER_ID", "VIDEO_THREADS_ACCESS_TOKEN"
                st.success(f"@{acc['username']} 계정입니다.", icon=":material/check_circle:")
                st.markdown(f"[Secrets]({secrets_url})에 **{id_name}** = 아래 ID, **{token_name}** = 방금 넣은 토큰")
                st.code(acc["id"], language=None)
            except accounts.AccountError as e:
                st.error(str(e))

    with st.expander("유튜브 채널 연결 (refresh token 받기)", icon=":material/smart_display:"):
        st.caption(
            "카드뉴스 봇에 쓰는 구글 OAuth 클라이언트(데스크톱 앱)의 ID와 보안 비밀번호를 넣고 누르면 브라우저가 열립니다. "
            "**영상을 올릴 채널**의 계정으로 로그인하고 '채널 선택'에서 그 채널을 고르세요. 값은 저장하지 않습니다."
        )
        with st.form("yt_auth", border=False):
            client_id = st.text_input("OAuth 클라이언트 ID", placeholder="....apps.googleusercontent.com")
            client_secret = st.text_input("클라이언트 보안 비밀번호", type="password")
            go = st.form_submit_button("구글 로그인 열기", icon=":material/login:")
        if go:
            with st.spinner("브라우저에서 로그인과 채널 선택을 기다리는 중… (최대 5분)"):
                try:
                    st.session_state.yt_auth_result = accounts.youtube_authorize(client_id, client_secret)
                except accounts.AccountError as e:
                    st.session_state.yt_auth_result = None
                    st.error(str(e))
        res = st.session_state.get("yt_auth_result")
        if res:
            st.success(f"채널: {res['channel_title']} — 영상을 올릴 채널이 맞는지 확인하세요.",
                       icon=":material/check_circle:")
            st.markdown(f"[Secrets]({secrets_url})에 **VIDEO_YOUTUBE_REFRESH_TOKEN** = 아래 값 (다른 곳에 공유 금지)")
            st.code(res["refresh_token"], language=None)
            st.markdown(f"[Variables]({variables_url})에 **VIDEO_YOUTUBE_CHANNEL_ID** = 아래 값 (다른 채널로 잘못 올라가는 것 방지)")
            st.code(res["channel_id"], language=None)
            if st.button("화면에서 지우기", icon=":material/visibility_off:"):
                st.session_state.yt_auth_result = None
                st.rerun()


# ── Claude (영상 내용 자동 작성) ─────────────────────────────

with st.container(border=True):
    st.subheader("Claude · 영상 내용 자동 작성", icon=":material/auto_awesome:")
    st.caption("선택 사항입니다. 키가 없으면 게시 문구를 직접 입력해서 올리면 됩니다.")
    with st.form("claude_form", border=False):
        api_key = st.text_input("Claude API 키", value=settings["anthropic_api_key"], type="password",
                                placeholder="sk-ant-...", help="console.anthropic.com → API Keys 에서 만듭니다.")
        model = st.selectbox("사용할 모델", list(config.CLAUDE_MODELS), format_func=config.CLAUDE_MODELS.get,
                             index=list(config.CLAUDE_MODELS).index(settings["claude_model"])
                             if settings["claude_model"] in config.CLAUDE_MODELS else 0)
        whisper_sizes = ["tiny", "base", "small", "medium"]
        whisper_model = st.selectbox(
            "음성 인식 모델 크기", whisper_sizes,
            index=whisper_sizes.index(settings["whisper_model"]) if settings["whisper_model"] in whisper_sizes else 2,
            help="클수록 정확하지만 느립니다. small을 권장합니다.",
            disabled=not transcribe.available(),
        )
        if st.form_submit_button("저장", icon=":material/save:"):
            save(anthropic_api_key=api_key.strip(), claude_model=model, whisper_model=whisper_model)
            st.toast("저장했습니다.", icon=":material/check:")
    if not transcribe.available():
        st.caption(":material/info: 말소리 인식 기능이 설치되어 있지 않아 화면만 보고 작성합니다. "
                   "프로그램 폴더의 install_voice.bat 으로 설치할 수 있습니다.")


# ── 업로드 기본값 ───────────────────────────────────────────

with st.container(border=True):
    st.subheader("업로드 기본값", icon=":material/tune:")
    with st.form("defaults_form", border=False):
        platforms = st.pills("기본으로 올릴 곳", list(PLATFORM_LABELS), selection_mode="multi",
                             format_func=PLATFORM_LABELS.get, default=settings["default_platforms"])
        default_tags = st.text_input("항상 붙일 해시태그", value=settings["default_hashtags"],
                                     placeholder="#내채널이름 #브이로그")
        privacy = st.segmented_control("유튜브 기본 공개 범위", list(PRIVACY_LABELS),
                                       format_func=PRIVACY_LABELS.get, default=settings["yt_privacy"],
                                       required=True)
        if st.form_submit_button("저장", icon=":material/save:"):
            save(default_platforms=platforms or [], default_hashtags=default_tags.strip(), yt_privacy=privacy)
            st.toast("저장했습니다.", icon=":material/check:")
