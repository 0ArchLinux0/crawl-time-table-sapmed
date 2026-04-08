# 텔레그램 봇 사용 가이드 (Orchestrator)

`main_bot.py`는 **텔레그램에서 명령을 받아** SMU 포털 → 노션 동기화 같은 **워커를 백그라운드로 돌리는** 허브입니다.  
자동 스케줄(Windows 작업 스케줄러 등)으로 돌리던 **`run_pipeline.py`와는 별개**입니다. 둘 다 `.env`를 공유합니다.

| 구분 | 용도 |
|------|------|
| `run_pipeline.py` | 정해진 시간에 무인 실행(기존 방식) |
| `main_bot.py` | 채팅에서 `/schedule` 등으로 수동 트리거 |

---

## 1. 준비

1. **가상환경** (프로젝트 루트에서 한 번만)

   ```powershell
   cd c:\Users\J\Desktop\code_repo\sapmed-portal-crawler
   python -m venv .venv
   .\.venv\Scripts\pip.exe install -r requirements.txt
   ```

2. **`.env`**  
   `.env.example`을 복사해 `.env`로 두고 값을 채웁니다.

   ```powershell
   Copy-Item .env.example .env
   ```

3. **봇 전용 토큰**  
   `TELEGRAM_BOT_TOKEN` — `@BotFather`에서 발급한 HTTP API 토큰.  
   알림(실패·경고 푸시)과 **같은 봇**을 써도 되고, 전용 봇을 만들어도 됩니다.

4. **채팅 ID** (알림용)  
   `TELEGRAM_CHAT_ID` — 파이프라인/워커가 실패·경고를 보낼 때 사용합니다. 봇이 **반드시** 이 채팅(또는 그룹)에서 허용된 상태여야 합니다.

5. **연결 테스트** (토큰 + 채팅으로 테스트 메시지)

   ```powershell
   .\.venv\Scripts\python.exe scripts\test_telegram.py
   ```

---

## 2. 봇 실행

**항상 저장소 루트**에서 실행합니다.

```powershell
cd c:\Users\J\Desktop\code_repo\sapmed-portal-crawler
.\.venv\Scripts\python.exe main_bot.py
```

- 정상이면 콘솔에 폴링 시작 로그가 나오고, 텔레그램에서 명령에 응답합니다.
- 끄려면 `Ctrl+C`.

### 상시 실행(재부팅 후·크래시 후 재기동) — 권장

Windows **작업 스케줄러**에 등록해 두면, **로그온 시** 데몬이 올라가고 **5분마다 한 번** “아직 안 떠 있으면 시작” 트리거가 겹쳐 있습니다(이미 돌고 있으면 무시). 안쪽 루프는 `main_bot.py`가 끝나면 **약 15초 뒤 무한 재시도**합니다.

1. **일반 데스크톱**에서 PowerShell을 열고 (가능하면 프로젝트와 같은 Windows 사용자):

   ```powershell
   cd c:\Users\J\Desktop\code_repo\sapmed-portal-crawler
   .\scripts\install_bot_daemon_task.ps1
   ```

2. 바로 돌려 보기:

   ```powershell
   Start-ScheduledTask -TaskName 'SapmedTelegramBotDaemon'
   ```

3. **코드 반영 후 봇만 재시작** (예: `/commands` 추가 뒤):

   ```powershell
   .\restart-bot.cmd
   ```

   같은 내용을 PowerShell 전용으로: **`.\restart-bot.ps1`**.  
   **Windows CMD**에서는 유닉스처럼 `./restart-bot.cmd` 가 아니라 **`.\restart-bot.cmd`** (백슬래시)를 씁니다.

   프로젝트 루트의 **`restart-bot.cmd`** 를 탐색기에서 더블클릭해도 됩니다(끝에 잠깐 멈춰 로그를 볼 수 있음). 바로 종료하려면 **`restart-bot.cmd nopause`**.

4. 로그:
   - `logs/bot_daemon.log` — 재시작·프로세스 종료 코드
   - `logs/bot.log` — `main_bot.py` 로깅

**제거:**

