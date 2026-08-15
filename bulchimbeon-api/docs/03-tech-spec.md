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
| LLM | OpenAI SDK — 단계별 모델 배정 7슬롯 (**§4.1**). 검증만 `gpt-5.6-sol`, 나머지는 `terra`/`luna`. **근거 검증 `LLM_MODEL_VERIFY`(별도 env)**, 임베딩 `text-embedding-3-small`(1536차원) | 모델명은 전부 env 설정, 프로바이더 추상화로 교체 가능. 검증 모델을 생성 모델과 **분리 가능하게** 둔 것이 환각 방어 2겹의 핵심("자기평가 순환논리" 반박) |
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
LLM_MODEL_ANSWER=gpt-5.6-terra           # ④ 답변 생성
LLM_MODEL_VERIFY=gpt-5.6-sol             # ⑤ 근거 검증
LLM_MODEL_REUSE_GATE=gpt-5.6-terra       # 재사용 판정 "같은 질문인가?" (§4.1.2)
LLM_MODEL_STRUCT=gpt-5.6-terra           # ⑦ 카드 구조화
LLM_MODEL_TRANSLATE=gpt-5.6-terra        # ① 질문 ko→en — ⛔ luna 금지 (§4.1.3)
LLM_MODEL_LESSON=gpt-5.6-luna            # 교훈 추출 (배치)
LLM_MODEL_ANSWER_TRANSLATE=gpt-5.6-terra # 담당자 확정문 en→ko (⚠️ 아래 §4.1)
LLM_REASONING_EFFORT=low            # ⚠️ minimal 은 gpt-5.6 계열에서 400 (§4.1)
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

### 4.1 단계별 모델 배정 (사용자 결정 2026-08-09)

**배정 기준은 "그 단계가 중요한가"가 아니라 "틀렸을 때 뒤에서 잡아 주는가"다.**

| 단계 | env | 모델 | 틀렸을 때 |
| --- | --- | --- | --- |
| ⑤ 근거 검증 | `LLM_MODEL_VERIFY` | **sol** | ⛔ **환각이 🟢로 발행된다.** 뒤에 잡아 줄 단계가 없다 |
| 재사용 판정 | `LLM_MODEL_REUSE_GATE` | terra | 실측상 sol 과 동일 (§4.1.2) |
| ④ 답변 생성 | `LLM_MODEL_ANSWER` | terra | ⑤가 🟡/🔴로 내려 준다 |
| ⑦ 카드 구조화 | `LLM_MODEL_STRUCT` | terra | 담당자가 읽고 판단하므로 사람이 보정한다 |
| 확정문 en→ko | `LLM_MODEL_ANSWER_TRANSLATE` | terra | ⛔ 아래 참조 |
| ① 질문 ko→en | `LLM_MODEL_TRANSLATE` | terra | **결정성** 때문이다 (§4.1.3) |
| 교훈 추출 | `LLM_MODEL_LESSON` | luna | 배치이고 담당자 승인을 거친다 |

### 4.1.1 ⑤ 근거 검증 실측 — sol 을 쓰는 근거 (2026-08-09)

운영 프롬프트(`VERIFY_SYSTEM` + `verify_user_prompt`)와 스키마(`VerdictsOut`)를 그대로 써서,
시드 문서 근거에 **환각 10종 + 정당한 패러프레이즈 8종**을 섞어 각 모델 3회씩 판정시켰다.

| 모델 | 환각 검출률 | 오탐률 | 지연 p50 | 출력 토큰 |
| --- | --- | --- | --- | --- |
| `gpt-5-mini` (종전) | **53.3%** | 0% | 3245ms | 174 |
| `gpt-5.6-luna` | 76.7% | 0% | 1607ms | 160 |
| `gpt-5.6-terra` | 90.0% | 0% | 1867ms | 160 |
| **`gpt-5.6-sol`** | **100%** | 0% | 4605ms | 298 |

⚠️ **숫자 변조·사실 반전 같은 쉬운 환각은 네 모델 모두 100% 잡는다.** 차이는 아래 두
유형에서만 난다 — 그래서 "어느 모델이든 된다"는 결론이 나오기 쉽다:

