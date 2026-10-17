# 영상 업로더 (video-uploader)

직접 찍은 영상을 **인스타그램 릴스 · 스레드 · 유튜브 쇼츠**에 한 번에 올리는 PC 프로그램입니다.
Claude API 키를 넣으면 영상을 보고 제목·설명·캡션·스레드 글·해시태그를 자동으로 써 주고, 키가 없으면 직접 입력한 내용으로 올라갑니다.

실제 게시는 이 저장소의 **카드뉴스 봇**이 GitHub Actions에서 합니다. 올라가는 계정은 카드뉴스와 다른 **영상 전용 계정**이고, 그 계정 정보는 GitHub Secrets(`VIDEO_*`)에 넣습니다. 이 프로그램에는 GitHub 토큰 하나만 넣으면 됩니다.

```
PC 앱: 영상 선택 → (AI로 문구 작성) → [업로드 시작]
  └─▶ GitHub: video/<ID> 브랜치에 영상·문구 커밋 → '영상 업로드' 워크플로 실행
        └─▶ 봇: 인스타 릴스 · 스레드 · 유튜브 쇼츠 게시 → video-records 브랜치에 결과 기록
              └─▶ PC 앱에 게시 링크 표시, 게시가 끝난 영상 브랜치는 자동 삭제
```

## 카드뉴스 자동 발행과는 완전히 따로 동작합니다

| | 카드뉴스 자동 발행 | 영상 업로드 |
|---|---|---|
| 워크플로 | `cardnews.yml` (매일 예약) | `video-upload.yml` (PC 앱이 실행할 때만) |
| 실행 순서 그룹 | `cardbot` | `video-upload` — 서로 기다리거나 막지 않음 |
| 데이터 | `cardbot-data` 브랜치, 이력 DB | `video/<ID>`, `video-records` 브랜치 |
| "오늘 이미 게시함" 판단 | 카드뉴스 이력 DB만 봄 | 카드뉴스 이력 DB에 쓰지 않음 |
| 게시 계정 | `IG_*`, `THREADS_*`, `YOUTUBE_REFRESH_TOKEN` | `VIDEO_IG_*`, `VIDEO_THREADS_*`, `VIDEO_YOUTUBE_REFRESH_TOKEN` |
| 토큰 자동 갱신 | `cardnews-refresh-tokens.yml` | `video-refresh-tokens.yml` |

영상을 하루에 몇 개 올려도 카드뉴스는 원래 일정대로 하루 1번 게시됩니다.
영상 전용 계정 값이 없으면 **카드뉴스 계정으로 대신 올리지 않고** 그 플랫폼만 실패로 표시합니다.
같이 쓰는 것은 Meta 개발자 앱과 구글 OAuth 클라이언트(둘 다 '앱'이라 계정과 무관)뿐입니다.

---

## 1. 처음 한 번만: 준비

### 1-1. 이 기능을 main에 병합
`영상 업로드` 워크플로는 기본 브랜치(main)에 있어야 실행됩니다. 이 기능의 PR을 병합하세요.

### 1-2. 영상 전용 계정 연결
저장소 **Settings → Secrets and variables → Actions** 에 아래 값을 넣습니다. 쓰지 않을 플랫폼은 비워 두면 됩니다.

| 올릴 곳 | Secrets | Variables (선택) |
|---|---|---|
| 인스타 릴스 | `VIDEO_IG_USER_ID`, `VIDEO_IG_ACCESS_TOKEN` | |
| 스레드 | `VIDEO_THREADS_USER_ID`, `VIDEO_THREADS_ACCESS_TOKEN` | |
| 유튜브 쇼츠 | `VIDEO_YOUTUBE_REFRESH_TOKEN` | `VIDEO_YOUTUBE_CHANNEL_ID` (권장), `VIDEO_YOUTUBE_CATEGORY` (기본 22=인물/블로그) |

**인스타그램**
1. 영상용 인스타 계정을 **프로페셔널 계정**(크리에이터/비즈니스)으로 전환합니다.
2. 카드뉴스에 쓰는 Meta 개발자 앱의 Instagram API(인스타그램 로그인 방식) 설정 화면에서, 카드뉴스 계정 때와 같은 방법으로 **영상용 계정을 추가하고 토큰을 생성**합니다.
   앱이 개발 모드라면 영상용 계정을 앱의 **Instagram 테스터**로 추가하고, 인스타 앱 설정에서 초대를 수락해야 합니다.
