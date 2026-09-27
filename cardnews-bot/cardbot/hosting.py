"""이미지·영상 공개 URL 만들기.

Instagram / Threads 게시 API는 파일 업로드가 아니라 '공개 URL'에서 이미지를 가져간다.
그래서 렌더링한 JPEG을 먼저 공개 저장소에 올려야 한다.

- github: 공개 GitHub 저장소에 커밋하고 raw.githubusercontent.com URL 사용 (무료, 설정 간단)
- s3: AWS S3 / Cloudflare R2 등 S3 호환 버킷 (boto3 필요)
- local: 이미 웹에 공개된 폴더(PUBLIC_DIR)로 복사하고 PUBLIC_BASE_URL로 접근
"""

from __future__ import annotations

import base64
import hashlib
import re
import shutil
from pathlib import Path

import requests


CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".mp4": "video/mp4"}


class ImageHost:
    def upload(self, files: list[Path], folder: str) -> list[str]:
        raise NotImplementedError

    def candidates(self, url: str) -> list[str]:
        """같은 파일을 가리키는 URL 후보 (앞쪽부터 시도). 기본은 그 URL 하나."""
        return [url]


def public_folder(folder: str) -> str:
    """공개 URL에 쓸 ASCII 전용 폴더 이름.

    Meta의 이미지 다운로더는 한글 등 비ASCII 문자가 들어간 URL을 가져오지 못하므로
    (error_subcode 2207052), 날짜-시각 접두어와 원래 이름의 해시로 바꾼다.
    """
    m = re.match(r"\d{8}-\d{6}", folder)
    digest = hashlib.sha1(folder.encode("utf-8")).hexdigest()[:8]
    return f"{m.group(0)}-{digest}" if m else digest


class GitHubHost(ImageHost):
    def __init__(self, token: str, repo: str, branch: str, base_dir: str):
        if not token or not repo:
            raise ValueError("github 호스팅에는 GITHUB_TOKEN, GITHUB_REPO가 필요합니다")
        self.token, self.repo, self.branch, self.base_dir = token, repo, branch, base_dir.strip("/")
        self._commits: dict[str, str] = {}  # raw URL → 올린 커밋 sha

    def upload(self, files: list[Path], folder: str) -> list[str]:
        folder = public_folder(folder)
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
        }
        urls = []
        for f in files:
            path = f"{self.base_dir}/{folder}/{f.name}"
            api = f"https://api.github.com/repos/{self.repo}/contents/{path}"
            body = {
                "message": f"cardnews: {folder}/{f.name}",
                "content": base64.b64encode(f.read_bytes()).decode(),
                "branch": self.branch,
            }
            # 재시도로 같은 경로에 다시 올릴 때는 기존 파일의 sha가 있어야 덮어쓸 수 있다
            existing = requests.get(api, headers=headers, params={"ref": self.branch}, timeout=30)
            if existing.status_code == 200:
                body["sha"] = existing.json().get("sha")
            r = requests.put(api, headers=headers, json=body, timeout=60)
            if r.status_code not in (200, 201):
                raise RuntimeError(f"GitHub 업로드 실패 {r.status_code}: {r.text[:300]}")
            url = f"https://raw.githubusercontent.com/{self.repo}/{self.branch}/{path}"
            sha = (r.json().get("commit") or {}).get("sha")
            if sha:
                self._commits[url] = sha
            urls.append(url)
        return urls

    def candidates(self, url: str) -> list[str]:
        """raw.githubusercontent.com은 영상을 application/octet-stream으로 보내므로,
        video/mp4로 내려주는 jsDelivr(커밋 고정 주소)를 먼저 쓰고 raw는 대안으로 남긴다."""
        sha = self._commits.get(url)
        prefix = f"https://raw.githubusercontent.com/{self.repo}/{self.branch}/"
        if not sha or not url.startswith(prefix):
            return [url]
        return [f"https://cdn.jsdelivr.net/gh/{self.repo}@{sha}/{url[len(prefix):]}", url]


class S3Host(ImageHost):
    def __init__(self, bucket: str, endpoint: str, prefix: str, public_base_url: str):
        if not bucket or not public_base_url:
            raise ValueError("s3 호스팅에는 S3_BUCKET, PUBLIC_BASE_URL이 필요합니다")
        import boto3  # 선택 의존성

        self.s3 = boto3.client("s3", endpoint_url=endpoint or None)
        self.bucket, self.prefix, self.base = bucket, prefix.strip("/"), public_base_url

    def upload(self, files: list[Path], folder: str) -> list[str]:
        folder = public_folder(folder)
        urls = []
        for f in files:
            key = f"{self.prefix}/{folder}/{f.name}"
            ctype = CONTENT_TYPES.get(f.suffix.lower(), "application/octet-stream")
            self.s3.upload_file(str(f), self.bucket, key, ExtraArgs={"ContentType": ctype})
            urls.append(f"{self.base}/{key}")
        return urls


class LocalHost(ImageHost):
    def __init__(self, public_dir: str, public_base_url: str):
        if not public_dir or not public_base_url:
            raise ValueError("local 호스팅에는 PUBLIC_DIR, PUBLIC_BASE_URL이 필요합니다")
        self.dir, self.base = Path(public_dir), public_base_url

    def upload(self, files: list[Path], folder: str) -> list[str]:
        folder = public_folder(folder)
        dest = self.dir / folder
        dest.mkdir(parents=True, exist_ok=True)
        urls = []
        for f in files:
            shutil.copy2(f, dest / f.name)
            urls.append(f"{self.base}/{folder}/{f.name}")
        return urls


def make_host(s) -> ImageHost:
    if s.hosting == "github":
        return GitHubHost(s.github_token, s.github_repo, s.github_branch, s.github_dir)
    if s.hosting == "s3":
        return S3Host(s.s3_bucket, s.s3_endpoint, s.s3_prefix, s.public_base_url)
    if s.hosting == "local":
        return LocalHost(s.public_dir, s.public_base_url)
    raise ValueError(f"알 수 없는 IMAGE_HOSTING: {s.hosting}")