```powershell
Unregister-ScheduledTask -TaskName 'SapmedTelegramBotDaemon' -Confirm:$false
```

**동작 방식:** `scripts\bot_daemon_loop.ps1`이 예외가 나도 빠지지 않고 계속 돌며 `main_bot.py`를 반복 실행합니다. 작업 스케줄러 **“실패 시 재시작”** 은 OS에 따라 생략될 수 있으나, **5분 주기 트리거 + 내부 루프**로 이중으로 복구합니다.

**주의:** 등록된 계정이 **로그온**해야 Interactive 작업이 돌아가는 경우가 많습니다. 로그인 화면만 두고 사용자 세션이 없을 때까지 봇을 돌리려면 NSSM 등 **Windows 서비스**로 감싸는 방식이 필요합니다.

---

## 3. 명령어

봇이 참여한 **개인 채팅 또는 그룹**에서 `/` 로 시작하는 명령을 보냅니다.

| 명령 | 설명 |
|------|------|
| `/start` | 안내 메시지(기능 목록) |
| `/help` | `/start`와 동일 |
| `/commands` | 명령 **전체 목록** (짧은 안내보다 상세) |
| `/gemini` | **Gemini** (`GEMINI_API_KEY`) |
| `/deepseek` · `/ds` | **DeepSeek** (`DEEPSEEK_API_KEY`) |
| `/g` · `/ask` | 설정된 **기본** 엔진 (`/provider`) |
| `/provider` · `/p` | 기본 엔진 `gemini` · `deepseek` |
| `/gemini_default` | 이 채팅 기본 **모드** (`fast` / `think` / `pro`, 양쪽 공통) |
| `/ping` | `pong` — 봇이 살아 있는지 확인 |
| `/schedule` | **SMU 포털 크롤 → 노션 동기화**를 **별도 프로세스**로 실행 |
| `/today` | **로컬 `schedule.json`** 기준 오늘(JST) 시간표 |
| `/tomorrow` | 내일 시간표 |
| `/syncstatus` | 마지막 성공 동기화 시각·`schedule.json` 행 수 |
| `/pack` | 노션 **준비물** 목록 · `/pack 2 4 5` 다중 토글 후 갱신 목록 응답 · `/pack clear` · `/pack setup` |

**BotFather 메뉴용 명령 한꺼번에 복사:** [`BOT_COMMANDS.md`](BOT_COMMANDS.md)

### `/pack` 준비물 ↔ 노션

