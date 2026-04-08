# 봇 명령 모음 (커맨드 치트시트)

텔레그램에서 `/` 를 입력하면 클라이언트가 명령을 자동완성해 줍니다.  
**메뉴에 표시**하려면 [@BotFather](https://t.me/BotFather) → 해당 봇 선택 → **Edit Bot** → **Edit Commands** 에 아래 블록을 붙여 넣으세요.

---

## BotFather에 붙여 넣을 목록 (영문 설명 = 텔레그램 메뉴용)

```
start - 도움말·명령 목록
help - start와 동일
commands - 명령 전체 목록
ping - 봇 응답 확인
schedule - SMU 포털→노션 동기화 실행
today - 오늘 시간표(로컬 schedule.json)
tomorrow - 내일 시간표(로컬 schedule.json)
syncstatus - 마지막 동기화 시각·행 개수
pack - 노션 준비물 체크리스트
gemini - Gemini 질문(AI Studio)
deepseek - DeepSeek 질문
ds - deepseek 단축
g - 기본 AI 질문(gemini|deepseek)
ask - g와 동일
provider - 기본 AI 엔진 선택
p - provider 단축
gemini_default - AI 기본 모드 fast|think|pro
gset - gemini_default 단축
```

- 한 줄에 **명령 · 공백 · 하이픈 · 공백 · 설명** 형식입니다.
- `/` 는 넣지 않습니다 (`start` 만).

---

## 명령 요약표

| 입력 | 하는 일 |
|------|--------|
| `/start` | 전체 안내 메시지 |
| `/help` | `/start` 와 동일 |
| `/commands` | **모든 명령** 한 번에 (표 형식 아님·긴 목록) |
| `/ping` | `pong` 으로 연결 확인 |
| `/schedule` | 크롤+노션 워커 실행(1~몇 분 소요 가능) |
| `/today` | **동기화로 받아 둔** `schedule.json` 기준 오늘(일본 시간) 수업 |
| `/tomorrow` | 내일 수업 |
| `/syncstatus` | `artifacts/last_sync_ok.json` + `schedule.json` 행 수 |
| `/pack` | 노션 페이지에 붙인 **준비물 to-do** 목록 |
| `/pack 3` | 3번만 체크 on/off |
| `/pack 2 4 5` | 2·4·5번 순서대로 토글 → **갱신된 전체 목록** 한 번 응답 |
| `/pack clear` | 전체 체크 해제 |
| `/pack setup` | 블록을 노션에 **다시** 생성 (중복 가능; 연동 깨졌을 때) |
| `/gemini` | 항상 **Gemini** (`GEMINI_API_KEY`) |
| `/deepseek` · `/ds` | 항상 **DeepSeek** (`DEEPSEEK_API_KEY`) |
| `/g` · `/ask` | 이 채팅 **기본 엔진**으로 질문 (`/provider` 로 gemini·deepseek 지정, 처음은 gemini) |
| `… think 질문…` · `… pro …` | 명령 뒤에 붙이면 일회 모드 (`think`: Gemini는 프롬프트·DS는 `deepseek-reasoner` 등) |
| `/provider` · `/p` | 기본 엔진 보기·`gemini` / `deepseek` 저장 |
| `/gemini_default` · `/gset` | 이 채팅 **공통** 기본 모드 `fast`·`think`·`pro` (둘 다 동일 규칙) |

**노션:** [10_MEDICAL 페이지](https://www.notion.so/10_MEDICAL-339442f6b46d4c849edb487a4db293b5) 에 Integration 초대 후 `NOTION_TOKEN`·첫 `/pack` 으로 섹션이 붙습니다. 다른 페이지로 바꾸려면 `.env` 의 `NOTION_PACK_PAGE_ID` 를 사용합니다.

---

## 참고

- `/today`, `/tomorrow`는 **최근에 성공한 `/schedule`(또는 `run_pipeline`) 결과**를 봅니다. 포털에 다시 안 붙습니다.
- 자세한 설정·로그 위치는 `BOT.md` 를 보세요.
