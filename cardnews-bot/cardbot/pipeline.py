"""트렌드 수집 → 주제 추천 → 조사 → 원고 → 렌더링 → 업로드 → 게시."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import trends
from .bgm import with_credit
from .blog import caption_hashtags, has_blog, refresh_blog, save_blog
from .hosting import make_host
from .blog import retarget_post
from .frames import frames_of
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
        # 루틴 방식(CONTENT_SOURCE=routine)에는 LLM이 없다: 원고는 Claude 루틴이 content/ 에 써 둔다
        self.writer = writer or (None if settings.content_source == "routine" else make_writer(settings))
        self.store = store or Store(settings.data_dir / "cardbot.db")
        self.failures: list[str] = []  # 이번 실행에서 실패한 게시 (CLI 종료 코드용)

    def _need_writer(self) -> BaseWriter:
        if self.writer is None:
            raise RuntimeError("CONTENT_SOURCE=routine 에서는 LLM이 없습니다. 원고는 Claude 루틴이 content/ 에 올립니다 (python -m cardbot ingest)")
        return self.writer

    # ----- 추천 -----
    def recommend(self, n: int = 5) -> tuple[list[TopicIdea], list[trends.TrendSignal]]:
        writer = self._need_writer()
        signals = trends.collect(self.s)
        topics = writer.recommend(
            signals, self.store.top_posts(), self.store.recent_topics(), n=n
        )
        return topics, signals

    # ----- 제작 -----
    def make(self, topic: TopicIdea, signals: list[trends.TrendSignal]) -> Path:
        folder = self.s.output_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{slugify(topic.title)}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "topic.json").write_text(topic.model_dump_json(indent=2), encoding="utf-8")

        writer = self._need_writer()
        log.info("자료 조사: %s", topic.title)
        notes = writer.research(topic, signals)
        (folder / "research.md").write_text(notes, encoding="utf-8")

        log.info("원고 작성")
        card = writer.write(topic, notes)
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
        if self.s.reels_engine == "lemo":
            try:
                return self.make_styled_reel(folder, card)
            except Exception as e:  # 스타일 영상이 실패해도 릴스·쇼츠는 예전 방식으로 올린다
                log.error("스타일 릴스 생성 실패, 기존 방식으로 만듭니다: %s", e)
                (folder / "reel_error.txt").write_text(f"lemo: {e}", encoding="utf-8")
        slides = sorted(folder.glob("slide_*.jpg"))
        log.info("릴스 영상 렌더링")
        video, _ = render_reel(
            card, slides, folder / "reel.mp4", self.s.fonts_dir, self.s.theme,
            self.s.brand_handle, self.s.reels_audio, fps=self.s.reels_fps,
            seconds_per_slide=self.s.reels_seconds_per_slide,
        )
        (folder / "reel_error.txt").unlink(missing_ok=True)
        return video

    def make_styled_reel(self, folder: Path, card: CardNews | None = None, style: str = "") -> Path:
        """주제에 맞는 스타일을 골라 내레이션·믹싱을 입힌 세로 영상을 만든다 (cardbot/lemoreel.py)."""
        from . import lemoreel

        card = card or CardNews.model_validate_json((folder / "card.json").read_text(encoding="utf-8"))
        topic = TopicIdea.model_validate_json((folder / "topic.json").read_text(encoding="utf-8"))
        forced = style or (self.s.reels_style if self.s.reels_style != "auto" else "")
        # 직전 두 편과 다른 스타일을 쓴다 (골고루 돌아가도록). 스타일을 직접 고정했으면 그대로 따른다.
        # 알 수 없는 이름(오타 등)은 고정으로 치지 않는다. 같은 초안을 다시 만들 때는 그 초안의 기록을 빼고 센다.
        avoid = () if forced in lemoreel.STYLES else tuple(
            self.store.recent_reel_styles(lemoreel.RECENT_WINDOW, exclude_folder=folder.name))
        writer = self.writer
        plan_file = folder / "reel.json"
        if plan_file.exists():  # 루틴 방식: 원고에 적힌 내레이션을 그대로 쓴다 (LLM 없음)
            from . import content as C

            fixed = C.read_reel_plan(plan_file)
            if forced not in lemoreel.STYLES:
                forced = C.choose_style(fixed.style, avoid, topic, card)
                avoid = ()
            fixed.style = forced
            writer = C.FixedWriter(fixed)
        video, _, info = lemoreel.make_reel(
            card, topic, folder, writer, self.s.fonts_dir, self.s.brand_handle or "@economypub",
            style=forced, voice=self.s.reels_voice, rate=self.s.reels_voice_rate, fps=self.s.reels_fps,
            avoid=avoid,
        )
        self.store.add_reel_style(info["style"], folder.name)
        (folder / "reel_error.txt").unlink(missing_ok=True)
        log.info("릴스 스타일: %s", info["label"])
        return video

    def try_make_reel(self, folder: Path) -> bool:
        """릴스 영상 실패가 카드뉴스 제작을 막지 않도록 오류는 기록만 한다."""
        try:
            self.make_reel(folder)
            self.try_capture_frames(folder)
            return True
        except Exception as e:
            log.error("릴스 영상 생성 실패: %s", e)
            (folder / "reel_error.txt").write_text(str(e), encoding="utf-8")
            return False

    def try_capture_frames(self, folder: Path) -> list[Path]:
        """릴스 영상에서 스레드·블로그용 장면 이미지(frame_NN.jpg)를 캡처한다. 실패하면 슬라이드를 대신 쓴다."""
        from . import frames

        try:
            got = frames.capture_frames(folder)
            log.info("릴스 장면 캡처 %d장", len(got))
            return got
        except Exception as e:
            log.warning("릴스 장면 캡처 실패, 슬라이드를 대신 씁니다: %s", e)
            return []

    # ----- 블로그 글 (네이버/티스토리 붙여넣기용) -----
    def make_blog(self, folder: Path) -> None:
        topic = TopicIdea.model_validate_json((folder / "topic.json").read_text(encoding="utf-8"))
        card = CardNews.model_validate_json((folder / "card.json").read_text(encoding="utf-8"))
        research = folder / "research.md"
        notes = research.read_text(encoding="utf-8") if research.exists() else ""
        images = [p.name for p in (frames_of(folder) or sorted(folder.glob("slide_*.jpg")))]
        log.info("블로그 글 작성: %s", topic.title)
        save_blog(folder, self._need_writer().write_blog(topic, notes, card, images))
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
        # CC BY 곡을 썼으면 영상이 올라가는 곳(인스타 릴스·유튜브)의 설명에만 출처 표기를 붙인다.
        # 스레드에는 영상이 없으니 붙이지 않는다. 직접 합성한 곡이면 아무것도 붙지 않는다.
        caption = with_credit(caption, folder, 2200)

        # 스레드·블로그에 쓸 이미지는 릴스 영상 캡처(frame_NN.jpg). 영상이 없으면 슬라이드를 쓴다
        images = frames_of(folder) or (self.try_capture_frames(folder) if (folder / "reel.mp4").exists() else [])
        images = images or sorted(folder.glob("slide_*.jpg"))
        # 비ASCII URL은 Meta가 가져오지 못하므로(이전 버전에서 올린 경우) 다시 올린다. 파일이 바뀌었어도 다시 올린다
        urls = meta.get("image_urls") or []
        if not urls or not all(u.isascii() for u in urls) or [u.rsplit("/", 1)[-1] for u in urls] != [p.name for p in images]:
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
        # 인스타 카드 슬라이드(캐러셀)를 다시 켠 경우에만 슬라이드 원본도 올린다 (캡처는 세로 9:16이라 캐러셀 비율이 안 맞는다)
        if (any(p.name == "instagram" for p in pending) and images and images[0].name.startswith("frame_")
                and not meta.get("slide_urls")):
            host = host or make_host(self.s)
            meta["slide_urls"] = host.upload(sorted(folder.glob("slide_*.jpg")), folder.name)
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        assets = {k: meta.get(k) for k in ("image_urls", "slide_urls", "video_urls", "reel_cover_url")}
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
        elif has_blog(folder):  # 올린 사진 URL을 넣어 블로그 파일을 다시 만든다
            try:
                refresh_blog(folder)
            except Exception as e:
                log.warning("블로그 글 사진 넣기 실패: %s", e)
        return meta

    # ----- 하루 1번 보장 -----
    def done_today(self, publish: bool, now: datetime | None = None) -> bool:
        """오늘(한국 시간) 이미 게시(또는 초안 생성)했는지. 예약을 여러 번 걸어도 하루 1번만 돌게 한다."""
        now = now or datetime.now().astimezone()
        kst = timezone(timedelta(hours=9))
        start = now.astimezone(kst).replace(hour=0, minute=0, second=0, microsecond=0)
        start = start.astimezone(now.tzinfo).replace(tzinfo=None)  # DB·폴더 이름은 실행 환경 현지 시각
        if publish:
            return self.store.posted_since(start)
        for d in self.s.output_dir.glob("*-*"):
            try:
                if datetime.strptime(d.name[:15], "%Y%m%d-%H%M%S") >= start:
                    return True
            except ValueError:
                continue
        return False

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
        # 30일 안에 갱신되지 않은 YouTube 지표는 삭제 (개인정보처리방침 5항)
        self.store.purge_stale_metrics("youtube", days=30)
        return n

    # ----- 한 사이클 -----
    # ----- 루틴 방식: 원고(content/) → 초안 → 게시 -----
    def ingest(self, content_dir: Path) -> Path:
        """루틴이 올린 원고 폴더를 기존 초안 폴더(카드 이미지·릴스·블로그 파일)로 만든다. LLM을 쓰지 않는다."""
        import shutil

        from . import content as C
        from .llm import BlogPost, normalize, normalize_blog

        for d in sorted(self.s.output_dir.glob("*-*")):  # 이미 만든 원고면 그 초안을 돌려준다
            src = d / C.SOURCE_FILE
            if src.exists() and src.read_text(encoding="utf-8").strip() == content_dir.name:
                log.info("이미 초안으로 만든 원고입니다: %s → %s", content_dir.name, d.name)
                return d
        topic = TopicIdea.model_validate_json((content_dir / "topic.json").read_text(encoding="utf-8"))
        card = normalize(CardNews.model_validate_json((content_dir / "card.json").read_text(encoding="utf-8")),
                         self.s.slides_min, self.s.slides_max)
        folder = self.s.output_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{slugify(topic.title)}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "topic.json").write_text(topic.model_dump_json(indent=2), encoding="utf-8")
        (folder / "card.json").write_text(card.model_dump_json(indent=2), encoding="utf-8")
        (folder / "caption.txt").write_text(instagram_caption(card), encoding="utf-8")
        (folder / "threads.txt").write_text(card.threads_text, encoding="utf-8")
        if (content_dir / "reel.json").exists():
            shutil.copy2(content_dir / "reel.json", folder / "reel.json")
        research = content_dir / "research.md"
        (folder / "research.md").write_text(
            research.read_text(encoding="utf-8") if research.exists() else "\n".join(f"- {u}" for u in card.sources),
            encoding="utf-8")
        (folder / C.SOURCE_FILE).write_text(content_dir.name, encoding="utf-8")

        log.info("이미지 렌더링: %s", topic.title)
        render_card(card, folder, self.s.fonts_dir, self.s.theme, self.s.brand_handle)
        if self.s.reels_enabled:
            self.try_make_reel(folder)
        blog_file = content_dir / "blog.json"
        if self.s.blog_enabled and blog_file.exists():
            images = [p.name for p in (frames_of(folder) or sorted(folder.glob("slide_*.jpg")))]
            post = BlogPost.model_validate_json(blog_file.read_text(encoding="utf-8"))
            retarget_post(post, folder)  # 루틴이 쓴 slide_NN.jpg 는 릴스 캡처 frame_NN.jpg 로
            save_blog(folder, normalize_blog(post, images))
        return folder

    def ingest_all(self, root: Path) -> list[Path]:
        from . import content as C

        return [self.ingest(d) for d in C.content_dirs(root)]

    def unpublished_ingested(self, max_age_hours: int = 30) -> list[Path]:
        """루틴 원고로 만든 초안 중 아직 다 안 올린 것 (최신순). 너무 오래된 초안은 늦게 올리지 않는다."""
        from . import content as C

        now = datetime.now()
        out = []
        for d in sorted(self.s.output_dir.glob("*-*"), reverse=True):
            if not (d / C.SOURCE_FILE).exists():
                continue
            try:
                made = datetime.strptime(d.name[:15], "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            if now - made > timedelta(hours=max_age_hours):
                break
            meta_path = d / "published.json"
            posts = json.loads(meta_path.read_text(encoding="utf-8")).get("posts", {}) if meta_path.exists() else {}
            if any(p not in posts for p in self.s.platforms):
                out.append(d)
        return out

    def publish_ingested(self, publish: bool) -> list[Path]:
        folders = self.unpublished_ingested()[: self.s.posts_per_run]
        if not folders:
            log.info("올릴 원고가 없습니다 (루틴이 아직 원고를 올리지 않았거나 이미 게시했습니다)")
        for folder in folders:
            if publish:
                self.publish(folder)
            else:
                log.info("미리보기만 둡니다 (게시 안 함): %s", folder)
        return folders

    def run_once(self, publish: bool) -> list[Path]:
        if self.s.content_source == "routine":
            return self.publish_ingested(publish)
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