3. PC 앱 **계정 설정 → 영상 전용 계정 → 인스타·스레드 토큰으로 계정 ID 확인**에 토큰을 넣으면 `@아이디`와 계정 ID가 나옵니다.
   아이디가 영상용 계정인지 확인하고, ID는 `VIDEO_IG_USER_ID`, 토큰은 `VIDEO_IG_ACCESS_TOKEN`에 넣습니다.

**스레드**
1. 같은 Meta 앱의 Threads API 설정 화면에서 영상용 스레드 계정으로 토큰을 생성합니다 (개발 모드면 **Threads 테스터**로 추가하고 스레드 설정에서 초대 수락).
2. PC 앱의 계정 확인에서 '스레드'를 고르고 토큰을 넣어 ID를 확인한 뒤 `VIDEO_THREADS_USER_ID`, `VIDEO_THREADS_ACCESS_TOKEN`에 넣습니다.

**유튜브**
1. PC 앱 **계정 설정 → 영상 전용 계정 → 유튜브 채널 연결**에 카드뉴스 봇에 쓰는 구글 OAuth 클라이언트(데스크톱 앱)의 ID와 보안 비밀번호를 넣고 **구글 로그인 열기**.
   (Google Cloud 콘솔 → API 및 서비스 → 사용자 인증 정보에서 확인할 수 있습니다.)
2. 브라우저에서 **영상용 채널의 구글 계정**으로 로그인하고, '채널 선택' 화면에서 영상용 채널을 고릅니다.
   OAuth 동의 화면이 아직 '테스트' 상태면 그 구글 계정을 테스트 사용자에 추가해야 합니다 ('프로덕션'이면 필요 없음).
3. 화면에 나온 refresh token은 Secrets `VIDEO_YOUTUBE_REFRESH_TOKEN`에, 채널 ID는 Variables `VIDEO_YOUTUBE_CHANNEL_ID`에 넣습니다.
   **카드뉴스의 `YOUTUBE_REFRESH_TOKEN`을 덮어쓰지 않게** 이름을 꼭 확인하세요.

구글 OAuth 클라이언트를 영상용으로 따로 만들었다면 `VIDEO_YOUTUBE_CLIENT_ID`, `VIDEO_YOUTUBE_CLIENT_SECRET`에 넣으세요 (없으면 카드뉴스 것을 씁니다).
유튜브 공개 범위는 PC 앱에서 영상마다 고릅니다.

**토큰 갱신**: 인스타·스레드 토큰은 60일마다 만료됩니다. `영상 계정 토큰 갱신` 워크플로(`video-refresh-tokens.yml`)가 매달 1일 `VIDEO_IG_ACCESS_TOKEN`, `VIDEO_THREADS_ACCESS_TOKEN`을 갱신합니다 (카드뉴스 토큰 갱신에 쓰는 `CARDBOT_ADMIN_TOKEN`을 같이 씀). 유튜브 refresh token은 OAuth 동의 화면이 '프로덕션'이면 만료되지 않습니다.

### 1-3. GitHub 토큰 만들기
1. <https://github.com/settings/personal-access-tokens/new> 에서 **fine-grained 토큰**을 만듭니다.
2. **Repository access**: *Only select repositories* → 이 저장소 하나만
3. **Permissions**: **Contents** = Read and write, **Actions** = Read and write
4. 만든 토큰을 PC 앱 **계정 설정 → 카드뉴스 봇 연결**에 넣고 **저장하고 연결 확인**.

