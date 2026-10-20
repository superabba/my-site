import streamlit as st

from shorts import aiimage, config

st.set_page_config(page_title="쇼츠 스튜디오", page_icon=":material/movie:", layout="centered")

if "settings" not in st.session_state:
    st.session_state.settings = config.load_settings()
settings = st.session_state.settings

with st.sidebar:
    st.header("설정", anchor=False)
    with st.form("settings_form", border=False):
        anthropic_key = st.text_input(
            "Anthropic API 키",
            value=settings["anthropic_api_key"],
            type="password",
            help="주제 추천과 대본 쓰기에 필요해요. platform.claude.com 에서 발급받을 수 있어요.",
        )
        model_labels = list(config.MODELS)
        current = next((k for k, v in config.MODELS.items() if v == settings["model"]), model_labels[0])
        model_label = st.selectbox("글쓰기 AI 모델", model_labels, index=model_labels.index(current))
        gemini_key = st.text_input(
            "Gemini API 키 (AI 그림용)",
            value=settings["gemini_api_key"],
            type="password",
            help="장면 배경을 나노바나나로 그릴 때 써요. aistudio.google.com 에서 발급하고, "
                 "이미지 생성은 무료 사용량이 없어 결제(Billing)를 연결해야 해요.",
        )
        image_model = st.selectbox(
            "그림 모델", list(aiimage.MODELS), format_func=aiimage.MODELS.get,
            index=list(aiimage.MODELS).index(settings["image_model"]) if settings["image_model"] in aiimage.MODELS else 0,
        )
        pixabay_key = st.text_input(
            "Pixabay API 키 (선택)",
            value=settings["pixabay_api_key"],
            type="password",
            help="무료 스톡 영상·사진 자동 검색용이에요. pixabay.com 에 가입한 뒤 "
                 "pixabay.com/api/docs 페이지에서 키를 볼 수 있어요.",
        )
        pexels_key = st.text_input(
            "Pexels API 키 (선택)",
            value=settings["pexels_api_key"],
            type="password",
            help="Pexels는 지금 새 키 발급을 멈춘 상태예요. 이미 키가 있다면 넣으세요.",
        )
        if st.form_submit_button("저장", icon=":material/save:", width="stretch"):
            settings.update(
                anthropic_api_key=anthropic_key.strip(),
                pixabay_api_key=pixabay_key.strip(),
                gemini_api_key=gemini_key.strip(),
                image_model=image_model,
                pexels_api_key=pexels_key.strip(),
                model=config.MODELS[model_label],
            )
            config.save_settings(settings)
            st.toast("설정을 저장했어요.", icon=":material/check:")

    st.caption("키는 이 컴퓨터의 settings.json 파일에만 저장돼요.")
    if config.anthropic_key(settings):
        st.badge("Claude 연결 준비됨", icon=":material/check_circle:", color="green")
    else:
        st.badge("Claude 키 없음: 직접 쓴 글만 가능", icon=":material/info:", color="orange")
    if config.gemini_key(settings):
        st.badge("AI 그림 준비됨 (Gemini)", icon=":material/check_circle:", color="green")
    stock = config.stock_keys(settings)
    if stock:
        names = " · ".join(n.capitalize() for n in stock)
        st.badge(f"스톡 검색 준비됨 ({names})", icon=":material/check_circle:", color="green")

page = st.navigation(
    [
        st.Page("app_pages/studio.py", title="영상 만들기", icon=":material/movie_edit:", default=True),
        st.Page("app_pages/library.py", title="만든 영상", icon=":material/video_library:"),
        st.Page("app_pages/publish.py", title="발행", icon=":material/send:"),
    ]
)
page.run()
