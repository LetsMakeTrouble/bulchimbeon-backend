# 03. 기술 스펙

## 1. 스택

| 영역 | 선택 | 비고 |
| --- | --- | --- |
| 언어/런타임 | Python 3.12 | uv로 버전 고정 |
| 패키지 관리 | **uv** | `pyproject.toml` + `uv.lock` |
| 웹 프레임워크 | FastAPI **`>=0.135.0`** | async 전면 사용. 네이티브 SSE(`fastapi.sse`) 사용을 위한 하한 핀 |
| ORM / 마이그레이션 | SQLAlchemy 2.0 (async) + asyncpg / Alembic | |
| 스키마 | Pydantic v2 | 요청/응답/LLM 구조화 출력 모두 |
| DB | PostgreSQL 18 + **pgvector 0.8.6** | 일반 데이터 + 임베딩 통합. **로컬·CI·배포를 pg18로 통일**한다 — M-1에서 Railway 매니지드 Postgres가 18.4를 주는 것이 확인됐고, 배포가 진실이므로 거기에 맞춘다 (`06 §2` ③) |
| LLM | OpenAI SDK — 생성·번역 `gpt-5-mini`(`LLM_MODEL_ANSWER`/`LLM_MODEL_TRANSLATE`), **근거 검증 `LLM_MODEL_VERIFY`(별도 env)**, 임베딩 `text-embedding-3-small`(1536차원) | 모델명은 전부 env 설정, 프로바이더 추상화로 교체 가능. 검증 모델을 생성 모델과 **분리 가능하게** 둔 것이 환각 방어 2겹의 핵심("자기평가 순환논리" 반박) |
| 실시간 | **FastAPI 네이티브 SSE** — `fastapi.sse.EventSourceResponse` | **`sse-starlette` 의존성 제거.** 네이티브가 `X-Accel-Buffering: no` 헤더와 15초 ping을 자동 처리하므로 별도 래퍼가 필요 없다 (FastAPI 0.135.0+) |
| 인증 | JWT — **`python-jose[cryptography]>=3.4.0`** + pwdlib[argon2] | access 30분 / refresh 14일. **3.4.0 미만은 CVE-2024-33663 / CVE-2024-33664** — 하한 핀 필수이며 대체 라이브러리 선택지를 두지 않는다 |
| 파일 파싱 | pypdf(PDF), python-docx(DOCX), MD/TXT 직접 | |
| 스케줄러 | APScheduler (AsyncIOScheduler) | 브리핑 배치, 72h 만료 스위퍼 |
| 외부 연동 | notion-client, GitHub REST(httpx) | 수동 동기화 |
| 테스트 | pytest + pytest-asyncio + httpx AsyncClient | LLM은 페이크 프로바이더로 대체 |
| 린트/포맷 | ruff (lint + format) | |
| 컨테이너 | docker compose (api + db) | **개발 표준은 `db`만 컨테이너**(§5.1). `api` 서비스는 배포 이미지 검증용. 클라우드 데모: Railway 또는 Render |

## 2. 아키텍처

```mermaid
flowchart LR
    FE[프론트엔드\n별도 팀] -->|REST /api/v1| API[FastAPI]
    FE -->|SSE /api/v1/sse/stream| API
    API --> SVC[서비스 레이어\nauth · project · document\nquestion · answer · review\nnotification · metrics]
    SVC --> PIPE[AI 파이프라인\n번역 → 재사용검사 → 검색\n→ 생성 → 근거검증 → 등급분기]
    PIPE --> LLM[LLM 프로바이더 추상화\nOpenAI 기본]
    SVC --> DB[(PostgreSQL\n+ pgvector)]
    SVC --> STORE[(파일 스토리지\nstorage/ 볼륨)]
    SCHED[APScheduler\n브리핑 배치 · 만료 스위퍼] --> SVC
    SYNC[동기화 워커\nNotion · GitHub] --> SVC
```