- 첫 `/pack` 실행 시 기본으로 [10_MEDICAL](https://www.notion.so/10_MEDICAL-339442f6b46d4c849edb487a4db293b5) 페이지 **맨 아래**에 제목·안내 문단·to-do 9개를 붙이고, 블록 ID는 `artifacts/notion_pack_blocks.json` 에 저장합니다.
- **Integration이 해당 페이지에 초대**돼 있어야 하며 `.env`에 `NOTION_TOKEN`이 있어야 합니다 (시간표 DB와 동일한 토큰 사용 가능).
- 다른 부모 페이지를 쓰려면 `.env`에 `NOTION_PACK_PAGE_ID`를 넣으세요.
- **`PACK_RESET_ON_SCHEDULE_SYNC=1`** 이면 `run_pipeline.py` / `workers.smu_scheduler`(아침 스크래핑·노션 동기화)가 **성공한 직후** 노션 준비물 to-do **전부 해제**합니다(`/pack clear` 와 동일한 언체크만, 목록 조회는 생략). 봇 `/schedule` 으로 돌려도 동일 파이프라인이면 같이 적용됩니다.

### `/schedule` 동작 요약

- 봇 스레드를 막지 않도록 `python -m workers.smu_scheduler`를 **서브프로세스**로 띄웁니다.
- 완료 후 채팅에 **종료 코드(`exit=…`)**와 **워커 표준 출력 일부**를 붙여 줍니다. (너무 길면 잘림)
- **실패·빈 시간표·강의실 불일치 등 알림**은 워커 내부에서 기존과 같이 `TELEGRAM_CHAT_ID` 등으로 **별도 메시지**로 갈 수 있습니다. `/schedule` 답장만으로 모든 알림을 대체하지는 않습니다.

---

## 4. `.env`에서 봇·알림에 쓰는 값

| 변수 | 봇 | 알림(파이프라인/워커) |
|------|:--:|:--:|
| `TELEGRAM_BOT_TOKEN` | 필수 | (알림 전송에도 같은 봇이면 동일 토큰) |
| `TELEGRAM_CHAT_ID` | 선택* | 실패/경고 수신에 사용 |

\* 봇이 메시지를 **받을** 채팅은 텔레그램 쪽에서 봇을 시작한 대화이면 되고, **푸시를 받을** 주소는 `TELEGRAM_CHAT_ID`와 맞추는 것이 일반적입니다.

포털·노션 등 나머지 변수는 `.env.example` 주석과 동일합니다. `/schedule`을 쓰려면 스케줄 워커와 동일하게 포털·노션 설정이 필요합니다. **`/pack`은 `NOTION_TOKEN`만 있으면 동작**합니다.

### 수업 시작 N분 전 자동 알림

- **조건:** `.env`에 `SCHEDULE_CLASS_REMINDERS=1`, `TELEGRAM_CHAT_ID`(알림 받을 채팅), 그리고 **`main_bot.py` 데몬이 실행 중**일 것.
- **데이터:** 최근 `/schedule`(또는 파이프라인)으로 갱신된 **`schedule.json`** 의 `date`·`start`·`period`·`subject`·`room`(JST 기준).
- **시각:** `CLASS_REMINDER_MINUTES_BEFORE`(기본 10)분 **앞**의 약 90초 안에 한 번 울리도록, 60초마다 검사합니다. 같은 날·같은 교시는 **`artifacts/class_reminders_sent.json`** 으로 중복 전송을 막습니다.

---

## 5. 로그

| 경로 | 내용 |
|------|------|
| `logs/bot.log` | `main_bot.py`용 로깅 (`setup_bot_logging`) |
| `artifacts/logs/pipeline-*.log` | SMU 스케줄 워커(크롤·동기화) 실행 로그 |

---

## 6. 자주 묻는 것

**`TELEGRAM_BOT_TOKEN missing`**  
`.env`에 토큰이 없거나 루트가 아닌 다른 폴더에서 `main_bot.py`를 실행한 경우입니다. 루트에서 실행했는지 확인하세요.

**`/schedule`만 오래 걸림**  
브라우저 로그인·MFA·노션 API 때문에 1~3분 이상 걸릴 수 있습니다. 봇은 그동안 다른 명령(`/ping`)은 처리할 수 있습니다.

**Gemini `/gemini` 가 404**  
모델 ID가 틀리거나 API 키가 그 모델을 못 쓰는 경우입니다. 저장소 루트에서 `.\.venv\Scripts\python.exe scripts\check_models.py` 로 **이 키에 열린 모델**을 확인한 뒤, (`python scripts\...` 만 쓰면 시스템 Python에 패키지가 없어 실패할 수 있음) `.env`의 `GEMINI_MODEL_*`를 그중 하나로 맞추세요. 키는 **Google AI Studio**에서 새 프로젝트로 받은 것이 가장 수월합니다. `.env`에는 `check_models.py` 출력에 나온 짧은 이름을 쓰면 됩니다(보통 `gemini-2.0-flash` 등; `models/` 접두사는 생략 가능, 코드에서 제거함).

**알림은 오는데 봇 명령이 안 됨**  
`main_bot.py` 프로세스가 켜져 있어야 합니다. `run_pipeline.py`만 돌리면 채팅 명령은 동작하지 않습니다.

---

## 7. 코드 구조 (참고)

- `core/` — 환경 로드, 텔레그램 알림, 노션 자격증명, 공통 로깅  
- `workers/smu_scheduler.py` — 실제 SMU ↔ 노션 파이프라인 (`/schedule`이 실행하는 모듈)  
- `workers/finance_worker.py` — 예비용 스텁, 봇 명령 미연결  

새 기능은 `workers/`에 두고, `main_bot.py`에서 subprocess 또는 이후 확장 패턴으로 연결하면 됩니다.
