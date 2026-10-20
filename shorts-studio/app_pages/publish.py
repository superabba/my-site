import time
from pathlib import Path

import streamlit as st

from shorts import config
from shorts import publish as pub
from shorts.publish import (ACCOUNT_LABEL, PLATFORM_LABELS, PLATFORMS, PRIVACY_LABELS, GitHubBot,
                            PublishError)

ss = st.session_state
settings = ss.settings
ss.setdefault("pub_loaded_for", None)
ss.setdefault("pub_result", None)


def save(**fields) -> None:
    settings.update(fields)
    config.save_settings(settings)


def load_texts(video: Path) -> None:
    for key, value in pub.default_texts(video).items():
        ss[f"pub_{key}"] = value
    ss.pub_loaded_for = str(video)
    ss.pub_result = None


def copy_from_video_uploader() -> None:
    found = pub.video_uploader_settings()
    if found:
        save(**found)
        ss.gh_repo_in = found["github_repo"]
        ss.gh_token_in = found["github_token"]
        st.toast("video-uploader의 GitHub 설정을 가져왔어요.", icon=":material/check:")


def request_publish() -> None:
    # 한 번 누를 때마다 계정 확인을 다시 받도록 체크를 풀어 둔다
    ss.pub_do = True
    ss.pub_confirm = False


def run_publish(video: Path, targets: list[str], post: dict) -> None:
    """영상을 봇 저장소로 보내고, 워크플로가 게시를 마칠 때까지 지켜본다."""
    with st.status("봇으로 보내는 중…", expanded=True) as status:
        step = st.empty()
        bar = st.progress(0.0)
        video_id = None
        try:
            bot = GitHubBot(settings["github_token"], settings["github_repo"])
            repo = bot.check()
            step.write("올릴 영상 준비 중…")
            uploads, names = pub.prepare_files(
                video, targets, lambda label, p: bar.progress(min(p, 1.0), text=f"{label} {p * 100:.0f}%"))
            post["files"] = names
            video_id = pub.new_video_id()
            step.write("GitHub에 영상 올리는 중…")
            bot.upload(video_id, uploads, post, lambda p: bar.progress(p, text=f"GitHub에 올리는 중 {p * 100:.0f}%"))
            try:
                bot.dispatch(video_id, repo["default_branch"])
            except PublishError:
                bot.delete_branch(video_id)
                raise
            pub.clear_work_files()

            step.write("봇이 실행되기를 기다리는 중…")
            bar.progress(0.0, text="실행 대기")
            run = bot.find_run(video_id)
            run_url = run["html_url"] if run else f"https://github.com/{bot.repo}/actions/workflows/{pub.WORKFLOW_FILE}"
            pub.add_history(video_id, video.name, post["title"], targets, run_url)
            if not run:
                raise PublishError("봇 실행을 찾지 못했습니다. 잠시 뒤 아래 '발행 기록'에서 결과를 확인해 주세요.")

            step.write(f"봇이 게시 중이에요. 이 창을 닫아도 게시는 계속돼요. [실행 보기]({run_url})")
            started = time.time()
            while run.get("status") != "completed" and time.time() - started < 30 * 60:
                elapsed = int(time.time() - started)
                bar.progress(min(elapsed / 300, 0.95), text=f"게시 중… {elapsed // 60}분 {elapsed % 60}초 (보통 2~5분)")
                time.sleep(8)
                run = bot.get_run(run["id"])

            record = bot.read_record(video_id)
            if record:
                state, results = pub.status_from_record(record)
            elif run.get("status") != "completed":
                state, results = "진행 중", {}
            else:
                state, results = "실패", {}
            pub.update_history(video_id, status=state, results=results)
            ss.pub_result = {"status": state, "results": results, "run_url": run_url}
            status.update(label=f"발행 {state}", state="complete" if state == "완료" else "error")
        except (PublishError, OSError) as e:
            status.update(label="보내기 실패", state="error")
            ss.pub_result = {"status": "실패", "results": {}, "error": str(e), "run_url": ""}
            if video_id:
                pub.update_history(video_id, status="실패", results={"오류": str(e)})


def refresh_history() -> None:
    try:
        bot = GitHubBot(settings["github_token"], settings["github_repo"])
        for entry in pub.load_history()[:30]:
            if entry.get("status") in ("진행 중", "실패", "일부 실패"):
                record = bot.read_record(entry["video_id"])
                if record:
                    state, results = pub.status_from_record(record)
                    pub.update_history(entry["video_id"], status=state, results=results)
        st.toast("발행 결과를 새로 불러왔어요.", icon=":material/refresh:")
    except PublishError as e:
        st.toast(str(e), icon=":material/error:")