**핵심 설계 원칙**
1. **라우터는 얇게** — 검증·권한만. 비즈니스 룰은 전부 서비스 레이어.
2. **LLM 호출은 `services/llm/` 프로바이더 인터페이스 경유만** — OpenAI 직접 import 금지. "+@" 확장(다른 프로바이더 추가)은 인터페이스 구현 추가로 해결.
3. **임계값·기준값은 `projects.settings`에서 로드** — 상수 하드코딩 금지 (룰 1).
   대상: `green_threshold` `yellow_threshold` `grounding_min` **`s_floor` `s_ceil` `similarity_floor`** `reuse_threshold` `similar_threshold` `draft_expire_hours` `max_lessons` `retrieval_top_k` **`daily_llm_call_limit`** `saved_wait_assumption_hours` `briefing_hour` `dnd_start` `dnd_end` (`04 §3`).
4. **질문 파이프라인은 비동기 백그라운드 실행** — FastAPI `BackgroundTasks`(MVP 충분) → 완료 시 SSE 발행. 목표 지연은 §7 참조.

   **BackgroundTasks 규약 (위반 시 `MissingGreenlet` / 세션 조기 종료)**
   - 백그라운드 함수에는 **UUID만 전달**한다. ORM 객체·`AsyncSession`을 넘기지 않는다 — FastAPI 0.106.0부터 `yield` 의존성이 백그라운드 태스크보다 **먼저** 정리되므로 요청 세션은 태스크 실행 시점에 이미 닫혀 있다.
   - 태스크 내부에서 **자체 세션을 연다**. 실패를 기록할 때도 **또 다른 새 세션**을 쓴다(예외가 난 세션은 이미 롤백 대상이라 그 위에서 커밋할 수 없다).

   ```python
   # routers/questions.py — UUID만 넘긴다
   background_tasks.add_task(run_answer_pipeline, question_id)   # ← question_id: UUID

   # services/pipeline/answer.py
   async def run_answer_pipeline(question_id: UUID) -> None:
       try:
           async with AsyncSessionLocal() as db:          # 태스크 자체 세션
               await _pipeline(db, question_id)
               await db.commit()
       except Exception as exc:
           async with AsyncSessionLocal() as db:          # 실패 기록은 반드시 새 세션
               await mark_question_failed(db, question_id, exc)
               await db.commit()
   ```

   - **좀비 회수 잡**(APScheduler): `status='processing' AND created_at < now() - interval '5 minutes'`인 질문을 `failed`로 내리고 `review_cards(reason='failed')`를 생성한다. 프로세스가 죽어 `except`조차 못 탄 경우의 안전망 (D23).
5. **단일 프로세스 전제 — `--workers 1` 고정** (결정 1.11). SSE 구독자 큐가 **인메모리**이고 APScheduler가 **프로세스마다 중복 발화**하므로 워커를 늘리면 조용히 깨진다.
   중복 발송 방지는 **락이 아니라 제약으로** 한다: `briefing_runs(project_id, run_date)` UNIQUE + `INSERT … ON CONFLICT DO NOTHING` → `rowcount == 1`일 때만 발송 (`04 §2`).
   *확장 시점의 정답(범위 밖, 주석으로만 남긴다)*: SSE 팬아웃은 Redis Pub/Sub 또는 Postgres `LISTEN/NOTIFY`, 스케줄러는 별도 프로세스로 분리, 잡 단위 상호배제는 `pg_try_advisory_lock`.
6. **모든 상태 변화는 events 기록** — 지표·타임라인의 단일 원천.

## 3. 폴더 구조

