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

    def recent_topics(self, days: int = 14) -> list[str]:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        rows = self.db.execute(
            "SELECT DISTINCT topic FROM posts WHERE created_at >= ? ORDER BY created_at DESC", (since,)
        )
        return [r["topic"] for r in rows]

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

    def top_posts(self, limit: int = 10) -> list[dict]:
        out = []
        for r in self.db.execute("SELECT * FROM posts WHERE metrics != '{}'"):
            m = json.loads(r["metrics"] or "{}")
            out.append({"topic": r["topic"], "platform": r["platform"], **m})
        # 조회수 우선, 저장·공유 가중치
        out.sort(key=lambda p: p.get("views", 0) + 20 * p.get("saves", 0) + 30 * p.get("shares", 0), reverse=True)
        return out[:limit]
