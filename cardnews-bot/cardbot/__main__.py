"""명령줄 진입점:  python -m cardbot <명령>"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from .config import Settings
from .llm import TopicIdea


def _pipeline(s):
    from .pipeline import Pipeline

    return Pipeline(s)


def cmd_trends(s, a):
    from . import trends

    sigs = sorted(trends.collect(s), key=lambda x: x.traffic, reverse=True)
    for sig in sigs[: a.limit]:
        print(sig.to_prompt_line())


def cmd_recommend(s, a):
    topics, _ = _pipeline(s).recommend(n=a.n)
    for i, t in enumerate(topics, 1):
        print(f"\n[{i}] {t.title}  (score {t.score})")
        print(f"    키워드: {t.keyword} | 관점: {t.angle}")
        print(f"    후킹: {t.hook}")
        print(f"    지금인 이유: {t.why_now}")
        if t.risk:
            print(f"    ⚠ 리스크: {t.risk}")


def cmd_make(s, a):
    p = _pipeline(s)
    if a.topic:
        topic = TopicIdea(
            title=a.topic, keyword=a.topic, angle=a.angle or "핵심 정리", hook="", why_now="사용자 지정",
            score=0, risk="",
        )
        signals = []
    else:
        topics, signals = p.recommend(n=max(a.pick, 3))
        topic = topics[a.pick - 1]
    folder = p.make(topic, signals)
    print(f"\n생성 완료: {folder}\n확인 후 게시하려면:  python -m cardbot publish \"{folder}\"")


def _exit_on_failures(p):
    if p.failures:
        for f in p.failures:
            print(f"게시 실패: {f}", file=sys.stderr)
        sys.exit(1)


def cmd_publish(s, a):
    p = _pipeline(s)
    meta = p.publish(Path(a.folder))
    for name, info in meta.get("posts", {}).items():
        print(f"{name}: {info.get('permalink') or info.get('media_id')}")
    _exit_on_failures(p)


def cmd_blog(s, a):
    p = _pipeline(s)
    folder = Path(a.folder)
    p.make_blog(folder)
    print(f"블로그 글 생성: {folder}/blog_naver.txt, blog_tistory.html, blog_guide.md")


def cmd_run(s, a):
    p = _pipeline(s)
    p.run_once(publish=a.publish or s.auto_publish)
    _exit_on_failures(p)


def cmd_auto(s, a):
    publish = a.publish or s.auto_publish
    log = logging.getLogger("auto")
    if not publish:
        log.warning("AUTO_PUBLISH가 꺼져 있어 초안만 만듭니다 (--publish 또는 AUTO_PUBLISH=true)")
    p = _pipeline(s)
    while True:
        try:
            n = p.refresh_insights() if publish else 0
            log.info("성과 지표 갱신: %d건", n)
            p.run_once(publish=publish)
        except Exception:
            if a.once:  # cron/Actions에서는 실패를 종료 코드로 알림
                raise
            log.exception("사이클 실패, 다음 주기에 재시도")
        if a.once:
            _exit_on_failures(p)
            return
        p.failures.clear()
        log.info("%d분 후 다음 사이클", s.interval_minutes)
        time.sleep(s.interval_minutes * 60)


def cmd_insights(s, a):
    p = _pipeline(s)
    print(f"갱신: {p.refresh_insights()}건")
    for post in p.store.top_posts(a.limit):
        print(post)


def cmd_refresh_token(s, a):
    from .publishers import refresh_token

    token = s.ig_access_token if a.platform == "instagram" else s.threads_access_token
    res = refresh_token(a.platform, token)
    print(f"새 토큰 (만료까지 {res.get('expires_in', '?')}초) — .env를 업데이트하세요:\n{res.get('access_token')}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cardbot", description="트렌드 카드뉴스 자동 생성·발행")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    x = sub.add_parser("trends", help="실시간 트렌드 신호 보기")
    x.add_argument("--limit", type=int, default=40)
    x.set_defaults(fn=cmd_trends)

    x = sub.add_parser("recommend", help="조회수 높을 주제 추천")
    x.add_argument("-n", type=int, default=5)
    x.set_defaults(fn=cmd_recommend)

    x = sub.add_parser("make", help="카드뉴스 초안 생성 (게시 안 함)")
    x.add_argument("--pick", type=int, default=1, help="추천 순위 중 몇 번째 주제로 만들지")
    x.add_argument("--topic", help="직접 주제 지정")
    x.add_argument("--angle", help="직접 지정 시 관점 (예: 체크리스트)")
    x.set_defaults(fn=cmd_make)

    x = sub.add_parser("publish", help="생성된 폴더를 업로드하고 게시")
    x.add_argument("folder")
    x.set_defaults(fn=cmd_publish)

    x = sub.add_parser("blog", help="초안 폴더로 네이버/티스토리 블로그 글 (재)생성")
    x.add_argument("folder")
    x.set_defaults(fn=cmd_blog)

    x = sub.add_parser("run", help="추천→제작(→게시) 1회 실행")
    x.add_argument("--publish", action="store_true", help="실제로 게시")
    x.set_defaults(fn=cmd_run)

    x = sub.add_parser("auto", help="주기적으로 자동 실행")
    x.add_argument("--publish", action="store_true")
    x.add_argument("--once", action="store_true", help="한 사이클만 (cron용)")
    x.set_defaults(fn=cmd_auto)

    x = sub.add_parser("insights", help="게시물 성과 지표 갱신")
    x.add_argument("--limit", type=int, default=10)
    x.set_defaults(fn=cmd_insights)

    x = sub.add_parser("refresh-token", help="장기 액세스 토큰 갱신")
    x.add_argument("platform", choices=["instagram", "threads"])
    x.set_defaults(fn=cmd_refresh_token)

    a = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    a.fn(Settings.load(), a)


if __name__ == "__main__":
    main()
