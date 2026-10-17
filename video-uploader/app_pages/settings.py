import streamlit as st

from core import config, transcribe
from core.config import PLATFORM_LABELS, PRIVACY_LABELS
from core.github_bot import GitHubBot, GitHubError

settings = config.load_settings()


def save(**changes) -> None:
    settings.update(changes)
    config.save_settings(settings)


st.caption("입력한 키와 토큰은 이 컴퓨터의 data 폴더에만 저장됩니다. data 폴더는 다른 사람과 공유하지 마세요.")


# ── 카드뉴스 봇 (GitHub) ────────────────────────────────────

with st.container(border=True):
    st.subheader("카드뉴스 봇 연결 (GitHub)", icon=":material/hub:")
    st.caption(
        "영상은 GitHub의 카드뉴스 봇 저장소로 보내지고, 봇이 이미 연결된 인스타·스레드·유튜브 계정으로 게시합니다. "
        "계정 토큰은 GitHub Secrets에 있는 것을 그대로 쓰므로 여기서 따로 연결할 필요가 없습니다."
    )
    with st.form("github_form", border=False):
        repo = st.text_input("저장소", value=settings["github_repo"], placeholder="owner/repo")
        token = st.text_input(
            "GitHub 토큰", value=settings["github_token"], type="password", placeholder="github_pat_...",
            help="fine-grained 토큰: 저장소는 위 저장소 하나만, 권한은 Contents와 Actions를 'Read and write'로.",
        )
        submitted = st.form_submit_button("저장하고 연결 확인", icon=":material/check:")
    if submitted:
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
