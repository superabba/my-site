"""유튜브 업로드용 refresh token을 한 번 발급받는 스크립트 (내 컴퓨터에서 실행).

    python -m cardbot.youtube_auth <OAuth 클라이언트 ID> <클라이언트 보안 비밀번호>

1) 브라우저가 열리면 구글 계정으로 로그인
2) '채널 선택' 화면에서 쇼츠를 올릴 유튜브 채널(브랜드 계정)을 고르기
3) 권한 허용 → 터미널에 refresh token과 인증된 채널 이름·ID가 출력됨
   → GitHub Secrets에 YOUTUBE_REFRESH_TOKEN으로 등록
"""

from __future__ import annotations

import http.server
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import requests

from .youtube import API, SCOPES, TOKEN_URL

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        print(__doc__)
        sys.exit(1)
    client_id, client_secret = argv
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in qs.items()})
            ok = "code" in result and result.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "인증 완료! 터미널로 돌아가세요." if ok else "인증 실패. 터미널 메시지를 확인하세요."
            self.wfile.write(f"<h2>{msg}</h2>".encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent select_account",  # refresh token을 확실히 받고, 계정·채널을 다시 고르게
        "state": state,
    })
    print("브라우저에서 로그인 후 쇼츠를 올릴 채널을 선택하세요.\n열리지 않으면 이 주소를 직접 여세요:\n" + url)
    threading.Timer(1, lambda: webbrowser.open(url)).start()
    server.handle_request()

    if result.get("state") != state or "code" not in result:
        sys.exit(f"인증 실패: {result.get('error', '응답이 올바르지 않습니다')}")
    r = requests.post(TOKEN_URL, data={
        "code": result["code"], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code",
    }, timeout=30)
    r.raise_for_status()
    tokens = r.json()
    if "refresh_token" not in tokens:
        sys.exit("refresh token을 받지 못했습니다. 구글 계정 설정 > 보안 > 서드파티 앱 연결에서 이 앱을 제거하고 다시 실행하세요.")
    ch = requests.get(f"{API}/channels", params={"part": "id,snippet", "mine": "true"},
                      headers={"Authorization": f"Bearer {tokens['access_token']}"}, timeout=30).json()
    items = ch.get("items") or []
    print("\n=== 인증 완료 ===")
    if items:
        print(f"채널: {items[0]['snippet']['title']}  (ID: {items[0]['id']})")
        print("→ 원하는 채널이 아니면 다시 실행해서 '채널 선택'에서 다른 채널을 고르세요.")
        print(f"→ GitHub Variables에 YOUTUBE_CHANNEL_ID={items[0]['id']} 를 넣으면 다른 채널로 잘못 올라가는 것을 막습니다.")
    else:
        print("⚠ 이 계정에는 유튜브 채널이 없습니다. 채널을 만든 뒤 다시 실행하세요.")
    print("\nGitHub Secrets에 아래 값을 YOUTUBE_REFRESH_TOKEN으로 등록하세요 (다른 곳에 공유 금지):")
    print(tokens["refresh_token"])


if __name__ == "__main__":
    main(sys.argv[1:])
