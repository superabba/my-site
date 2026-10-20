"""만든 영상을 카드뉴스 봇 저장소(GitHub)로 보내 '쇼츠 발행' 워크플로가 경제주막 계정에 게시하게 한다.

흐름: 영상·post.json을 `shorts/<ID>` 브랜치로 커밋 → `shorts-upload.yml` 실행(video_id=<ID>)
     → 봇이 경제주막(@economypub) 계정의 릴스·스레드·쇼츠에 게시 → `shorts-records` 브랜치에서 결과 확인.
카드뉴스 자동 발행(cardnews.yml, cardbot-data 브랜치)과는 워크플로·브랜치가 모두 따로다.
토큰: 봇 저장소만 고른 fine-grained 토큰, 권한 Contents·Actions 'Read and write' (video-uploader와 같은 토큰).
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import requests

from .config import ROOT, WORK_DIR
from .ffmpeg import FFMPEG

API = "https://api.github.com"
WORKFLOW_FILE = "shorts-upload.yml"
BRANCH_PREFIX = "shorts/"
RECORDS_BRANCH = "shorts-records"
ACCOUNT = "economypub"
ACCOUNT_LABEL = "@economypub (경제주막)"

PLATFORMS = ("reels", "threads", "youtube")
PLATFORM_LABELS = {"reels": "인스타 릴스", "threads": "스레드", "youtube": "유튜브 쇼츠"}
PRIVACY_LABELS = {"public": "공개", "unlisted": "일부 공개", "private": "비공개"}

# 인스타·스레드는 공개 주소에서 영상을 가져가는데, 봇이 쓰는 jsDelivr가 20MB 넘는 파일을 거절한다.
META_MAX_BYTES = 19 * 1024 * 1024
# 유튜브용 파일은 GitHub 파일 한도(100MB) 아래로 맞춘다.
YOUTUBE_MAX_BYTES = 90 * 1024 * 1024

PUBLISH_DIR = WORK_DIR / "publish"
HISTORY_FILE = ROOT / "publish_history.json"
VIDEO_UPLOADER_SETTINGS = ROOT.parent / "video-uploader" / "data" / "settings.json"  # 같은 저장소 안

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class PublishError(Exception):
    pass


def new_video_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"


def token_problem(token: str) -> str | None:
    t = (token or "").strip()
    if not t:
        return "GitHub 토큰을 입력해 주세요."
    if t.startswith("github_pat_"):
        return None
    if t.startswith(("ghp_", "gho_", "ghu_", "ghs_")):
        return "예전 방식(classic) 토큰입니다. 'github_pat_'로 시작하는 fine-grained 토큰을 넣어 주세요."
    return "GitHub 토큰은 'github_pat_'로 시작해야 합니다. 복사한 값이 맞는지 확인해 주세요."


def repo_problem(repo: str) -> str | None:
    if re.fullmatch(r"[\w.-]+/[\w.-]+", (repo or "").strip()):
        return None
    return "저장소는 'owner/repo' 형식으로 입력해 주세요 (예: superabba/my-site)."


def video_uploader_settings() -> dict | None:
    """같은 컴퓨터의 video-uploader가 쓰는 GitHub 저장소·토큰 (같은 봇 저장소라 그대로 쓸 수 있다)."""
    try:
        data = json.loads(VIDEO_UPLOADER_SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("github_token") and data.get("github_repo"):
        return {"github_repo": data["github_repo"], "github_token": data["github_token"]}
    return None


class _ProgressBody:
    """요청 본문을 조금씩 보내면서 진행률을 알려 준다 (requests가 len()으로 Content-Length를 정함)."""

    def __init__(self, data: bytes, cb: Callable[[int], None] | None):
        self._data, self._pos, self._cb = data, 0, cb

    def __len__(self) -> int:
        return len(self._data)

    def read(self, n: int = -1) -> bytes:
        n = 256 * 1024 if n is None or n < 0 else n
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        if self._cb and chunk:
            self._cb(len(chunk))
        return chunk


class GitHubBot:
    def __init__(self, token: str, repo: str):
        if problem := token_problem(token) or repo_problem(repo):
            raise PublishError(problem)
        self.repo = repo.strip().strip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token.strip()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def _req(self, method: str, path: str, ok=(200, 201, 204), **kw) -> requests.Response:
        kw.setdefault("timeout", 60)
        try:
            resp = self.session.request(method, f"{API}/repos/{self.repo}{path}", **kw)
        except requests.RequestException as e:
            raise PublishError(f"GitHub에 연결하지 못했습니다: {e}") from e
        if resp.status_code in ok:
            return resp
        try:
            message = resp.json().get("message", "")
        except ValueError:
            message = resp.text[:200]
        if resp.status_code == 401:
            raise PublishError("GitHub 토큰이 잘못되었거나 만료되었습니다. 발행 연결 설정에서 새 토큰을 넣어 주세요.")
        if resp.status_code == 403:
            raise PublishError(f"토큰 권한이 부족합니다 (Contents·Actions 'Read and write' 필요). {message}")
        if resp.status_code == 404:
            raise PublishError(f"찾을 수 없습니다: {path} — 저장소 이름과 토큰의 저장소 접근 범위를 확인해 주세요.")
        raise PublishError(f"GitHub 오류 {resp.status_code}: {message}")

    def check(self) -> dict:
        repo = self._req("GET", "").json()
        try:
            self._req("GET", f"/actions/workflows/{WORKFLOW_FILE}")
        except PublishError as e:
            raise PublishError(
                f"봇 저장소에 '쇼츠 발행' 워크플로({WORKFLOW_FILE})가 없습니다. "
                "쇼츠 발행 기능 PR이 main에 병합됐는지 확인해 주세요."
            ) from e
        return {"default_branch": repo.get("default_branch", "main"), "private": repo.get("private", False)}

    def _blob(self, data: bytes, progress: Callable[[int], None] | None = None) -> str:
        body = b'{"encoding":"base64","content":"' + base64.b64encode(data) + b'"}'
        scale = len(data) / max(len(body), 1)  # 진행률은 원본 크기 기준
        cb = (lambda n: progress(int(n * scale))) if progress else None
        resp = self._req("POST", "/git/blobs", data=_ProgressBody(body, cb),
                         headers={"Content-Type": "application/json"}, timeout=(30, 900))
        return resp.json()["sha"]

    def upload(self, video_id: str, files: dict[str, Path], post: dict,
               progress_cb: Callable[[float], None] | None = None) -> str:
        """files: {저장소 안 파일 이름: 내 컴퓨터 경로}. 영상 브랜치를 만들고 커밋 sha를 돌려준다."""
        total = sum(p.stat().st_size for p in files.values()) or 1
        sent = 0

        def advance(n: int):
            nonlocal sent
            sent += n
            if progress_cb:
                progress_cb(min(sent / total, 1.0))

        entries = []
        for name, path in files.items():
            sha = self._blob(path.read_bytes(), advance)
            entries.append({"path": f"videos/{video_id}/{name}", "mode": "100644", "type": "blob", "sha": sha})
        post_sha = self._blob(json.dumps(post, ensure_ascii=False, indent=2).encode("utf-8"))
        entries.append({"path": f"videos/{video_id}/post.json", "mode": "100644", "type": "blob", "sha": post_sha})
        tree = self._req("POST", "/git/trees", json={"tree": entries}).json()["sha"]
        commit = self._req("POST", "/git/commits", json={
            "message": f"shorts: {video_id}", "tree": tree, "parents": [],
        }).json()["sha"]
        self._req("POST", "/git/refs", json={"ref": f"refs/heads/{BRANCH_PREFIX}{video_id}", "sha": commit})
        if progress_cb:
            progress_cb(1.0)
        return commit

    def delete_branch(self, video_id: str) -> None:
        self._req("DELETE", f"/git/refs/heads/{BRANCH_PREFIX}{video_id}", ok=(204, 404, 422))

    def dispatch(self, video_id: str, ref: str) -> None:
        self._req("POST", f"/actions/workflows/{WORKFLOW_FILE}/dispatches",
                  json={"ref": ref, "inputs": {"video_id": video_id}})

    def find_run(self, video_id: str, wait_s: int = 90) -> dict | None:
        """방금 실행한 워크플로 실행(run)을 찾는다. 실행 이름에 영상 ID가 들어 있다."""
        deadline = time.time() + wait_s
        while time.time() < deadline:
            runs = self._req("GET", f"/actions/workflows/{WORKFLOW_FILE}/runs",
                             params={"event": "workflow_dispatch", "per_page": 30}).json()
            for run in runs.get("workflow_runs", []):
                if video_id in (run.get("display_title") or ""):
                    return run
            time.sleep(4)
        return None

    def get_run(self, run_id: int) -> dict:
        return self._req("GET", f"/actions/runs/{run_id}").json()

    def daily_records(self, limit: int = 7) -> list[dict]:
        """매일 자동 쇼츠의 최근 결과 (shorts-records 브랜치). 결과 브랜치가 아직 없으면 빈 목록."""
        resp = self._req("GET", "/contents/records", ok=(200, 404), params={"ref": RECORDS_BRANCH})
        if resp.status_code == 404:
            return []
        names = sorted((f["name"] for f in resp.json() if f.get("name", "").endswith(".json")), reverse=True)
        out = []
        for name in names[:40]:
            rec = self.read_record(name.removesuffix(".json"))
            if rec and rec.get("daily"):
                out.append(rec)
                if len(out) >= limit:
                    break
        return out

    def read_record(self, video_id: str) -> dict | None:
        resp = self._req("GET", f"/contents/records/{video_id}.json", ok=(200, 404),
                         params={"ref": RECORDS_BRANCH},
                         headers={"Accept": "application/vnd.github.raw+json"})
        if resp.status_code == 404:
            return None
        return json.loads(resp.content.decode("utf-8"))


# ---------------------------------------------------------------- 업로드용 파일

def probe_duration(path: Path) -> float:
    proc = subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-i", str(path)], stdin=subprocess.DEVNULL,
                          capture_output=True, creationflags=_NO_WINDOW)
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr.decode("utf-8", "replace"))
    if not m:
        return 0.0
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


def transcode(src: Path, out: Path, max_bytes: int, max_video_kbps: int,
              progress_cb: Callable[[float], None] | None = None) -> Path:
    """H.264/AAC로 다시 인코딩해 max_bytes 아래로 맞춘다."""
    duration = max(probe_duration(src), 1.0)
    audio_kbps = 128
    video_kbps = max(300, min(max_video_kbps, int(max_bytes * 8 / 1000 / duration * 0.9) - audio_kbps))
    for _ in range(3):
        cmd = [FFMPEG, "-nostdin", "-y", "-loglevel", "error", "-i", str(src),
               "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
               "-b:v", f"{video_kbps}k", "-maxrate", f"{int(video_kbps * 1.5)}k", "-bufsize", f"{video_kbps * 2}k",
               "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ar", "44100", "-ac", "2",
               "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(out)]
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=_NO_WINDOW, text=True, encoding="utf-8", errors="replace")
        for line in proc.stdout:
            if line.startswith("out_time_us=") and progress_cb:
                try:
                    progress_cb(min(int(line.split("=", 1)[1]) / 1e6 / duration, 1.0))
                except ValueError:
                    pass
        err = proc.stderr.read()
        if proc.wait() != 0:
            raise PublishError(f"영상 압축 실패: {err.strip()[-500:]}")
        if out.stat().st_size <= max_bytes:
            return out
        video_kbps = max(200, int(video_kbps * 0.75))
    raise PublishError(f"영상을 {max_bytes // (1024 * 1024)}MB 이하로 줄이지 못했습니다. 영상 길이를 줄여 주세요.")


def prepare_files(video: Path, platforms: list[str],
                  progress_cb: Callable[[str, float], None] | None = None) -> tuple[dict[str, Path], dict[str, str]]:
    """봇에 보낼 파일과 post.json의 files 항목을 만든다. 두 용도가 같은 파일이면 하나만 올린다."""
    PUBLISH_DIR.mkdir(parents=True, exist_ok=True)
    clear_work_files()
    say = progress_cb or (lambda _label, _p: None)
    size = video.stat().st_size
    uploads: dict[str, Path] = {}
    names: dict[str, str] = {}
    if "youtube" in platforms:
        yt = video if size <= YOUTUBE_MAX_BYTES else transcode(
            video, PUBLISH_DIR / "video.mp4", YOUTUBE_MAX_BYTES, 12000, lambda p: say("유튜브용 영상 압축 중", p))
        uploads["video.mp4"] = yt
        names["youtube"] = "video.mp4"
    if {"reels", "threads"} & set(platforms):
        if size <= META_MAX_BYTES:
            uploads.setdefault("video.mp4", video)
            names["meta"] = "video.mp4"
        else:
            uploads["meta.mp4"] = transcode(video, PUBLISH_DIR / "meta.mp4", META_MAX_BYTES, 8000,
                                            lambda p: say("인스타·스레드용 영상 압축 중 (20MB 이하)", p))
            names["meta"] = "meta.mp4"
    return uploads, names


def clear_work_files() -> None:
    if PUBLISH_DIR.exists():
        for f in PUBLISH_DIR.glob("*.mp4"):
            try:
                f.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- 발행 기록

def load_history() -> list[dict]:
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save_history(history: list[dict]) -> None:
    HISTORY_FILE.write_text(json.dumps(history[:300], ensure_ascii=False, indent=2), encoding="utf-8")


def add_history(video_id: str, video_name: str, title: str, platforms: list[str], run_url: str = "") -> None:
    history = load_history()
    history.insert(0, {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"), "video_id": video_id, "video": video_name,
        "title": title, "platforms": platforms, "status": "진행 중", "results": {}, "run_url": run_url,
    })
    _save_history(history)


def update_history(video_id: str, **fields) -> None:
    history = load_history()
    for entry in history:
        if entry.get("video_id") == video_id:
            entry.update(fields)
            break
    _save_history(history)


def status_from_record(record: dict) -> tuple[str, dict]:
    """봇의 결과 기록 → (상태, {플랫폼: 링크 또는 '실패: 사유'})."""
    results = {}
    for name in record.get("platforms", []):
        if name in record.get("posts", {}):
            info = record["posts"][name]
            results[name] = info.get("permalink") or f"게시됨 (ID {info.get('media_id')})"
        else:
            results[name] = "실패: " + record.get("errors", {}).get(name, "알 수 없는 오류")
    failed = [n for n in record.get("platforms", []) if n not in record.get("posts", {})]
    if not failed:
        return "완료", results
    if len(failed) == len(record.get("platforms", [])):
        return "실패", results
    return "일부 실패", results


# ---------------------------------------------------------------- 게시 문구 기본값

def default_texts(video: Path) -> dict:
    """영상과 같이 저장된 대본(.json)·설명(.txt)으로 게시 문구 기본값을 만든다."""
    script = {}
    try:
        script = json.loads(video.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    credits = ""
    try:
        info = video.with_suffix(".txt").read_text(encoding="utf-8")
        if "배경 출처:" in info:
            credits = info.split("배경 출처:", 1)[1].strip()
    except OSError:
        pass
    title = (script.get("title") or video.stem.split("_", 2)[-1].replace("_", " ")).strip()
    caption = (script.get("caption") or "").strip()
    narration = " ".join(s.get("narration", "") for s in script.get("scenes", [])).strip()
    body = caption or narration[:300]
    yt_desc = body + (f"\n\n배경 영상·사진 출처\n{credits}" if credits else "")
    ig = body + ("\n\n배경 영상: 무료 스톡(Pixabay·Pexels)" if credits else "")
    return {
        "yt_title": title[:100],
        "yt_desc": yt_desc,
        "ig_caption": ig[:2000],
        "threads_text": body[:500],
        "hashtags": " ".join(f"#{h}" for h in script.get("hashtags", [])),
        "synthetic": bool(script.get("ai_images")),
    }


def parse_hashtags(text: str) -> list[str]:
    tags = []
    for t in re.split(r"[\s,]+", text or ""):
        t = t.strip().lstrip("#")
        if t and t not in tags:
            tags.append(t)
    return tags