```
bulchimbeon-api/
├── pyproject.toml            # uv 관리
├── uv.lock
├── docker-compose.yml        # api + postgres(pgvector)
├── Dockerfile
├── .env.example
├── .gitattributes            # *.md text eol=lf (content_hash 안정화, §5.2)
├── alembic/                  # 마이그레이션 (script.py.mako에 Vector import 추가, §5.4)
├── app/
│   ├── main.py               # 앱 팩토리, 라우터 등록, CORS, 예외 핸들러
│   ├── config.py             # pydantic-settings 기반 환경설정
│   ├── database.py           # async engine/session
│   ├── models/               # SQLAlchemy 모델 (04-data-model.md와 1:1)
│   ├── schemas/              # Pydantic 요청/응답 (05-api-contract.md와 1:1)
│   ├── routers/
│   │   ├── auth.py  projects.py  members.py  documents.py
│   │   ├── integrations.py  questions.py  answers.py
│   │   ├── review.py  briefing.py  official_qas.py  lessons.py
│   │   ├── notifications.py  sse.py  events.py  metrics.py
│   ├── services/
│   │   ├── llm/
│   │   │   ├── base.py       # LLMProvider 프로토콜 (complete_json, translate, embed)
│   │   │   ├── openai_provider.py
│   │   │   └── fake_provider.py   # 테스트/오프라인 데모용
│   │   ├── pipeline/
│   │   │   ├── ingest.py     # 파싱→청킹→임베딩
│   │   │   ├── answer.py     # 질문 파이프라인 오케스트레이션
│   │   │   ├── retrieval.py  # 벡터 검색 (활성 버전 청크만 — 공식 Q&A 제외, D24)
│   │   │   ├── grading.py    # S·G·매칭률·강제빨강
│   │   │   └── translate.py
│   │   ├── auth_service.py  project_service.py  document_service.py
│   │   ├── question_service.py  review_service.py  official_qa_service.py
│   │   ├── lesson_service.py  notification_service.py  sse_manager.py
│   │   ├── briefing_service.py  metrics_service.py  event_service.py
│   │   └── sync/notion_sync.py  sync/github_sync.py
│   ├── core/
│   │   ├── security.py       # JWT 발급/검증, 해시
│   │   ├── deps.py           # get_current_user, require_role
│   │   ├── errors.py         # 에러 코드 체계
│   │   └── scheduler.py      # APScheduler 잡 등록
│   └── utils/
├── scripts/
│   ├── probe_calibration.py  # M-1 게이트: s_floor/s_ceil/reuse_threshold 실측
│   └── seed.py               # 08-demo-scenario.md 시드 주입
├── seed/                     # 데모용 영어 문서 파일
└── tests/
    ├── conftest.py           # TEST_DATABASE_URL 세션 픽스처 + 트랜잭션 롤백 격리, FakeLLM (§5.3)
    ├── test_auth.py  test_projects.py  test_documents.py
    ├── test_pipeline.py  test_grading.py  test_review.py
    ├── test_reuse.py  test_notifications.py  test_metrics.py
```

## 4. 환경변수 (.env.example)

```env
# App
APP_ENV=local                      # local | demo
API_BASE_URL=http://localhost:8000
CORS_ORIGINS=http://localhost:3000
SECRET_KEY=change-me
ACCESS_TOKEN_EXPIRE_MINUTES=30
REFRESH_TOKEN_EXPIRE_DAYS=14

# DB
DATABASE_URL=postgresql+asyncpg://bulchimbeon:bulchimbeon@localhost:5432/bulchimbeon
TEST_DATABASE_URL=postgresql+asyncpg://bulchimbeon:bulchimbeon@localhost:5432/bulchimbeon_test

# LLM
LLM_PROVIDER=openai                 # openai | fake
OPENAI_API_KEY=sk-...
LLM_MODEL_ANSWER=gpt-5-mini         # ④ 답변 생성
LLM_MODEL_VERIFY=gpt-5-mini         # ⑤ 근거 검증 전용 — 생성과 분리 가능하게 별도 env
LLM_MODEL_TRANSLATE=gpt-5-mini      # ① 번역 · ⑦ 구조화
LLM_REASONING_EFFORT=minimal        # M-1 실측: 지원됨(400 아님). 지연 예산 달성의 필수 조건
LLM_TIMEOUT_SECONDS=45              # 단건 호출 타임아웃 (M-1 실측 p90 8.09s 대비 충분)
LLM_PIPELINE_DEADLINE_SECONDS=25    # 🟢/🟡 경로 데드라인 (3회 호출, M-1 실측 p90 24.3s)
LLM_PIPELINE_DEADLINE_RED_SECONDS=35  # 🔴 경로 데드라인 (4회 호출 — ⑦구조화 추가, M-1 실측 p90 32.4s)
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIM=1536                  # 응답 차원 검증 + dimensions 파라미터용. 벡터 컬럼은 vector(1536) 리터럴 고정

# Storage
STORAGE_DIR=/home/kancth03/bulchimbeon/bulchimbeon-api/storage    # 절대 경로 필수 (호스트/컨테이너 혼선 방지)

# Integrations (프로젝트별 토큰은 DB 암호화 저장, 여긴 암호화 키만)
INTEGRATION_ENCRYPTION_KEY=change-me-32bytes
```

