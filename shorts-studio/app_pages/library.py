import datetime as dt
import os
import sys
from functools import partial

import streamlit as st

from shorts.config import OUTPUT_DIR

st.title("만든 영상", anchor=False)

with st.container(horizontal=True, vertical_alignment="center"):
    st.caption(f"저장 위치: {OUTPUT_DIR}")
    st.space("stretch")
    if sys.platform == "win32" and st.button("폴더 열기", icon=":material/folder_open:"):
        os.startfile(OUTPUT_DIR)

videos = sorted(OUTPUT_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
if not videos:
    st.info("아직 만든 영상이 없어요. '영상 만들기'에서 첫 영상을 만들어 보세요.", icon=":material/movie:")
    st.stop()

PAGE = 6
pages = (len(videos) + PAGE - 1) // PAGE
page_no = st.number_input("페이지", 1, pages, 1, key="lib_page") if pages > 1 else 1

for video in videos[(page_no - 1) * PAGE: page_no * PAGE]:
    info = video.with_suffix(".txt")
    made = dt.datetime.fromtimestamp(video.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    with st.container(border=True):
        left, right = st.columns([1, 2])
        with left:
            st.video(str(video), alt=f"{video.stem} 영상")
        with right:
            st.markdown(f"**{video.stem.split('_', 2)[-1].replace('_', ' ')}**")
            st.caption(f"{made} · {video.stat().st_size / 1e6:.1f}MB")
            with st.container(horizontal=True):
                st.download_button("영상 받기", partial(video.read_bytes), file_name=video.name, mime="video/mp4",
                                   icon=":material/download:", key=f"dl_{video.name}")
                if st.button("발행", icon=":material/send:", key=f"pub_{video.name}"):
                    st.session_state.publish_target = str(video)
                    st.switch_page("app_pages/publish.py")
            if info.exists():
                with st.expander("설명·대본 보기"):
                    st.code(info.read_text(encoding="utf-8"), language=None, wrap_lines=True)
