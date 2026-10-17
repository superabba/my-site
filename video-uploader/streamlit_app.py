import streamlit as st

st.set_page_config(page_title="숏폼 자동 업로더", page_icon=":material/movie:", layout="centered")

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
