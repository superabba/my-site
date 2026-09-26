"""이미지 공개 URL 만들기.

Instagram / Threads 게시 API는 파일 업로드가 아니라 '공개 URL'에서 이미지를 가져간다.
그래서 렌더링한 JPEG을 먼저 공개 저장소에 올려야 한다.

- github: 공개 GitHub 저장소에 커밋하고 raw.githubusercontent.com URL 사용 (무료, 설정 간단)
- s3: AWS S3 / Cloudflare R2 등 S3 호환 버킷 (boto3 필요)
- local: 이미 웹에 공개된 폴더(PUBLIC_DIR)로 복사하고 PUBLIC_BASE_URL로 접근
"""

from __future__ import annotations

import base64
import shutil
from pathlib import Path

import requests


class ImageHost:
    def upload(self, files: list[Path], folder: str) -> list[str]:
        raise NotImplementedError


class GitHubHost(ImageHost):
    def __init__(self, token: str, repo: str, branch: str, base_dir: str):
        if not token or not repo:
            raise ValueError("github 호스팅에는 GITHUB_TOKEN, GITHUB_REPO가 필요합니다")
        self.token, self.repo, self.branch, self.base_dir = token, repo, branch, base_dir.strip("/")

    def upload(self, files: list[Path], folder: str) -> list[str]:
        urls = []
        for f in files:
            path = f"{self.base_dir}/{folder}/{f.name}"
            r = requests.put(
                f"https://api.github.com/repos/{self.repo}/contents/{path}",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                },
                json={
                    "message": f"cardnews: {folder}/{f.name}",
                    "content": base64.b64encode(f.read_bytes()).decode(),
                    "branch": self.branch,
                },
                timeout=60,
            )
            if r.status_code not in (200, 201):
                raise RuntimeError(f"GitHub 업로드 실패 {r.status_code}: {r.text[:300]}")
            urls.append(f"https://raw.githubusercontent.com/{self.repo}/{self.branch}/{path}")
        return urls


class S3Host(ImageHost):
    def __init__(self, bucket: str, endpoint: str, prefix: str, public_base_url: str):
        if not bucket or not public_base_url:
            raise ValueError("s3 호스팅에는 S3_BUCKET, PUBLIC_BASE_URL이 필요합니다")
        import boto3  # 선택 의존성

        self.s3 = boto3.client("s3", endpoint_url=endpoint or None)
        self.bucket, self.prefix, self.base = bucket, prefix.strip("/"), public_base_url

    def upload(self, files: list[Path], folder: str) -> list[str]:
        urls = []
        for f in files:
            key = f"{self.prefix}/{folder}/{f.name}"
            self.s3.upload_file(str(f), self.bucket, key, ExtraArgs={"ContentType": "image/jpeg"})
            urls.append(f"{self.base}/{key}")
        return urls


class LocalHost(ImageHost):
    def __init__(self, public_dir: str, public_base_url: str):
        if not public_dir or not public_base_url:
            raise ValueError("local 호스팅에는 PUBLIC_DIR, PUBLIC_BASE_URL이 필요합니다")
        self.dir, self.base = Path(public_dir), public_base_url

    def upload(self, files: list[Path], folder: str) -> list[str]:
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
