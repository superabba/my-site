"""게시 이력과 성과 지표 저장 (SQLite)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    topic TEXT NOT NULL,
    keyword TEXT,
    platform TEXT NOT NULL,
    media_id TEXT,
    permalink TEXT,
    folder TEXT,
    metrics TEXT DEFAULT '{}',
    metrics_at TEXT
);
CREATE TABLE IF NOT EXISTS reel_styles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    style TEXT NOT NULL,
    folder TEXT
);
"""


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def add(self, topic: str, keyword: str, platform: str, media_id: str, permalink: str, folder: str) -> None:
        self.db.execute(
            "INSERT INTO posts (created_at, topic, keyword, platform, media_id, permalink, folder)"
            " VALUES (?,?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), topic, keyword, platform, media_id, permalink, folder),
        )
        self.db.commit()

    def add_reel_style(self, style: str, folder: str) -> None:
        self.db.execute(
            "INSERT INTO reel_styles (created_at, style, folder) VALUES (?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), style, folder),
        )
        self.db.commit()

    def recent_reel_styles(self, n: int = 2) -> list[str]:
        """최근 릴스 스타일을 최신순으로 중복 없이 n개 돌려준다 (같은 스타일이 연달아 나온 것은 한 번만 센다)."""
        out: list[str] = []
        for r in self.db.execute("SELECT style FROM reel_styles ORDER BY id DESC"):
            if r["style"] not in out:
                out.append(r["style"])
            if len(out) >= n:
                break
        return out

    def recent_topics(self, days: int = 14) -> list[str]:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        rows = self.db.execute(
            "SELECT DISTINCT topic FROM posts WHERE created_at >= ? ORDER BY created_at DESC", (since,)
        )
        return [r["topic"] for r in rows]

    def posted_since(self, since: datetime) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM posts WHERE media_id IS NOT NULL AND created_at >= ? LIMIT 1",
            (since.isoformat(timespec="seconds"),),
        ).fetchone()
        return row is not None

    def posts_for_insights(self, max_age_days: int = 30) -> list[sqlite3.Row]:
        since = (datetime.now() - timedelta(days=max_age_days)).isoformat()
        return list(
            self.db.execute("SELECT * FROM posts WHERE media_id IS NOT NULL AND created_at >= ?", (since,))
        )

    def set_metrics(self, post_id: int, metrics: dict) -> None:
        self.db.execute(
            "UPDATE posts SET metrics=?, metrics_at=? WHERE id=?",
            (json.dumps(metrics), datetime.now().isoformat(timespec="seconds"), post_id),
        )
        self.db.commit()

    def purge_stale_metrics(self, platform: str, days: int = 30) -> int:
        """오래된 지표를 지운다 (YouTube API 정책: API 데이터는 30일 넘게 보관하지 않음)."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        cur = self.db.execute(
            "UPDATE posts SET metrics='{}', metrics_at=NULL WHERE platform=? AND metrics_at IS NOT NULL AND metrics_at < ?",
            (platform, cutoff),
        )
        self.db.commit()
        return cur.rowcount

    def top_posts(self, limit: int = 10) -> list[dict]:
        out = []
        for r in self.db.execute("SELECT * FROM posts WHERE metrics != '{}'"):
            m = json.loads(r["metrics"] or "{}")
            out.append({"topic": r["topic"], "platform": r["platform"], **m})
        # 조회수 우선, 저장·공유 가중치
        out.sort(key=lambda p: p.get("views", 0) + 20 * p.get("saves", 0) + 30 * p.get("shares", 0), reverse=True)
        return out[:limit]