- **`daily_llm_call_limit`은 env에 두지 않는다** — `projects.settings`로 이동했다(룰 1 일관성, `04 §3`). 초과 시 강제 🔴 `quota_exceeded`.
- **금지 파라미터**: `temperature` / `top_p` / `presence_penalty` / `frequency_penalty` / `seed` — GPT-5 계열에 전달하면 400. 프로바이더에서 화이트리스트로 강제한다. `max_tokens` 대신 **`max_completion_tokens`**.
- **`EMBEDDING_DIM`은 컬럼 차원을 바꾸는 스위치가 아니다.** 기동 시 `EMBEDDING_DIM != 1536`이면 **fail-fast** (`04` 문서 상단).
- `STORAGE_DIR`은 **절대 경로**로 둔다. 상대 경로는 uvicorn 실행 위치·컨테이너 WORKDIR에 따라 다른 디렉터리를 가리켜 "업로드는 됐는데 파일이 없다"를 만든다.

## 5. 실행 방법

### 5.1 개발 표준 — DB만 컨테이너, 앱은 호스트

```bash
uv sync
docker compose up -d db                                  # DB만 기동
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --workers 1 --port 8000

# 테스트 / 린트
uv run pytest
uv run ruff check . && uv run ruff format --check .

# 시드 데이터
uv run python scripts/seed.py
```

- Swagger UI: `http://localhost:8000/docs` — 프론트 팀에 URL 공유 (05-api-contract.md와 함께 전달)
- 헬스체크: `GET /health` → `{"status":"ok","db":"ok"}`
- `docker compose up --build`(api까지 컨테이너로)는 **배포 이미지 검증용**이며 개발 루프에서는 쓰지 않는다 — §5.2 참조.

### 5.2 ⚠️ 실행 환경 주의

> - **`STORAGE_DIR`는 절대 경로**로 지정한다(예: `/home/kancth03/bulchimbeon/bulchimbeon-api/storage`). 상대 경로는 uvicorn 실행 위치·컨테이너 WORKDIR에 따라 다른 디렉터리를 가리킨다.
> - **호스트 실행 모드와 컨테이너 실행 모드를 섞지 않는다.** 두 모드는 `DATABASE_URL` 호스트명(`localhost` vs `db`)과 `STORAGE_DIR` 경로가 서로 다르다.
>   섞으면 "마이그레이션은 됐는데 앱이 빈 DB를 본다" / "업로드 파일이 사라진다"가 난다.
>   **개발 표준은 §5.1 하나로 고정**한다: `docker compose up -d db` + 호스트 uvicorn.
> - **`.gitattributes`에 `*.md text eol=lf`** 를 넣는다. CRLF가 섞이면 시드 문서의 `content_hash`가 OS마다 달라져 교훈 중복 차단(D8)이 무력화된다.
> - **`content_hash` 정규화 정의(명문화)**: `sha256(lesson.strip().lower().replace("\r\n", "\n"))` + **연속 공백 단일화**. 이 정의를 벗어난 해시 계산을 만들지 않는다.

### 5.3 테스트 인프라 (확정)