# ---------------------------------------------------------------- 화면

st.title("발행", anchor=False)
st.caption(f"만든 영상을 {ACCOUNT_LABEL} 계정의 인스타 릴스·스레드·유튜브 쇼츠에 올려요. "
           "카드뉴스 자동 발행과는 따로 동작해서, 카드뉴스는 원래 일정대로 계속 올라가요.")

token_issue = pub.token_problem(settings["github_token"]) if settings["github_token"] else None
bot_ready = bool(settings["github_token"] and settings["github_repo"]) and not token_issue

with st.expander("발행 연결 설정 (GitHub)", expanded=not bot_ready, icon=":material/link:"):
    st.caption("실제 게시는 카드뉴스 봇 저장소의 '쇼츠 발행' 워크플로가 해요. "
               "video-uploader와 같은 저장소·토큰을 쓰면 돼요 (Contents·Actions 'Read and write').")
    if pub.video_uploader_settings():
        st.button("video-uploader 설정 가져오기", icon=":material/download:", on_click=copy_from_video_uploader)
    ss.setdefault("gh_repo_in", settings["github_repo"])
    ss.setdefault("gh_token_in", settings["github_token"])
    with st.form("gh_form", border=False):
        repo_in = st.text_input("봇 저장소 (owner/repo)", key="gh_repo_in")
        token_in = st.text_input("GitHub 토큰 (github_pat_…)", key="gh_token_in", type="password")
        if st.form_submit_button("저장하고 연결 확인", icon=":material/link:"):
            problem = pub.repo_problem(repo_in) or pub.token_problem(token_in)
            if problem:
                st.error(problem)
            else:
                save(github_repo=repo_in.strip(), github_token=token_in.strip())
                try:
                    with st.spinner("연결 확인 중…"):
                        GitHubBot(token_in, repo_in).check()
                    st.success("연결됐어요. '쇼츠 발행' 워크플로도 확인했어요.", icon=":material/check_circle:")
                except PublishError as e:
                    st.error(str(e))

