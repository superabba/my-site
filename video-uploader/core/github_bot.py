"""카드뉴스 봇 저장소(GitHub)로 영상을 보내고, '영상 업로드' 워크플로를 실행·확인한다.

흐름: 영상·post.json을 `video/<ID>` 브랜치로 한 번에 커밋 → 워크플로 실행(video_id=<ID>)
     → 봇이 릴스·스레드·쇼츠에 게시 → `video-records` 브랜치의 records/<ID>.json에서 결과 확인.
카드뉴스 자동 발행(cardbot-data 브랜치, cardnews.yml)과는 브랜치·워크플로가 모두 따로다.

토큰: 이 저장소만 고른 fine-grained 토큰, 권한 Contents·Actions 'Read and write'.
"""

import base64
import json
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import requests

API = "https://api.github.com"
WORKFLOW_FILE = "video-upload.yml"
RECORDS_BRANCH = "video-records"


class GitHubError(Exception):
    pass


def new_video_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"


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
        if not token or not repo:
            raise GitHubError("계정 설정에서 GitHub 저장소와 토큰을 먼저 입력해 주세요.")
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
            raise GitHubError(f"GitHub에 연결하지 못했습니다: {e}") from e
        if resp.status_code in ok:
            return resp
        try:
            message = resp.json().get("message", "")
        except ValueError:
            message = resp.text[:200]
        if resp.status_code == 401:
            raise GitHubError("GitHub 토큰이 잘못되었거나 만료되었습니다. 계정 설정에서 새 토큰을 넣어 주세요.")
        if resp.status_code == 403:
            raise GitHubError(f"토큰 권한이 부족합니다 (Contents·Actions 'Read and write' 필요). {message}")
        if resp.status_code == 404:
            raise GitHubError(f"찾을 수 없습니다: {path} — 저장소 이름과 토큰의 저장소 접근 범위를 확인해 주세요.")
        raise GitHubError(f"GitHub 오류 {resp.status_code}: {message}")

    # ── 연결 확인 ──
    def check(self) -> dict:
        repo = self._req("GET", "").json()
        try:
            self._req("GET", f"/actions/workflows/{WORKFLOW_FILE}")
        except GitHubError as e:
            raise GitHubError(
                f"저장소에 '영상 업로드' 워크플로({WORKFLOW_FILE})가 없습니다. "
                "영상 업로드 기능 PR이 기본 브랜치에 병합됐는지 확인해 주세요."
            ) from e
        return {"default_branch": repo.get("default_branch", "main"), "private": repo.get("private", False)}

    # ── 영상 보내기 ──
    def _blob(self, data: bytes, progress: Callable[[int], None] | None = None) -> str:
        body = b'{"encoding":"base64","content":"' + base64.b64encode(data) + b'"}'
        # base64는 원본보다 4/3배 크므로 진행률은 원본 크기 기준으로 환산한다
        scale = len(data) / max(len(body), 1)
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
            "message": f"video: {video_id}", "tree": tree, "parents": [],
        }).json()["sha"]
        self._req("POST", "/git/refs", json={"ref": f"refs/heads/video/{video_id}", "sha": commit})
        if progress_cb:
            progress_cb(1.0)
        return commit

    def delete_video_branch(self, video_id: str) -> None:
        self._req("DELETE", f"/git/refs/heads/video/{video_id}", ok=(204, 404, 422))

    # ── 워크플로 ──
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

    def read_record(self, video_id: str) -> dict | None:
        resp = self._req("GET", f"/contents/records/{video_id}.json", ok=(200, 404),
                         params={"ref": RECORDS_BRANCH},
                         headers={"Accept": "application/vnd.github.raw+json"})
        if resp.status_code == 404:
            return None
        return json.loads(resp.content.decode("utf-8"))