### 1-4. 실행
1. Python 3.11 이상이 필요합니다 (<https://www.python.org/downloads/>, 설치할 때 **Add python.exe to PATH** 체크).
2. 이 폴더의 **`run.bat`** 을 더블클릭 → 처음 한 번은 설치에 몇 분 걸리고, 브라우저에 `http://localhost:8501` 이 열립니다.
3. 검은 창은 프로그램을 쓰는 동안 닫지 마세요.

### 1-5. (선택) Claude API 키 · 말소리 인식
- <https://console.anthropic.com> 에서 API 키를 만들어 **계정 설정 → Claude**에 넣으면 AI 자동 작성을 쓸 수 있습니다.
- 말하는 영상이 많다면 **`install_voice.bat`** 으로 말소리 인식을 설치하세요. 내 컴퓨터에서 처리되며 추가 비용은 없습니다.

---

## 2. 사용 방법

1. **업로드** 화면에서 영상을 끌어다 놓습니다. 길이·비율을 검사해 문제가 있으면 알려 줍니다.
2. **AI로 내용 채우기**(또는 직접 입력)로 유튜브 제목·설명, 인스타 캡션, 스레드 글, 해시태그를 채웁니다.
   영상만으로 알 수 없는 정보(장소·제품명)는 **메모** 칸에 적으면 반영됩니다.
3. **올릴 곳**과 유튜브 공개 범위를 고르고 **미리보기**로 확인한 뒤 **업로드 시작**.
4. 보통 2~5분 뒤 게시 링크가 표시됩니다. 창을 닫아도 게시는 GitHub에서 계속되며, **업로드 기록**에서 결과를 확인할 수 있습니다.

### 영상 크기 처리
- 인스타·스레드는 공개 주소에서 영상을 가져가는데, 봇이 쓰는 jsDelivr가 **20MB 넘는 파일을 거절**합니다.
  그래서 인스타·스레드용 영상은 **20MB 이하로 자동 압축**해서 올립니다 (1분 영상이면 화질 차이가 거의 없고, 2분이 넘으면 떨어질 수 있음).
- 유튜브는 **원본**을 올립니다 (GitHub 파일 한도 때문에 90MB가 넘으면 90MB 이하로 압축).

### 실패했을 때
- 업로드 기록의 **실행 기록 → Re-run jobs** 를 누르면 실패한 플랫폼만 다시 올립니다 (이미 올라간 곳은 건너뜀).
- 모든 플랫폼에 게시되면 영상 브랜치(`video/<ID>`)는 자동으로 지워지고, 실패가 있으면 다시 시도할 수 있게 남겨 둡니다.

| 증상 | 해결 |
|---|---|
| "토큰이 잘못되었거나 만료" | 1-3으로 새 GitHub 토큰을 만들어 넣기 |
| "영상 업로드 워크플로가 없습니다" | 1-1의 PR 병합 확인 |
| 인스타/스레드 "영상을 가져오지 못했습니다" | 저장소가 공개인지 확인. 영상이 너무 길면 길이를 줄이기 |
| "영상용 … Secrets가 없습니다" | 1-2의 `VIDEO_*` 값을 넣었는지, 이름이 정확한지 확인 |
| 유튜브 "채널이 달라 올리지 않았습니다" | `VIDEO_YOUTUBE_CHANNEL_ID` 변수와, refresh token을 받을 때 고른 채널이 같은지 확인 |
| 인스타/스레드 "토큰이 올바르지 않거나 만료" | 1-2로 새 토큰을 받아 `VIDEO_*_ACCESS_TOKEN`을 교체. 매달 자동 갱신은 `video-refresh-tokens.yml` |

---

## 3. 파일 구성

| 파일/폴더 | 내용 |
|---|---|
| `run.bat` / `install_voice.bat` | 실행 / 말소리 인식 설치 |
| `streamlit_app.py`, `app_pages/` | 화면 |
| `core/github_bot.py` | 봇 저장소로 영상 보내기, 워크플로 실행·결과 확인 |
| `core/accounts.py` | 영상 전용 계정 도우미 (인스타·스레드 계정 ID 확인, 유튜브 refresh token 받기) |
| `core/media.py` | 영상 정보, AI용 장면 추출, 업로드용 압축 |
| `core/analyzer.py` | Claude로 게시 문구 작성 |
| `data/` | 설정(키·토큰), 업로드 기록, 작업용 영상 — **커밋되지 않으며 공유하지 마세요** |
| `../cardnews-bot/cardbot/videopost.py` | (봇 쪽) 영상 게시 |
| `../.github/workflows/video-upload.yml` | (봇 쪽) 영상 업로드 워크플로 |
| `../.github/workflows/video-refresh-tokens.yml` | (봇 쪽) 영상 전용 인스타·스레드 토큰 매달 갱신 |