- **testcontainers는 도입하지 않는다.** Windows Docker 환경에서 기동 지연·권한 문제로 1주 일정에 부담이다.
- 테스트는 **이미 떠 있는 Postgres에 붙는다** — `TEST_DATABASE_URL`(`…/bulchimbeon_test`). 개발 표준의 `docker compose up -d db` 컨테이너를 그대로 재사용한다.
- 세션 스코프 픽스처에서 `CREATE EXTENSION IF NOT EXISTS vector` → `Base.metadata.create_all`.
- **테스트별 격리는 트랜잭션 롤백**으로 한다(테스트마다 DROP/CREATE 금지 — 느리고 HNSW 인덱스 재생성 비용이 크다).
- CI는 GitHub Actions `services: pgvector/pgvector:pg18`만 사용한다.
- **마이그레이션 검증 테스트 1개만** `@pytest.mark.slow`로 분리한다(`alembic upgrade head`가 빈 DB에서 통과하는지).

### 5.4 ⚠️ Alembic + pgvector 함정 3종

1. **최초 마이그레이션의 첫 줄**에 `op.execute("CREATE EXTENSION IF NOT EXISTS vector")`. 없으면 `vector` 타입 컬럼 생성에서 `UndefinedObject`로 죽는다.
2. **`alembic/script.py.mako`에 pgvector import 추가**. autogenerate가 만든 리비전 파일은 벡터 컬럼을 뱉지만 import는 넣어주지 않아 실행 시 `NameError`가 난다.
   > ⚠️ **M2 실측(2026-08-08)**: autogenerate가 실제로 뱉는 것은 `Vector(1536)`이 아니라 완전 경로 **`pgvector.sqlalchemy.vector.VECTOR(dim=1536)`**이다. `from pgvector.sqlalchemy import Vector`는 `Vector`만 바인딩하므로 이것만으로는 `NameError: pgvector`를 막지 못한다. 템플릿에 **`import pgvector.sqlalchemy`를 함께** 넣고, 리비전은 `Vector(1536)`로 손질한다.
3. **`register_vector`는 raw asyncpg 전용이다. SQLAlchemy 경로에서는 등록하지 않는다.**
   > ⚠️ **M2 실측(2026-08-08, 로컬 pg18.4 / pgvector 0.8.6)이 기존 문구를 뒤집었다.** 원래 이 항목은 "`database.py`의 asyncpg 커넥션 init에서 `register_vector`를 호출하라"였으나, 그렇게 하면 **SQLAlchemy 경유 모든 벡터 바인딩이 죽는다**:
   > `asyncpg.exceptions.DataError: invalid input for query argument $1: '[1.0, 0.0, ...]' (expected list or ndarray)`
   >
   > ORM INSERT뿐 아니라 `SELECT (:a)::vector <=> (:b)::vector` 같은 단순 캐스트도 함께 죽는다. 원인은 **이중 변환 충돌**이다 — `pgvector.sqlalchemy.Vector`의 bind processor가 이미 리스트를 `'[...]'` 문자열로 만들어 보내는데, 등록된 asyncpg 코덱은 리스트를 기대한다.
   >
   > 코덱 없이 붙으면 문자열이 그대로 Postgres의 vector 파서로 들어가 정상 동작하며, `<=>`가 **거리**라는 성질도 그대로다(직교 벡터 = 1.0). M1이 초록이었던 것은 벡터를 한 번도 바인딩하지 않았기 때문이다.
   >
   > **raw asyncpg로 직접 쿼리하는 스크립트를 만든다면 그쪽에서는 `register_vector`가 필요하다.** 구분 기준은 "누가 파라미터를 인코딩하는가"다.

## 6. 클라우드 데모 배포 (Railway 기준)

