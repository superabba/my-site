import streamlit as st

from core import config
from core.config import PLATFORM_LABELS
from core.github_bot import GitHubBot, GitHubError

settings = config.load_settings()
history = config.load_history()

pending = [h for h in history if h.get("status") == "진행 중"]
if pending and st.button(f"진행 중인 {len(pending)}건 결과 확인", icon=":material/refresh:"):
    try:
        bot = GitHubBot(settings["github_token"], settings["github_repo"])
        with st.spinner("봇의 결과 기록을 확인하는 중…"):
            for entry in pending:
                record = bot.read_record(entry["video_id"])
                if record:
                    state, results = config.status_from_record(record)
                    config.update_history(entry["video_id"], status=state, results=results)
        st.rerun()
    except GitHubError as e:
        st.error(str(e))

if not history:
    st.caption("아직 업로드 기록이 없습니다.")
else:
    rows = []
    for h in history:
        row = {"time": h["time"], "title": h.get("title", ""), "status": h.get("status", ""),
               "run_url": h.get("run_url") or None}
        for p in PLATFORM_LABELS:
            detail = (h.get("results") or {}).get(p)
            row[p] = detail if detail else ("—" if p not in h.get("platforms", []) else "")
        rows.append(row)
    st.dataframe(
        rows,
        hide_index=True,
        column_order=["time", "status", "title", "reels", "threads", "youtube", "naver_blog", "run_url"],
        column_config={
            "time": st.column_config.TextColumn("시각"),
            "status": st.column_config.TextColumn("상태"),
            "title": st.column_config.TextColumn("제목"),
            "reels": st.column_config.TextColumn(PLATFORM_LABELS["reels"]),
            "threads": st.column_config.TextColumn(PLATFORM_LABELS["threads"]),
            "youtube": st.column_config.TextColumn(PLATFORM_LABELS["youtube"]),
            "naver_blog": st.column_config.TextColumn(PLATFORM_LABELS["naver_blog"]),
            "run_url": st.column_config.LinkColumn("실행 기록", display_text="열기"),
        },
    )
    st.caption("링크 칸을 두 번 클릭하면 주소를 복사할 수 있습니다. 실패한 플랫폼은 실행 기록에서 'Re-run jobs'를 누르면 실패한 곳만 다시 올립니다.")
