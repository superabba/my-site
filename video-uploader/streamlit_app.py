import streamlit as st

from core import config

st.set_page_config(page_title="숏폼 자동 업로더", page_icon=":material/movie:", layout="centered")


@st.cache_resource(show_spinner=False)
def clean_leftovers_once() -> bool:
    # 프로그램을 켤 때 한 번, 지난번에 남은 작업용 사본을 지운다 (창을 닫아 정리가 안 된 경우 대비).
    config.clear_working_files()
    return True


clean_leftovers_once()

page = st.navigation(
    [
        st.Page("app_pages/upload.py", title="업로드", icon=":material/upload:", default=True),
        st.Page("app_pages/settings.py", title="계정 설정", icon=":material/settings:"),
        st.Page("app_pages/history.py", title="업로드 기록", icon=":material/history:"),
    ],
    position="top",
)
st.title(page.title, icon=page.icon)
page.run()
