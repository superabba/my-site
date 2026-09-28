"""트렌드 수집 → 주제 추천 → 조사 → 원고 → 렌더링 → 업로드 → 게시."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from . import trends
from .blog import caption_hashtags, has_blog, save_blog
from .hosting import make_host
from .llm import BaseWriter, CardNews, TopicIdea, instagram_caption, make_writer
from .publishers import make_publishers
from .reel import render_reel
from .render import render_card
from .store import Store

log = logging.getLogger(__name__)


def slugify(text: str, n: int = 30) -> str:
    s = re.sub(r"[^\w가-힣]+", "-", text).strip("-")
    return s[:n] or "card"


class Pipeline:
    def __init__(self, settings, writer: BaseWriter | None = None, store: Store | None = None):
        self.s = settings
        self.writer = writer or make_writer(settings)
        self.store = store or Store(settings.data_dir / "cardbot.db")
        self.failures: list[str] = []  # 이번 실행에서 실패한 게시 (CLI 종료 코드용)

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

        if self.s.reels_enabled:
            self.try_make_reel(folder)
        if self.s.blog_enabled:
            self.try_make_blog(folder)
        return folder

    # ----- 릴스 영상 -----
    def make_reel(self, folder: Path) -> Path:
        card = CardNews.model_validate_json((folder / "card.json").read_text(encoding="utf-8"))
        slides = sorted(folder.glob("slide_*.jpg"))
        log.info("릴스 영상 렌더링")
        video, _ = render_reel(
            card, slides, folder / "reel.mp4", self.s.fonts_dir, self.s.theme,
            self.s.brand_handle, self.s.reels_audio, fps=self.s.reels_fps,
            seconds_per_slide=self.s.reels_seconds_per_slide,
        )
        (folder / "reel_error.txt").unlink(missing_ok=True)
        return video

    def try_make_reel(self, folder: Path) -> bool:
        """릴스 영상 실패가 카드뉴스 제작을 막지 않도록 오류는 기록만 한다."""
        try:
            self.make_reel(folder)
            return True
        except Exception as e:
            log.error("릴스 영상 생성 실패: %s", e)
            (folder / "reel_error.txt").write_text(str(e), encoding="utf-8")
            return False

    # ----- 블로그 글 (네이버/티스토리 붙여넣기용) -----
    def make_blog(self, folder: Path) -> None:
        topic = TopicIdea.model_validate_json((folder / "topic.json").read_text(encoding="utf-8"))
        card = CardNews.model_validate_json((folder / "card.json").read_text(encoding="utf-8"))
        research = folder / "research.md"
        notes = research.read_text(encoding="utf-8") if research.exists() else ""
        images = [p.name for p in sorted(folder.glob("slide_*.jpg"))]
        log.info("블로그 글 작성: %s", topic.title)
        save_blog(folder, self.writer.write_blog(topic, notes, card, images))
        (folder / "blog_error.txt").unlink(missing_ok=True)

    def try_make_blog(self, folder: Path) -> bool:
        """블로그 글 실패가 카드뉴스 제작·게시를 막지 않도록 오류는 기록만 한다."""
        try:
            self.make_blog(folder)
            return True
        except Exception as e:
            log.error("블로그 글 작성 실패: %s", e)
            (folder / "blog_error.txt").write_text(str(e), encoding="utf-8")
            return False

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
        # 비ASCII URL은 Meta가 가져오지 못하므로(이전 버전에서 올린 경우) 다시 올린다
        urls = meta.get("image_urls") or []
        if not urls or not all(u.isascii() for u in urls):
            host = host or make_host(self.s)
            meta["image_urls"] = host.upload(images, folder.name)
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

        pubs = publishers if publishers is not None else make_publishers(self.s)
        pending = [p for p in pubs if p.name not in meta.get("posts", {})]
        # 릴스·쇼츠: 영상이 없으면(이전 초안) 만든다
        reel = folder / "reel.mp4"
        wants_video = [p for p in pending if getattr(p, "needs_video", False) or getattr(p, "needs_video_file", False)]
        if wants_video and not reel.exists():
            self.try_make_reel(folder)
        # 인스타 릴스는 공개 URL이 필요하므로 아직 안 올렸으면 업로드
        if any(getattr(p, "needs_video", False) for p in pending) and not meta.get("video_urls") and reel.exists():
            host = host or make_host(self.s)
            video_url, cover_url = host.upload([reel, folder / "reel_cover.jpg"], folder.name)
            meta["video_urls"] = host.candidates(video_url)
            meta["reel_cover_url"] = cover_url
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        assets = {k: meta.get(k) for k in ("image_urls", "video_urls", "reel_cover_url")}
        # 유튜브는 로컬 파일을 직접 올리고, 제목·태그가 따로 필요하다
        assets["video_path"] = str(reel) if reel.exists() else None
        assets["title"] = topic.title
        assets["hashtags"] = caption_hashtags(caption)

        for pub in pubs:
            if pub.name in meta.get("posts", {}):
                log.info("%s: 이미 게시됨, 건너뜀", pub.name)
                continue
            text = threads_text if pub.name == "threads" else caption
            try:
                res = pub.publish(assets, text)
            except Exception as e:
                log.error("%s 게시 실패: %s", pub.name, e)
                meta.setdefault("errors", {})[pub.name] = str(e)
                self.failures.append(f"{folder.name}/{pub.name}: {e}")
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
                continue
            log.info("%s 게시 완료: %s", pub.name, res.permalink or res.media_id)
            meta.setdefault("posts", {})[pub.name] = {"media_id": res.media_id, "permalink": res.permalink}
            meta.get("errors", {}).pop(pub.name, None)
            self.store.add(topic.title, topic.keyword, pub.name, res.media_id, res.permalink, folder.name)
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

        # 블로그 기능 이전에 만든 초안이면 게시와 함께 블로그 글도 만든다
        if self.s.blog_enabled and not has_blog(folder):
            self.try_make_blog(folder)
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