- **부분 환각**: 한 문장 안에 맞는 절 + 지어낸 절 (`"매주 일요일 초기화되므로, 테스트
  데이터를 영구 보관하면 안 된다"` — 앞은 근거에 있고 뒤는 없다). sol 만 잡았다.
- **추론 비약**: 근거에서 한 걸음 더 나감 (`"24시간 보관되므로 25시간 뒤 재요청은
  중복 주문을 만든다"`). terra 이상만 잡았다.

종전 `gpt-5-mini` 는 이 구간에서 **절반을 통과시켰다.** 통과한 문장은 G 를 유지시켜
`min(S,G)` 가 높게 남고, 결국 **🟢 즉답으로 발행된다** — "모르면 지어내지 않는다"가
뚫리는 지점이다. 비용(출력 토큰 1.9배 × 단가 2배)을 감수하고 sol 을 두는 이유다.

⚠️ 표본은 30 관측(10종 × 3회)이고 케이스는 손으로 만든 것이다. 순위는 뚜렷하지만
절대 수치를 과신하지 말 것.

### 4.1.2 재사용 판정 실측 — sol 이 **필요 없다** (2026-08-09)

`SAME_QUESTION_SYSTEM` + `SameQuestionOut` 으로 10쌍(같음 3 / 다름 7)을 각 3회.
다름 쌍에는 *"멱등키 보관 기간"* vs *"액세스 토큰 유효기간"*(둘 다 답이 24시간인 함정)
같은 것을 넣었다.

| 모델 | 정답률 | **틀린 재사용** | 놓침 |
| --- | --- | --- | --- |
| `gpt-5-mini` | 100% | **0** | 0 |
| `gpt-5.6-luna` | 90% | **0** | 3 |
| `gpt-5.6-terra` | 100% | **0** | 0 |
| `gpt-5.6-sol` | 100% | **0** | 0 |

**가장 위험한 오답인 "틀린 재사용"은 네 모델 모두 0건**이다. 앞에 `reuse_threshold`(0.925)
라는 1차 필터가 있어 어려운 쌍이 애초에 도달하지 않는다 — 그래서 terra 로 내렸다.

⛔ **luna 로는 내리지 마라.** 오답이 전부 "놓침"이라 안전한 방향이긴 하나, 그중 하나가
*"How long is a token good for?"* ≡ *"What is the access token TTL?"* 였다.
게이트가 luna 면 **데모 5단계(Q10 재사용)가 안 터질 수 있다.**

### 4.1.3 ① 질문 번역 실측 — terra 를 쓰는 이유는 품질이 아니라 **결정성** (2026-08-09)

번역이 바뀌면 **영어 문장 → 임베딩 → `sim_raw`** 가 함께 움직인다. 같은 한국어 질문을
5회 번역해 배포 DB 청크에 대한 top-1 유사도를 쟀다.

**Q8** (`환불 정책이 일본 리전에도 동일하게 적용되나요?` — 강제 🔴 이어야 한다):

| 모델 | sim 범위 | 서로 다른 번역문 | 구 차단선(0.423) |
| --- | --- | --- | --- |
| `gpt-5-mini` | 0.4236 ~ 0.4240 | 2종 | ⛔ 넘김 |
| `gpt-5.6-luna` | 0.4190 ~ **0.4258** | **4종** | ⛔ 넘김 |
| `gpt-5.6-terra` | 0.4074 ~ 0.4112 | 2종 | 안전 |
| `gpt-5.6-sol` | 0.4190 ~ 0.4233 | 2종 | ⛔ 넘김 |

`luna` 는 5회에 **4종**의 서로 다른 문장을 만든다(terra 는 1~2종). 문장이 흔들리면
유사도가 흔들리고, 유사도가 차단선 근처면 **같은 질문의 등급이 실행마다 달라진다.**
데모는 같은 질문에 같은 결과가 나와야 하므로 여기서는 결정성이 비용보다 비싸다.

⚠️ 차이를 만든 것은 `as well` 한 마디였다 — terra 의 `"...to the Japan region as well?"`
이 luna 의 `"...to the Japan region?"` 보다 시드 코퍼스와 덜 닮았다. **번역 모델 교체는
검색 결과를 바꾸는 변경이다.** 바꾸면 `eval_questions.py` 로 재확인하라.