videos = sorted(config.OUTPUT_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
if not videos:
    st.info("아직 만든 영상이 없어요. '영상 만들기'에서 먼저 영상을 만들어 주세요.", icon=":material/movie:")
    st.stop()

# ---------------------------------------------------------------- 1. 영상

with st.container(border=True):
    st.subheader("1. 올릴 영상", anchor=False)
    names = [v.name for v in videos]
    target = ss.pop("publish_target", None)
    if target and Path(target).name in names:
        ss.pub_video = Path(target).name
    ss.setdefault("pub_video", names[0])
    if ss.pub_video not in names:
        ss.pub_video = names[0]
    chosen = st.selectbox("영상", names, key="pub_video",
                          format_func=lambda n: n.split("_", 2)[-1].removesuffix(".mp4").replace("_", " ")
                          + f"  ·  {n[:4]}-{n[4:6]}-{n[6:8]} {n[9:11]}:{n[11:13]}")
    video = config.OUTPUT_DIR / chosen
    if ss.pub_loaded_for != str(video):
        load_texts(video)
    left, right = st.columns([1, 2])
    with left:
        st.video(str(video), alt=f"{chosen} 미리보기")
    with right:
        dur = pub.probe_duration(video)
        st.caption(f"{dur:.1f}초 · {video.stat().st_size / 1e6:.1f}MB")
        if dur > 180:
            st.warning("3분이 넘으면 유튜브에서 쇼츠가 아닌 일반 동영상으로 올라가요.", icon=":material/warning:")
        if dur < 3:
            st.warning("인스타 릴스는 3초 이상이어야 해요.", icon=":material/warning:")
        st.button("대본에서 문구 다시 채우기", icon=":material/restart_alt:", type="tertiary",
                  on_click=load_texts, args=(video,))

# ---------------------------------------------------------------- 2. 게시 문구

with st.container(border=True):
    st.subheader("2. 게시 문구", anchor=False)
    st.text_input("유튜브 제목", key="pub_yt_title", max_chars=100)
    st.text_area("유튜브 설명", key="pub_yt_desc", height=110)
    st.text_area("인스타그램 캡션", key="pub_ig_caption", height=110, max_chars=2200)
    st.text_area("스레드 글", key="pub_threads_text", height=80, max_chars=500)
    st.text_input("해시태그 (유튜브·인스타 공통)", key="pub_hashtags", placeholder="#경제 #재테크  (띄어쓰기로 구분)")

# ---------------------------------------------------------------- 3. 발행

with st.container(border=True):
    st.subheader("3. 발행", anchor=False)
    with st.container(horizontal=True):
        st.badge(f"게시 계정 {ACCOUNT_LABEL}", icon=":material/account_circle:", color="blue")
        if bot_ready:
            st.badge(f"봇 저장소 {settings['github_repo']}", icon=":material/check:", color="green")
        else:
            st.badge("봇 연결 안 됨", icon=":material/link_off:", color="gray")
    st.caption("카드뉴스와 같은 계정이라, 카드뉴스가 막히지 않도록 봇이 게시 직전에 계정 아이디와 "
               "하루 게시 한도를 확인해요 (쇼츠는 플랫폼마다 하루 최대 5개, 카드뉴스 몫은 남겨둠).")

    targets = st.pills("올릴 곳", PLATFORMS, selection_mode="multi", format_func=PLATFORM_LABELS.get,
                       default=[p for p in settings["default_platforms"] if p in PLATFORMS], key="pub_targets") or []
    privacy = settings["yt_privacy"]
    synthetic = False
    if "youtube" in targets:
        with st.container(horizontal=True, vertical_alignment="bottom"):
            privacy = st.segmented_control("유튜브 공개 범위", list(PRIVACY_LABELS), format_func=PRIVACY_LABELS.get,
                                           default=settings["yt_privacy"], required=True, key="pub_privacy")
            synthetic = st.toggle("AI 합성 콘텐츠로 표시", key="pub_synthetic",
                                  help="AI 그림(나노바나나)으로 만든 영상이면 자동으로 켜져요. "
                                       "AI 목소리 내레이션과 자동 배경·스톡만 쓴 영상이면 보통 끄면 돼요.")

    tags = pub.parse_hashtags(ss.pub_hashtags)
    tag_line = " ".join(f"#{t}" for t in tags)
    ig_full = f"{ss.pub_ig_caption.strip()}\n\n{' '.join(f'#{t}' for t in tags[:30])}".strip()
    threads_final = (ss.pub_threads_text.strip() or ss.pub_ig_caption.strip())[:500]

    problems = []
    if not targets:
        problems.append("올릴 곳을 하나 이상 고르세요.")
    if token_issue:
        problems.append(f"GitHub 토큰이 올바르지 않아요. {token_issue}")
    elif not bot_ready:
        problems.append("위의 '발행 연결 설정'에서 GitHub 연결을 먼저 해 주세요.")
    if "youtube" in targets and not ss.pub_yt_title.strip():
        problems.append("유튜브 제목을 입력해 주세요.")
    if "reels" in targets and len(ig_full) > 2200:
        problems.append(f"인스타그램 캡션이 해시태그 포함 {len(ig_full)}자예요. 2,200자 이하로 줄여 주세요.")
    if "reels" in targets and not ig_full:
        problems.append("인스타그램 캡션을 입력해 주세요.")
    if "threads" in targets and not threads_final:
        problems.append("스레드 글(또는 인스타 캡션)을 입력해 주세요.")

    if targets:
        with st.expander("실제로 올라갈 문구 미리보기", icon=":material/visibility:"):
            if "youtube" in targets:
                st.markdown("**유튜브 쇼츠**")
                st.text(f"[제목] {ss.pub_yt_title.strip()}\n\n{ss.pub_yt_desc.strip()}\n\n{tag_line}\n\n"
                        "(#Shorts는 자동으로 붙어요)")
            if "reels" in targets:
                st.markdown("**인스타 릴스**")
                st.text(ig_full)
            if "threads" in targets:
                st.markdown("**스레드**")
                st.text(threads_final)

    already = [h for h in pub.load_history() if h.get("video") == video.name and h.get("status") != "실패"]
    allow = True
    if already:
        st.warning(f"이 영상은 {already[0]['time']}에 이미 보냈어요.", icon=":material/content_copy:")
        allow = st.checkbox("같은 영상을 한 번 더 올리기", key="pub_again")
    confirmed = st.checkbox(f"{ACCOUNT_LABEL} 계정에 바로 게시되는 것을 확인했어요", key="pub_confirm")

    for p in problems:
        st.caption(f":material/error: {p}")

    st.button("발행하기", type="primary", icon=":material/send:", width="stretch",
              disabled=bool(problems) or not allow or not confirmed, on_click=request_publish)
    if ss.pop("pub_do", False) and not problems and allow:
        title = ss.pub_yt_title.strip() or ss.pub_ig_caption.strip().split("\n")[0][:100]
        run_publish(video, targets, {
            "title": title,
            "youtube_description": ss.pub_yt_desc.strip(),
            "instagram_caption": ss.pub_ig_caption.strip(),
            "threads_text": threads_final,
            "hashtags": tags,
            "platforms": targets,
            "youtube_privacy": privacy,
            "youtube_synthetic": bool(synthetic),
            "account": pub.ACCOUNT,
            "source_name": video.name,
        })

    result = ss.pub_result
    if result:
        if result.get("error"):
            st.error(f"보내기 실패: {result['error']}", icon=":material/error:")
        elif result["status"] == "진행 중":
            st.info("봇이 아직 게시 중이에요. 아래 '발행 기록'에서 결과를 확인해 주세요.", icon=":material/pending:")
        elif not result["results"]:
            st.error("봇 실행이 실패했어요. 실행 기록에서 원인을 확인해 주세요.", icon=":material/error:")
        for platform, detail in result["results"].items():
            label = PLATFORM_LABELS.get(platform, platform)
            if detail.startswith("실패"):
                st.error(f"{label}: {detail}", icon=":material/error:")
            else:
                st.success(f"{label}: {detail}", icon=":material/check_circle:")
        if result.get("run_url"):
            st.caption(f"[GitHub 실행 기록 보기]({result['run_url']}) · 실패한 곳만 다시 올리려면 실행 기록에서 "
                       "'Re-run jobs'를 누르세요 (이미 올라간 곳은 건너뛰어요).")

# ---------------------------------------------------------------- 매일 자동 발행 기록


@st.cache_data(ttl="5m", max_entries=4, show_spinner="매일 자동 기록을 불러오는 중…")
def load_daily(token: str, repo: str) -> list[dict]:
    return GitHubBot(token, repo).daily_records()


if bot_ready:
    daily_box = st.expander("매일 자동 발행 기록 (오전 7시 10분)", icon=":material/schedule:", key="daily_box",
                            on_change="rerun")
    if daily_box.open:
        with daily_box:
            st.caption("GitHub의 '매일 자동 쇼츠'가 올린 결과예요. 수동 발행과 따로 돌아가요. "
                       f"[실행 기록 보기](https://github.com/{settings['github_repo']}/actions/workflows/shorts-daily.yml)")
            try:
                records = load_daily(settings["github_token"], settings["github_repo"])
            except PublishError as e:
                records = []
                st.error(str(e))
            if not records:
                st.caption("아직 매일 자동 발행 기록이 없어요.")
            for rec in records:
                state, results = pub.status_from_record(rec)
                with st.container(border=True):
                    with st.container(horizontal=True, vertical_alignment="center"):
                        st.markdown(f"**{rec.get('title') or rec.get('topic', '')}**")
                        st.badge(state, color={"완료": "green", "일부 실패": "orange"}.get(state, "red"))
                        st.space("stretch")
                        st.caption(f"{rec.get('theme', '')} · {str(rec.get('created_at', ''))[:10]}")
                    for platform, detail in results.items():
                        label = PLATFORM_LABELS.get(platform, platform)
                        st.markdown(f"- {label}: [{detail}]({detail})" if detail.startswith("http")
                                    else f"- {label}: {detail}")

# ---------------------------------------------------------------- 발행 기록

history = pub.load_history()
if history:
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.subheader("발행 기록", anchor=False)
            st.space("stretch")
            st.button("결과 새로고침", icon=":material/refresh:", on_click=refresh_history, disabled=not bot_ready)
        for entry in history[:20]:
            color = {"완료": "green", "진행 중": "blue", "일부 실패": "orange"}.get(entry["status"], "red")
            with st.container(border=True):
                with st.container(horizontal=True, vertical_alignment="center"):
                    st.markdown(f"**{entry['title']}**")
                    st.badge(entry["status"], color=color)
                    st.space("stretch")
                    st.caption(entry["time"])
                for platform, detail in entry.get("results", {}).items():
                    label = PLATFORM_LABELS.get(platform, platform)
                    if detail.startswith("http"):
                        st.markdown(f"- {label}: [{detail}]({detail})")
                    else:
                        st.markdown(f"- {label}: {detail}")
                if entry.get("run_url"):
                    st.caption(f"[실행 기록]({entry['run_url']})")
