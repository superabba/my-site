#!/usr/bin/env python3
"""쇼츠 원고 루틴용: 오늘의 테마와 최근에 다룬 주제를 보여 준다 (표준 라이브러리만 씀, 설치 불필요).

  python3 shorts-studio/routine/info.py

- 오늘의 테마: shorts/daily.py 의 THEMES 를 날짜로 돌린다 (SHORTS_THEMES 변수는 루틴에서 볼 수 없으니 기본 목록)
- 최근 쇼츠 주제: shorts-records 브랜치 records/*.json (최근 30일)
- 최근 카드뉴스 주제: cardbot-data 브랜치 data/cardbot.db (최근 7일)
- 대기 중인 원고: shorts-records 브랜치 inbox/
"""
import ast
import json
import re
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]


def git(*args: str, binary: bool = False):
    r = subprocess.run(["git", *args], capture_output=True)
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def themes():
    tree = ast.parse((ROOT / "shorts" / "daily.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "THEMES":
            return ast.literal_eval(node.value)
    return []


def main():
    now = datetime.now(KST)
    items = themes()
    name, desc, queries = items[now.date().toordinal() % len(items)]
    print(f"오늘(KST {now:%Y-%m-%d})의 테마: {name}\n설명: {desc}\n검색어: {', '.join(queries)}\n")

    subprocess.run(["git", "fetch", "-q", "--depth=1", "origin", "shorts-records", "cardbot-data"], capture_output=True)
    since = now - timedelta(days=30)
    print("최근 30일 쇼츠 주제 (겹치지 않게):")
    names = git("ls-tree", "--name-only", "origin/shorts-records", "records/").split()
    shown = 0
    for n in names:
        try:
            rec = json.loads(git("show", f"origin/shorts-records:{n}"))
        except ValueError:
            continue
        try:
            when = datetime.fromisoformat(rec.get("created_at", ""))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if when >= since and (rec.get("topic") or rec.get("title")):
            print("-", rec.get("topic") or rec.get("title"))
            shown += 1
    if not shown:
        print("- (없음)")

    print("\n최근 7일 카드뉴스 주제 (겹치지 않게):")
    db = git("show", "origin/cardbot-data:data/cardbot.db", binary=True)
    topics = []
    if db:
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            f.write(db)
            f.flush()
            try:
                con = sqlite3.connect(f.name)
                cut = (datetime.now() - timedelta(days=7)).isoformat()
                topics = [r[0] for r in con.execute("SELECT DISTINCT topic FROM posts WHERE created_at >= ?", (cut,)) if r[0]]
            except sqlite3.Error:
                pass
    for t in topics or ["(없음)"]:
        print("-", t)

    pend = [re.sub(r".*/", "", p) for p in git("ls-tree", "--name-only", "origin/shorts-records", "inbox/").split()]
    print("\n대기 중인 원고(inbox):", ", ".join(pend) if pend else "(없음)")


if __name__ == "__main__":
    main()
