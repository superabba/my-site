"""트렌드 수집 → 주제 추천 → 조사 → 원고 → 렌더링 → 업로드 → 게시."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from . import trends
from .hosting import make_host
from .llm import CardNews, TopicIdea, Writer, instagram_caption
from .publishers import make_publishers
from .render import render_card
from .store import Store

log = logging.getLogger(__name__)


def slugify(text: str, n: int = 30) -> str:
    s = re.sub(r"[^\w가-힣]+", "-", text).strip("-")
    return s[:n] or "card"


class Pipeline:
    def __init__(self, settings, writer: Writer | None = None, store: Store | None = None):
        self.s = settings
        self.writer = writer or Writer(settings)
        self.store = store or Store(settings.data_dir / "cardbot.db")

    # ----- 추천 -----
    def recommend(self, n: int = 5) -> tuple[list[TopicIdea], list[trends.TrendSignal]]:
        signals = trends.collect(self.s)
        topics = self.writer.recommend(
            signals, self.store.top_posts(), self.store.recent_topics(), n=n
        )
        return topics, signals

    # ----- 제작 -----
    def make(self, topic: TopicIdea, signals: list[trends.TrendSignal]) -> Path:
        folder = self.s.output_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{slugify(topic.title)}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "topic.json").write_text(topic.model_dump_json(indent=2), encoding="utf-8")

        log.info("자료 조사: %s", topic.title)
        notes = self.writer.research(topic, signals)
        (folder / "research.md").write_text(notes, encoding="utf-8")

        log.info("원고 작성")
        card = self.writer.write(topic, notes)
        (folder / "card.json").write_text(card.model_dump_json(indent=2), encoding="utf-8")
        (folder / "caption.txt").write_text(instagram_caption(card), encoding="utf-8")
        (folder / "threads.txt").write_text(card.threads_text, encoding="utf-8")

        log.info("이미지 렌더링")
        render_card(card, folder, self.s.fonts_dir, self.s.theme, self.s.brand_handle)
        return folder

    # ----- 게시 -----
    def publish(self, folder: Path, publishers=None, host=None) -> dict:
        # 원고 파일이 손상되지 않았는지 먼저 검증
        CardNews.model_validate_json((folder / "card.json").read_text(encoding="utf-8"))
        topic = TopicIdea.model_validate_json((folder / "topic.json").read_text(encoding="utf-8"))
        meta_path = folder / "published.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

        # 사람이 캡션 파일을 수정했으면 그걸 사용
        caption = (folder / "caption.txt").read_text(encoding="utf-8").strip()
        threads_text = (folder / "threads.txt").read_text(encoding="utf-8").strip()[:500]

        images = sorted(folder.glob("slide_*.jpg"))
        if "image_urls" not in meta:
            host = host or make_host(self.s)
            meta["image_urls"] = host.upload(images, folder.name)
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

        for pub in publishers if publishers is not None else make_publishers(self.s):
            if pub.name in meta.get("posts", {}):
                log.info("%s: 이미 게시됨, 건너뜀", pub.name)
                continue
            text = caption if pub.name == "instagram" else threads_text
            try:
                res = pub.publish(meta["image_urls"], text)
            except Exception as e:
                log.error("%s 게시 실패: %s", pub.name, e)
                meta.setdefault("errors", {})[pub.name] = str(e)
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
                continue
            log.info("%s 게시 완료: %s", pub.name, res.permalink or res.media_id)
            meta.setdefault("posts", {})[pub.name] = {"media_id": res.media_id, "permalink": res.permalink}
            meta.get("errors", {}).pop(pub.name, None)
            self.store.add(topic.title, topic.keyword, pub.name, res.media_id, res.permalink, folder.name)
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        return meta

    # ----- 성과 수집 -----
    def refresh_insights(self, publishers=None) -> int:
        pubs = {p.name: p for p in (publishers if publishers is not None else make_publishers(self.s))}
        n = 0
        for row in self.store.posts_for_insights():
            pub = pubs.get(row["platform"])
            if not pub:
                continue
            try:
                self.store.set_metrics(row["id"], pub.insights(row["media_id"]))
                n += 1
            except Exception as e:
                log.warning("인사이트 조회 실패 %s/%s: %s", row["platform"], row["media_id"], e)
        return n

    # ----- 한 사이클 -----
    def run_once(self, publish: bool) -> list[Path]:
        topics, signals = self.recommend(n=max(3, self.s.posts_per_run))
        folders = []
        for topic in topics[: self.s.posts_per_run]:
            log.info("선택된 주제: %s (score %d)", topic.title, topic.score)
            folder = self.make(topic, signals)
            folders.append(folder)
            if publish:
                self.publish(folder)
            else:
                log.info("미리보기만 생성했습니다 (게시 안 함): %s", folder)
        return folders
