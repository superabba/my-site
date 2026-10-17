"""영상 전용 계정(VIDEO_* Secrets)에 넣을 값을 확인·발급하는 도우미.

- 인스타·스레드: Meta 앱에서 받은 토큰으로 계정 ID와 아이디(@)를 확인한다.
- 유튜브: 구글 로그인 → 채널 선택으로 refresh token을 받는다 (카드뉴스 봇 cardbot/youtube_auth.py와 같은 방식).
여기서 얻은 값은 사용자가 GitHub Secrets에 직접 넣는다. 이 앱은 값을 저장하지 않는다.
"""

import http.server
import secrets
import threading
import urllib.parse
import webbrowser

import requests

IG_ME_URL = "https://graph.instagram.com/v23.0/me"
THREADS_ME_URL = "https://graph.threads.net/v1.0/me"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


class AccountError(Exception):
    pass


def _meta_get(url: str, token: str, fields: str) -> dict:
    try:
        resp = requests.get(url, params={"fields": fields, "access_token": token.strip()}, timeout=30)
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise AccountError(f"확인하지 못했습니다: {e}") from e
    if "error" in data:
        raise AccountError(f"토큰이 올바르지 않거나 만료되었습니다: {data['error'].get('message', data['error'])}")
    return data


def instagram_account(token: str) -> dict:
    """인스타그램 로그인 방식 토큰 → {"id": 게시용 계정 ID, "username": 아이디}."""
    data = _meta_get(IG_ME_URL, token, "user_id,username")
    return {"id": str(data.get("user_id") or data.get("id", "")), "username": data.get("username", "")}


def threads_account(token: str) -> dict:
    data = _meta_get(THREADS_ME_URL, token, "id,username")
    return {"id": str(data.get("id", "")), "username": data.get("username", "")}


def youtube_authorize(client_id: str, client_secret: str, timeout_s: int = 300) -> dict:
    """브라우저에서 구글 로그인·채널 선택 → {"refresh_token", "channel_id", "channel_title"}."""
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if not client_id or not client_secret:
        raise AccountError("OAuth 클라이언트 ID와 보안 비밀번호를 입력해 주세요.")
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
            msg = "인증 완료! 프로그램 화면으로 돌아가세요." if ok else "인증 실패. 프로그램 화면의 안내를 확인하세요."
            self.wfile.write(f"<h2>{msg}</h2>".encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = timeout_s
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": " ".join(YOUTUBE_SCOPES),
        "access_type": "offline",
        "prompt": "consent select_account",  # refresh token을 확실히 받고, 계정·채널을 다시 고르게
        "state": state,
    })
    threading.Timer(1, lambda: webbrowser.open(url)).start()
    try:
        server.handle_request()
    finally:
        server.server_close()

    if not result:
        raise AccountError("로그인 시간(5분)이 지났습니다. 다시 시도해 주세요.")
    if result.get("state") != state or "code" not in result:
        raise AccountError(f"인증 실패: {result.get('error', '응답이 올바르지 않습니다')}")
    try:
        r = requests.post(GOOGLE_TOKEN_URL, data={
            "code": result["code"], "client_id": client_id, "client_secret": client_secret,
            "redirect_uri": redirect, "grant_type": "authorization_code",
        }, timeout=30)
        tokens = r.json()
    except (requests.RequestException, ValueError) as e:
        raise AccountError(f"토큰을 받지 못했습니다: {e}") from e
    if r.status_code != 200:
        raise AccountError(f"토큰을 받지 못했습니다: {tokens.get('error_description') or tokens.get('error')}")
    if "refresh_token" not in tokens:
        raise AccountError("refresh token을 받지 못했습니다. 구글 계정 → 보안 → 서드파티 앱 연결에서 "
                           "이 앱을 제거하고 다시 시도해 주세요.")
    ch = requests.get(f"{YOUTUBE_API}/channels", params={"part": "id,snippet", "mine": "true"},
                      headers={"Authorization": f"Bearer {tokens['access_token']}"}, timeout=30).json()
    items = ch.get("items") or []
    if not items:
        raise AccountError("이 계정에는 유튜브 채널이 없습니다. 채널을 만든 뒤 다시 시도해 주세요.")
    return {"refresh_token": tokens["refresh_token"], "channel_id": items[0]["id"],
            "channel_title": items[0]["snippet"]["title"]}