1. GitHub 레포 연결 → Railway 프로젝트 생성
2. PostgreSQL 추가 → `CREATE EXTENSION vector;` 실행
   > ✅ **M-1에서 검증 완료 (2026-08-07).** Railway 매니지드 Postgres는 `CREATE EXTENSION vector` **권한을 준다.**
   > 실측: **pgvector 0.8.6 / PostgreSQL 18.4**, `hnsw.iterative_scan='relaxed_order'` 설정 가능,
   > `vector(1536)` 컬럼 + HNSW 인덱스 생성 가능. 재검증은 `bash scripts/probe_deploy_db.sh`.
   >
   > ⚠️ **`DATABASE_PUBLIC_URL`은 기본으로 없다.** Postgres 서비스 → **Settings → Networking → TCP Proxy**(포트 5432)를
   > 켜야 생성된다. 기본 `DATABASE_URL`은 내부망(`postgres.railway.internal`) 전용이라 외부에서 붙지 않는다.
   >
   > 권한이 없는 제공자로 갈아탈 경우: **pgvector 지원 이미지(`pgvector/pgvector:pg18`)를 직접 띄우거나 pgvector 애드온이 있는 DB**로 간다.
   > 어느 경우든 `SELECT extversion FROM pg_extension WHERE extname='vector'`로 **0.8.0 이상**을 확인한다(`hnsw.iterative_scan` 필요, `04 §7`).
3. 환경변수 등록 (§4 전체, `APP_ENV=demo`). `STORAGE_DIR`는 컨테이너 내 **절대 경로**로.
4. 시작 커맨드: **`uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`** (배포 시 `alembic upgrade head` 선행)
   - **`--workers 1`은 선택이 아니다.** SSE 구독자 큐가 인메모리라 워커가 2개면 다른 워커에 붙은 클라이언트에게 이벤트가 가지 않고, APScheduler가 워커 수만큼 중복 발화한다 (원칙 5).
   - 그럼에도 재시작·수동 트리거로 브리핑이 두 번 나갈 수 있으므로, 발송은 `briefing_runs`의 **`UNIQUE(project_id, run_date)` + `INSERT … ON CONFLICT DO NOTHING`** 을 거쳐 `rowcount == 1`일 때만 수행한다 (`04 §2`).
5. 배포 후 `scripts/seed.py` 1회 실행 → 데모 계정·데이터 준비
6. Render 사용 시 동일 구성 (render.yaml 선택) — pgvector 권한 확인은 Render에서도 동일하게 선행한다.
7. **SSE는 프록시에서 강제 종료된다**: Railway 기준 **15분**(하트비트가 없으면 5분). 재연결은 예외가 아니라 **필연**이므로,
   프론트는 재연결 시 구독 리소스를 전량 재조회하고 서버는 스트림 시작 시 미읽음 수를 1회 push한다 (`05 §12`).

## 7. 보안·품질 기준

- 비밀번호 argon2 해시. JWT `sub`=user_id, 만료 검증 필수.
- 프로젝트 리소스 접근 시 **멤버십 + 역할 검증**을 서비스 레이어에서 강제 (403).
- Notion/GitHub 토큰은 `INTEGRATION_ENCRYPTION_KEY`로 대칭 암호화(Fernet) 후 저장. 로그에 토큰·문서 본문 출력 금지.
- 업로드 제한: 파일당 20MB, 프로젝트당 문서 100개 (설정화).
- **SSE 인증 — 단명 티켓 방식**: EventSource가 헤더를 못 실으므로 `POST /sse/ticket`(Authorization 헤더 필요)으로 **TTL 60초·1회용 티켓**을 받아 `GET /sse/stream?ticket=…`로 접속한다.
  **access token을 URL에 싣지 않는다** — 쿼리 문자열은 프록시 로그·리퍼러·브라우저 히스토리에 남아 30분짜리 자격증명이 유출된다. 티켓은 검증 즉시 폐기한다.
  access token 만료 시 서버가 스트림을 종료하고, 프론트는 refresh 후 **새 티켓**으로 재연결한다.
- 에러 응답 포맷 통일: `{"error": {"code": "...", "message": "..."}}` — 코드 체계는 05-api-contract.md §1.4.
- 성능 목표: 질문 접수 202 응답 < 500ms. **파이프라인 완료 지연은 M-1 캘리브레이션으로 확정**됐다(2026-08-07, n=8) — 🟢/🟡 경로 **25초**, 🔴 경로 **35초**. 산출 근거와 호출 방식 고정(`responses.parse`)은 `06 §0`.
  데드라인 초과 시에는 그 시점까지의 결과로 🟡 발행 + 카드 생성(안전망)으로 빠진다.