⚠️ **가장 강한 모델은 답변 생성이 아니라 검증에 쓴다.** 직관과 반대지만,
답변은 실수해도 ⑤가 잡고 ⑤가 실수하면 잡을 것이 없다 — 환각 방어 2겹의 마지막 겹이다.

### 4.1.4 나머지 네 자리 실측 (2026-08-09)

`scripts/compare_models.py` 로 잰다. 앞의 세 절이 다루지 않은 자리들이고,
**"다 괜찮다"도 근거로 기록한다** — 차이가 없다는 사실이 비용 조정의 판단 재료다.

**⑧ 담당자 확정문 en→ko — 세 모델 동점.** 확정문 8개에서 번역하면 안 되는 토큰
(`30`·`KRW/USD/JPY`·`409`·`Idempotency-Key`·`HMAC-SHA256`·`3600`·`UTC`)의 잔존과
부정문 4개의 부정 유지, 합계 17개 항목을 셌다.

| 모델 | 보존 | 보존률 |
| --- | --- | --- |
| `gpt-5.6-sol` | 17 / 17 | 100% |
| `gpt-5.6-terra` | 17 / 17 | 100% |
| `gpt-5.6-luna` | 17 / 17 | 100% |

⚠️ **동점이지만 luna 로 내리지 않는다.** 이 자리는 오역을 잡아 줄 사람이 경로에 없고
(담당자는 영어로 쓰고 질문자는 한국어를 읽는다) 결과가 룰 4 로 **공식 Q&A 에 굳는다**.
표본 17개로 만점이 나왔다는 것은 "차이가 없다"가 아니라 "이 표본에서 차이가 안 잡혔다"이며,
실패 비용이 비대칭인 자리에서는 그 근거로 등급을 내리기에 부족하다.

**④ 답변 생성 — luna 만 함정 1건에서 무너진다.** 같은 근거·같은 질문을 주고 ⑤(sol 고정)로
채점했다. 정상 질문 3건 + 함정 3건이며, 함정은 **주제는 문서에 있는데 물어본 사실만 없는**
것들이다(초당 레이트 리밋 / 리프레시 토큰 수명 / 연차).

| 모델 | 평균 G | 없는 청크 id 인용 | 함정 회피 |
| --- | --- | --- | --- |
| `gpt-5.6-sol` | 100 | 0 | 3 / 3 |
| `gpt-5.6-terra` | 100 | 0 | 3 / 3 |
| `gpt-5.6-luna` | 100 | 0 | **2 / 3** |

luna 가 놓친 것은 "초당 레이트 리밋"이다. 문서에는 **분당 60회**만 있는데 luna 는 이를
**초당 1회로 환산해** 답을 만들었다. 단위 환산이라 악의는 없지만 근거에 없는 값을 낸
것이고, ④ 시스템 프롬프트의 *"주제만 언급한 청크는 근거가 아니다"* 를 넘어선 추론이다.
sol·terra 는 둘 다 `not_answerable=true` 로 물러섰다. **현재 배정(terra)이 실측으로
뒷받침된다** — 이 자리에서 sol 을 쓸 이유는 없다.

**⑦ 카드 구조화 · 교훈 추출 — 숫자를 만들지 않는다.** 정답이 없는 산출물이라 억지 지표
대신 같은 입력에 대한 세 산출물을 나란히 두고 판단했다(`compare_models.py struct|lesson`).
세 모델 모두 형식을 지켰고 배경 요약·선택지 4개·교훈 한 문장이 모두 쓸 만했다.
차이는 품질이 아니라 **말투와 구체성**이었다.

- 구조화: terra 의 선택지가 가장 행동 지향적이었다(*"regional/legal team 에 확인"* 처럼
  다음 행동이 붙는다). sol 은 가장 간결하고 luna 는 가장 서술적이다. **terra 유지.**
- 교훈: luna 의 문장이 가장 짧고 규칙처럼 읽힌다(*"결함품이면 표준 반품 기한을 적용하지
  말고 보증 청구로 처리한다"*). 교훈은 프롬프트에 누적되므로 짧은 편이 낫다. **luna 유지.**

> 요약: 이번에 잰 네 자리는 **전부 현재 배정을 바꿀 이유가 없었다.** 바꿀 이유가 없다는
> 확인도 측정의 결과다 — 다음에 비용을 줄여야 할 때 어디를 건드리면 안 되는지가 남는다.

⛔ **`LLM_MODEL_ANSWER_TRANSLATE` 를 `LLM_MODEL_TRANSLATE` 와 합치지 마라.**
전자는 `review_card_service` 의 담당자 확정문 en→ko 경로이고, 그 결과가 **질문자가 읽는
확정 답변**이자 룰 4(확정 ko 원문 재번역 금지)로 공식 Q&A 에 그대로 굳는다.
담당자는 영어로 쓰고 질문자는 한국어를 읽으므로 **오역을 잡아 줄 사람이 경로에 없다.**
후자는 검색용 질문 번역이라 실패해도 등급만 떨어진다. 성격이 정반대다.

⚠️ **`LLM_REASONING_EFFORT=minimal` 은 gpt-5.6 계열에서 400 이다** (2026-08-09 실측).
`minimal` 은 gpt-5-mini 전용이었다. 5.6 의 최소값은 `low` 이고, gpt-5-mini 도 `low` 를
받으므로 두 계열 혼용이 이 값 하나로 된다. 실측 지연(근거 6청크 기준 p50):
terra 1.19s · luna 1.35s · sol 1.73s · gpt-5-mini(minimal) 1.78s —
**terra 가 종전 모델보다 빠르다.** 3~4회 호출을 곱해도 데드라인(25s/35s)에 여유가 크다.

⚠️ **모델을 바꾸면 등급 분포가 이동한다.** ⑤가 G(근거 점수)를 만들고 매칭률은 `min(S,G)`
이므로, 검증 모델 교체는 🟢/🟡 경계를 직접 흔든다. 교체 후에는 반드시
`scripts/eval_questions.py` 로 12건을 대조하고, 필요하면 이력을 다시 채운다.
**발표 직전에 할 작업이 아니다.**

⛔ **`EMBEDDING_MODEL` 은 이 논의에서 제외한다.** 임베딩을 바꾸면 전체 재인제스트에 더해
**임계값 5종을 전부 재측정**해야 한다 (`09 §7.2` — 제목 줄 한 줄 차이로 Q8 이 🔴↔🟡 로
뒤집힌 사례가 있다).

### 4.2 호출 수·토큰·단가 실측 (2026-08-09, 배포본)

**한도(호출 수).** 일일 상한은 토큰이 아니라 **호출 수**로 센다(`pipeline/quota.py`).
소비 지점은 `AnswerContext.call_json` 한 곳뿐이므로, `question.graded` 이벤트의 `steps`
키 개수를 더하면 정확한 소비량이 나온다. 데모 시드 1회(질문 123건·답변 126건) 실측:

| 단계 | 호출 수 | 비고 |
|---|---|---|
| ① 질문 ko→en | 126 | 전 질문 공통 |
| ④ 답변 생성 | 108 | 근거를 못 찾은 18건은 생성 없이 🔴 |
| ⑤ 근거 검증 | 104 | 인용이 하나도 없으면 ⑤ 를 건너뛴다 |
| ⑦ 카드 구조화 | 32 | 🔴 에서만 |
| **합계** | **370 / 500 (74%)** | 질문 1건당 평균 **2.94회** |

- **여유 130회 ≈ 질문 43건**(3회/건 기준). 지금 123건 → **최대 약 166건**까지 안전하다.
- 시드는 자체 프로세스에서 파이프라인을 부르므로 **이 소비는 API 서버 카운터와 별개**다.
  발표 중 라이브 질문은 서버 쪽 500회를 온전히 쓴다.
- **한도를 안 쓰는 LLM 호출이 따로 있다**: 담당자 확정문 en→ko(45회)와 교훈 추출(13회).
  `provider.complete_json` 을 직접 부르는 경로라 `quota.consume` 을 지나지 않는다.
  의도된 설계(`lesson_service` 독스트링 — 담당자 저장이 한도에 막히면 안 된다)지만
  **비용은 발생한다.**

**단가(토큰).** `scripts/measure_tokens.py` 로 실측한다. 데모 문서를 복사한 별도
프로젝트에서 돌려 데모를 건드리지 않는다.

| 등급 경로 | 호출 | 입력 토큰 | 출력 토큰 | 질문 1건 단가 |
|---|---|---|---|---|
| 🟢 | ①④⑤ | 약 2,450 | 약 330 | **$0.0138** |
| 🟡 | ①④⑤ | 약 2,200 | 약 200 | **$0.0104** |
| 🔴 | ①⑦ | 약 1,100 | 약 165 | **$0.0052** |

질문당 평균 **$0.0098**, 시드 123건 환산 **약 $1.21**(확정·교훈 제외, 실제로는 $1.3 안팎).
임베딩은 질문당 10~15토큰으로 반올림하면 0 이다. 추론 토큰은 `low` 에서 대부분 0 이고,
발생해도 `output_tokens` 에 **포함**된 값이라 따로 더하면 이중 계상이다.

> 단가 산정용 관측일 뿐 회계가 아니다. 상한과 엮지 않는다 — 엮는 순간 상한의 성격이
> "폭주 방어"에서 "과금 통제"로 바뀌어 룰 3 의 임계값 정의와 어긋난다.

### 4.3 동시성 상한 (2026-08-11, 운영 전환 항목 A)

**`--workers 1` 은 그대로다.** 바꾼 것은 한 프로세스 안에서 동시에 도는 파이프라인 수다.

| env | 기본값 | 의미 |
| --- | --- | --- |
| `PIPELINE_MAX_CONCURRENCY` | **8** | 동시에 도는 답변 파이프라인 수. 초과분은 **대기**(실패 아님) |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | **10 / 10** | 커넥션 풀. 합 20 중 8 은 파이프라인, 12 는 요청 처리 |

> ### ⛔ 무엇이 문제였나 — 15건에서 **접수까지** 막혔다
> `database.py` 가 풀 크기를 지정하지 않아 SQLAlchemy 기본값 **5+10=15** 가 실질 상한이었다.
> 파이프라인은 DB 세션을 **LLM 3~4회를 도는 내내**(실측 5~15초) 붙잡으므로, 동시 질문
> 15건이면 풀이 비고 **새 질문 접수(`get_db`)가 같은 풀을 쓰기 때문에 접수 자체가 멈춘다.**
> 사용자에게는 "느려졌다"가 아니라 "먹통"으로 보인다.

> ### 왜 풀만 키우지 않았나
> 풀을 키우면 DB 는 버티지만 OpenAI 동시 호출이 무제한이 된다. 조직 rate limit(429)은
> `LLMProviderError` → **파이프라인 총 실패(D23)** 라서 질문이 통째로 `failed` 가 된다.
> 세마포어 하나가 두 자원을 함께 묶는다 (`services/pipeline/concurrency.py`).

> ### ⚠️ 순서가 사양이다 — **슬롯 → 세션 → 데드라인 시계**
> 세션을 먼저 열면 대기 중인 질문이 커넥션을 쥔 채 줄을 서서 상한이 무의미해진다.
> 시계를 먼저 켜면 **줄을 섰다는 이유로** 데드라인(🟢🟡 25초)을 넘겨 멀쩡한 답변이 🟡 로
> 강등된다. `tests/test_concurrency.py` 가 이 순서를 고정한다.

부하 재현은 `scripts/probe_concurrency.py` 가 실제 서버에 대고 한다 — 테스트 스위트는
커넥션 하나를 공유하므로(`03 §5.3`) 풀 고갈이 성립하지 않는다.

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

- Swagger UI: `http://localhost:8000/api/docs` — 프론트 팀에 URL 공유 (05-api-contract.md와 함께 전달)
- 헬스체크: `GET /api/health` → `{"status":"ok","db":"ok"}`
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

## 9. 운영 DB (2026-08-12)

### 9.1 요구 사항 — 타협 불가 두 가지

| 항목 | 값 | 왜 |
| --- | --- | --- |
| PostgreSQL | **18** | 로컬·CI·배포를 하나로 통일했다 (`03 §1`). 15~17 도 동작하지만 검증 범위 밖이다 |
| pgvector | **0.8.0 이상** | HNSW `iterative_scan` 이 여기서 들어왔다 |

> ### ⛔ pgvector 0.7 대에서는 **조용히** 깨진다
> HNSW 는 인덱스 스캔 **이후** `WHERE` 를 적용한다(post-filtering). 활성 버전 필터가 걸리면
> top-k 가 0건이 될 수 있고, `iterative_scan` 이 그 보정 수단이다. 없으면 **인덱스는
> 정상적으로 만들어지고 검색만 0건**이 된다 — 에러가 안 나므로 배포는 성공한 것처럼 보이고
> 모든 질문이 근거 없음(🔴)으로 떨어진다. `sql/schema.sql` 이 이걸 직접 막는다.

그 밖에 필요한 것은 표준 기능뿐이다: `gen_random_uuid()`(PG13+ 내장) · `clock_timestamp()` ·
`jsonb` · 부분 인덱스. 확장은 `vector` 하나다.

### 9.2 스키마 올리기

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/schema.sql
```

- 전체가 **한 트랜잭션**이라 중간에 실패하면 아무것도 남지 않는다.
- 끝에 `alembic_version = 0012` 가 기록되므로 이후 변경은 `alembic upgrade head` 로 이어진다.
- ⚠️ `sql/schema.sql` 은 `alembic upgrade head --sql` 의 **출력**이다. 손으로 고치지 말고
  마이그레이션을 고친 뒤 다시 생성한다.
- 앱 컨테이너가 기동 시 `alembic upgrade head` 를 돌리는 구성이라면 이 파일 없이도 된다.
  DB 를 DBA 가 따로 만드는 조직에서 쓰라고 있는 파일이다.

### 9.3 어디에 띄울까

`vector` 확장을 쓸 수 있는 매니지드 Postgres면 어디든 된다. 확인된 것들:

| 후보 | pg18 | pgvector | 비고 |
| --- | --- | --- | --- |
| **Railway Postgres** | 18.4 | **0.8.6** | 현재 데모가 쓰는 곳. M-1 에서 확장 생성 권한까지 실증했다 |
| Supabase | 15~17 | 0.8.x | pgvector 지원이 확실. pg18 은 확인 필요 |
| Neon | 17 계열 | 0.8.x | 서버리스. 콜드 스타트가 SSE 연결과 어떻게 맞물리는지 확인 필요 |
| AWS RDS / Aurora | 버전별 | **인스턴스마다 다름** | `SHOW rds.extensions` 로 pgvector 버전을 반드시 먼저 확인 |
| GCP Cloud SQL | 버전별 | 버전별 | 위와 같음 |
| 직접 운영 (docker) | `pgvector/pgvector:pg18` | 0.8.6 | 로컬·CI 와 완전히 같은 이미지 |

**무엇을 고르든 띄우기 전에 이 세 줄로 확인하라.** 여기서 걸리면 그 DB 는 쓸 수 없다.

```sql
SELECT version();                                              -- PostgreSQL 18.x
CREATE EXTENSION IF NOT EXISTS vector;                         -- 권한 확인
SELECT extversion FROM pg_extension WHERE extname = 'vector';  -- 0.8.0 이상
SET hnsw.iterative_scan = 'relaxed_order';                     -- 에러면 버전 부족
```

⚠️ **커넥션 수**를 함께 본다. 앱이 최대 `DB_POOL_SIZE + DB_MAX_OVERFLOW`(기본 20) 개를
잡는다. 매니지드 DB 의 `max_connections` 가 그보다 빠듯하면(소형 인스턴스는 20~25 인
경우가 있다) 풀 크기와 `PIPELINE_MAX_CONCURRENCY` 를 함께 줄여야 한다 (`03 §4.3`).

### 9.4 도커로 통째로 띄우기 (2026-08-12)

운영 서버를 직접 운영할 때의 구성이다. ⚠️ **오케스트레이션은 이 저장소에 없다** —
[`bulchimbeon-infra`](https://github.com/LetsMakeTrouble/bulchimbeon-infra) 의 루트
`docker-compose.yml` 이 프론트까지 함께 띄운다. 이 저장소는 `Dockerfile` 까지만,
즉 **"이미지를 어떻게 만드는가"** 만 정의한다.

```bash
git clone --recurse-submodules https://github.com/LetsMakeTrouble/bulchimbeon-infra.git
cd bulchimbeon-infra
sh scripts/init-env.sh                # .env 생성 — 비밀값 4개를 만들고 빠진 값을 짚어 준다
docker compose up -d --build
```

⛔ **개발용 `docker-compose.yml` 과 섞지 마라.** 이 저장소의 그것은 DB 만 띄우고 앱은
호스트에서 도는 개발 표준이며(`§5.1`) 비밀값이 개발용으로 박혀 있고 마이그레이션도 돌지 않는다.

**구성** (아래는 infra 저장소의 compose 기준이다)

| 서비스 | 역할 |
| --- | --- |
| `db` | `pgvector/pgvector:pg18`. 호스트 포트를 **열지 않는다** |
| `migrate` | `alembic upgrade head` 를 돌리고 **종료하는 원샷** |
| `api` | `migrate` 가 성공해야 시작한다 (`service_completed_successfully`) |
| `web` | 프론트 정적 번들(nginx). 앞단 터널이 `/api/*` 만 `api` 로 보낸다 |

> ### 왜 마이그레이션을 앱 커맨드에 붙이지 않았나
> `sh -c 'alembic upgrade head && exec uvicorn …'` 로 묶으면 마이그레이션 실패가 앱
> 재시작 루프에 묻혀 원인이 안 보인다. 별도 서비스로 두면 **거기서 멈추고 로그가 남는다.**
> (Railway 는 서비스가 하나뿐이라 그쪽에서는 묶을 수밖에 없다 — `09 §2`.)

> ### ⛔ 비밀값에 기본값을 두지 않는다
> `${SECRET_KEY:?...}` 문법이라 안 채우면 **컨테이너가 뜨기 전에** 이유와 함께 죽는다.
> `SECRET_KEY=change-me` 인 채로 조용히 뜨는 것이 최악이다 — 로그인 토큰을 누구나
> 위조할 수 있다.

**검증 실적 (2026-08-12, 실제 기동)** — 이 저장소에 있던 `docker-compose.prod.yml` 로
잰 값이다. infra 저장소의 compose 는 `db`·`migrate`·`api` 세 서비스가 같은 구조라
아래 항목이 그대로 적용되지만, **`web` 을 포함한 재확인은 아직 하지 않았다.**

| 확인 | 결과 |
| --- | --- |
| 기동 순서 | db healthy → migrate 종료(0) → api 시작 ✓ |
| 스키마 | 21 테이블 · `alembic_version=0012` · pgvector 0.8.6 ✓ |
| `/api/health` | `{"status":"ok","db":"ok"}` ✓ |
| `--workers 1` | PID 1 커맨드로 확인 ✓ (룰 9) |
| 실행 사용자 | `uid=10001(appuser)` — root 아님 ✓ |
| 업로드 볼륨 | appuser 로 쓰기 가능 ✓ (Railway 에서 겪은 소유권 문제가 없다) |
| CORS | 지정 오리진 허용 · 그 외 차단 ✓ |
| DB 포트 | 호스트 미노출 ✓ |
| 기동 잡 | `job_runs` 에 `zombie_recovery→expiry_sweeper→briefing` 이 `startup/ok` 로 기록 ✓ |

**리버스 프록시를 앞에 둘 때**

- ⚠️ **SSE 는 버퍼링을 끄지 않으면 죽는다.** nginx 라면 `proxy_buffering off;` 와 긴
  `proxy_read_timeout`(권장 1h+)이 필요하다. 앱이 `X-Accel-Buffering: no` 를 보내므로
  nginx 는 대개 알아서 처리하지만, 다른 프록시는 직접 꺼야 한다.
- `API_PORT` 를 `127.0.0.1:8000` 으로 묶어 프록시만 붙게 하는 것을 권한다.

**백업 — 두 가지를 함께 받아야 한다**

- `pgdata` 볼륨 (또는 `pg_dump`) — 질문·답변·청크·임베딩
- **`storage` 볼륨** — 업로드 **원본 파일**. DB 만 백업하면 청크는 남지만 원문은 복구되지
  않는다. 문서 재인제스트도 불가능해진다.
