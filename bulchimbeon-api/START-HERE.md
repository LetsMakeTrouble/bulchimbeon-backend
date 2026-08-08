# START HERE — 세션별 복붙 프롬프트

> 이 레포는 `bulchimbeon` 킷을 복사한 **구현 레포**다. 원본 킷(`../bulchimbeon`)은 참조용으로 보존한다.
> **프롬프트 1개 = Claude Code 세션 1개.** 완료·커밋 후 `/clear` 하고 다음으로 넘어간다.

## 실행 전 1회

```bash
# 최초 1회 — 키를 .env에 기록 (git 추적 제외됨)
echo 'OPENAI_API_KEY=sk-...' > .env && chmod 600 .env

# 매 세션 — .env 로드
set -a; . ./.env; set +a
```

> ⚠️ 키를 `~/.bashrc`에만 넣으면 **에이전트가 쓰는 non-interactive 셸에서는 로드되지 않는다.**
> Ubuntu 기본 `.bashrc`는 상단(`case $- in *i*`)에서 non-interactive 셸을 즉시 `return` 시키기 때문이다.
> 그래서 키의 원천은 `.env` 하나로 고정한다.

M-1 캘리브레이션은 **실 임베딩 호출이 필수**라 FakeLLM으로 대체 불가하다. 키가 없으면 시작할 수 없다.

---

## 세션 순서

| # | 세션 | 프롬프트 | 컷 가능? |
| --- | --- | --- | --- |
| 0 | **M-1 캘리브레이션 게이트** — **✅ 완료 2026-08-07** | ~~`@prompts/00-calibration.md`~~ 산출값 `04 §3` 반영 완료 | ⛔ 불가 |
| 1 | M0 스캐폴딩 — **✅ 완료 2026-08-07** | ~~`@prompts/00-kickoff.md`~~ DoD 5/5, `chore(M0): scaffold` | ⛔ 불가 |
| 2 | M1 인증·프로젝트 — **✅ 완료 2026-08-08** | ~~`@prompts/01-auth-projects.md`~~ DoD 7/7, `feat(M1): auth, projects, members` | ⛔ 불가 |
| 3 | M2 문서 인제스트 — **✅ 완료 2026-08-08** | ~~`@prompts/02-documents-ingest.md`~~ DoD 6/6, `feat(M2): document ingest pipeline` | 컷 없음(4포맷 전부) |
| 4~5 | **M3 질문 파이프라인** ⭐ — **✅ 완료 2026-08-08** | ~~`@prompts/03-question-pipeline.md`~~ DoD 7/7, `feat(M3): question answering pipeline with grading` | ⛔ 불가 |
| 6 | **M4 확인 워크플로** ⭐ — **✅ 완료 2026-08-08** | ~~`@prompts/04-review-workflow.md`~~ DoD 통과, `feat(M4): review workflow and knowledge loop` | ⛔ 불가 |
| 7 | M5 알림·SSE — **✅ 완료 2026-08-08** | ~~`@prompts/05-notifications-sse.md`~~ DoD 6/6, `feat(M5): notifications, sse, expiry sweeper` | 컷 없음(전 범위) |
| 8 | M6 브리핑·교훈 — **✅ 완료 2026-08-08** | ~~`@prompts/06-briefing-lessons.md`~~ DoD 8/8, `feat(M6): briefing scheduler and lesson memory` | 컷 없음(전 범위) |
| 9 | M7 지표 — **✅ 완료 2026-08-08** | ~~`@prompts/07-metrics-events.md`~~ DoD 7/7, `feat(M7): events timeline and metrics` | 컷 없음(4종+정확도) |
| 10 | M8 외부 연동 | `@prompts/08-integrations.md 실행해줘` | 컷 없음 (AUTORUN §1) |
| 11 | M9 시드 | `@prompts/09-seed-deploy.md 의 시드 부분` | ⛔ 시드는 불가 |
| 12 | **클라우드 배포 + 리허설** | `@prompts/09-seed-deploy.md 의 배포·리허설 부분` | ⛔ 불가 |

> ### ⚠️ 배포 순서를 바꿨다 (**사용자 결정 2026-08-08**)
> **원래 계획은 배포를 6일차(M5 직후)로 앞당기는 것이었다.** `07 §일정`이 그렇게 배치한 근거는
> "Railway pgvector extension 권한·마이그레이션·SSE 프록시 같은 **외부 마찰은 코드로 해결되지
> 않으므로** 하루 먼저 노출시켜 복구 시간을 확보한다"였다. 사용자 지시로 **개발을 먼저 완주하고
> 배포를 마지막에** 두기로 바꿨다. `07 §일정`은 그대로 두었다 — 사양이 아니라 일정 권고이고,
> 되돌릴 때 원래 근거가 남아 있어야 한다.
>
> **그래서 마지막 세션이 가장 위험하다.** 가장 큰 미지수였던 `CREATE EXTENSION vector` 권한은
> M-1에서 실측 검증됐지만(`03 §6`), 남은 것들은 **아직 한 번도 실행되지 않았다**:
> - SSE가 프록시를 통과하는지 (Railway는 15분에 강제 종료 — 정상 동작이지만 확인은 필요하다)
> - `STORAGE_DIR` 볼륨이 재시작을 넘어 살아남는지 (업로드 파일이 사라지면 근거 검색이 비어 버린다)
> - 시작 커맨드의 `--workers 1` 고정과 `alembic upgrade head` 선행
>
> 배포 세션을 데모 **전날 이전**에 잡는다. 당일에 잡으면 복구 시간이 0이다.

---

## 다음 세션 프롬프트 (그대로 복붙)

> ✅ **M7 지표·타임라인은 2026-08-08에 완료됐다** (`feat(M7): events timeline and metrics`).
> `pytest` **389 passed**(slow 포함), `ruff check`·`ruff format --check` 통과,
> 마이그레이션 head는 **0008**. `alembic check` = "No new upgrade operations detected".
> `grep -rn "TODO(M7)" app/` → **0건**. 별도 리뷰 패스(`code-reviewer`+`verifier`) 통과.
>
> ### ⚠️ M7이 고친 것 — `created_at` 기본값이 `now()`가 아니라 `clock_timestamp()`다
> M7 작업 1(이벤트 감사)이 찾은 건 **빠진 이벤트 타입이 아니라 시각이었다.** `04 §5`의 26종 중
> `sync.run`(M8 몫)을 뺀 **25종이 전부 제자리에 기록되고 있었지만**, `now()`는
> `transaction_timestamp()`라 한 요청이 만든 행들의 `created_at`이 **완전히 동일**했다.
> 파이프라인 한 번이 남기는 이벤트 4건이 동률이라 `05 §13` 타임라인에 **순서가 없었다** —
> 그런데도 조회는 성공하므로 우연히 맞는 순서로 초록이 지나갔다.
> `app/models/base.py`의 `ROW_TIMESTAMP` 하나가 정본이고 전 테이블에 적용됐다(마이그 0008).
> **새 모델은 `CreatedAtMixin`/`TimestampMixin`을 그대로 쓴다** — `server_default=func.now()`를
> 직접 쓰면 같은 결함이 되살아난다. 이제 `alembic/env.py`가 `compare_server_default=True`라
> 모델과 DB의 기본값이 어긋나면 `alembic check`가 잡는다.
>
> **M8이 그대로 물려받는 것** (다시 만들지 마라)
> - ⭐ **`sync.run`은 `04 §5` 이벤트 타입 중 유일하게 아직 기록되지 않은 하나다.** M8이 채운다.
>   `event_service`에 상수부터 추가한다(타입 문자열을 인라인으로 쓰지 않는다).
> - ⭐ **`sync.completed`/`sync.failed` 알림과 SSE `sync.completed`는 이미 닫힌 집합 안에 있다.**
>   `tests/test_notification_contract.py`가 `04 §4`와 `05 §12.3`을 **정규식으로 파싱해** 막으므로
>   문안·payload를 문서와 다르게 만들면 그 자리에서 터진다. SSE payload는
>   `{integration_id, new_documents, new_versions}` 고정이다.
> - **동기화가 만든 새 버전은 M2·M4 파이프라인을 그대로 탄다** — `document_service`의 인제스트·
>   활성화와 `review_cascade_service.cascade_for_document`가 이미 룰 5를 구현한다. 별도 경로를
>   만들면 "문서가 바뀌었는데 확정 답변이 재검토되지 않는" 구멍이 생긴다(`08 §3` 3번이 명시).
> - **활성 버전 교체는 2문 절차**다(`04 §7`). 동기화가 버전을 올릴 때도 예외 없다 —
>   단일 UPDATE는 행 순서에 따라 통과하기도 해서 테스트가 초록인 채 운영에서 간헐 실패한다.
> - **지표·타임라인은 손대지 않는다.** `metrics_service`(비율 4종·시계열)와
>   `accuracy_service.for_grade`(D25)가 정의의 유일한 위치이고, `event_service.list_timeline`이
>   타임라인의 유일한 조회다. 새 이벤트를 기록하기만 하면 시계열·타임라인에 자동으로 실린다.
> - **이벤트는 전부 질문 스코프**로 남긴다(`event_service.ENTITY_ANSWER` 주석). `sync.run`은
>   질문이 아니라 연동 스코프이므로 `entity_type='integration'`이 자연스럽다 — 이건 `04 §5`가
>   payload를 규정하지 않은 지점이라 **구현 전에 물어라**.
>
> ⚠️ **`INTEGRATION_ENCRYPTION_KEY`(Fernet)와 Notion·GitHub 토큰은 직접 만들 수 없다.**
> AUTORUN §3.3 — 외부 계정·키가 필요한 지점이다. 시작하자마자 요청하고, 그동안 스텁 경로와
> 모델·마이그레이션처럼 키가 필요 없는 작업을 먼저 한다.
>
> ⚠️ **`08-integrations.md` 상단의 "⛔ 기본 제외"는 적용하지 않는다** — AUTORUN §1이
> "M8 실구현이 전부 범위 안"이라고 뒤집었다. 스텁 501로 끝내지 마라.
>
> 🔧 **M7이 남긴 알려진 미해결 2건** (커밋했고, 급하지 않다):
> - `card_handle_30s_rate`는 `payload->>'card_id'`로 조인한다 — JSONB 안이라 인덱스를 타지
>   않는다. 바깥 집합이 "창 안에 열람된 카드"로 좁혀져 있어 데모·운영 규모에서는 문제가 아니지만,
>   프로젝트 하나의 이벤트가 수십만 건이 되면 `events(type, (payload->>'card_id'))`가 필요하다.
> - `grade_accuracy[]`(D25)만 원천이 `answers` 행이고 나머지 지표는 전부 `events`다. 그래서
>   `reason='failed'` 카드를 담당자가 직접 써서 확정한 답변이 🔴 분모·분자에 함께 잡힌다
>   (자동응답률에서는 제외된다 — `question.graded`가 없으므로). D25의 "해당 등급 전체 발행 수"를
>   문자 그대로 따른 결과이며, 바꾸려면 D25를 먼저 고쳐야 한다.

```
@prompts/AUTORUN.md @prompts/08-integrations.md

두 문서를 읽고 M8 외부 연동(Notion·GitHub)을 실행해줘.

AUTORUN.md가 실행 규약이다. 특히 §3 "반드시 질문해야 하는 상황"을 지켜줘 —
사양이 갈리거나, 실측이 문서를 뒤집거나, DoD가 안 닫히거나, 스코프가 애매하면
추측하지 말고 AskUserQuestion으로 물어봐. 반대로 §4에 있는 것들(문서에 답이
있는 것, 관례적 판단)은 묻지 말고 그냥 진행해.

⚠️ 08-integrations.md 상단의 "⛔ 기본 제외"와 스텁 501 경로는 **적용하지 않는다.**
데드라인이 없으므로 GitHub·Notion 동기화를 실제로 구현한다 (AUTORUN §1).

M0~M7은 끝났다. 마이그레이션 head는 0008, pytest 389 passed다.
M7이 남겨 둔 접점 (다시 만들지 마라):
- sync.run 은 04 §5 이벤트 타입 중 아직 기록되지 않은 유일한 하나다. M8 이 채운다.
- 동기화가 만든 새 버전은 M2 인제스트 + M4 재검토 연쇄를 그대로 탄다.
  별도 경로를 만들면 룰 5(문서 갱신 시 확정 답변 재검토)가 조용히 깨진다.
- created_at 기본값은 clock_timestamp() 다. 새 모델은 CreatedAtMixin/TimestampMixin
  을 그대로 쓴다 — func.now() 를 직접 쓰면 M7 이 고친 결함이 되살아난다.
- 지표·타임라인은 손대지 마라. 이벤트를 기록하기만 하면 자동으로 실린다.

⚠️ INTEGRATION_ENCRYPTION_KEY(Fernet)와 Notion·GitHub 토큰은 네가 만들 수 없다.
   AUTORUN §3.3 이다 — 시작하자마자 요청하고, 기다리는 동안 키가 필요 없는
   모델·마이그레이션·스키마부터 해라.
⚠️ 토큰이 DB 에 평문으로 저장되지 않는 것을 테스트로 확인한다 (완료 기준).
⚠️ 알림 타입·SSE 이벤트는 닫힌 집합이다. tests/test_notification_contract.py 가
   04 §4 와 05 §12.3 을 정규식으로 파싱해 막는다.

M8 DoD를 하나씩 확인하고, 통과하면 커밋하고, START-HERE.md 세션표를 갱신한 뒤
보고하고 멈춰줘. 다음은 M9 시드다(새 세션).
```

> 이후 마일스톤도 같은 형태다 — `@prompts/AUTORUN.md @prompts/01-auth-projects.md` 처럼
> **규약 + 해당 마일스톤 프롬프트**를 함께 넘긴다. AUTORUN은 매 세션 다시 읽혀야 한다.

---

## 매 세션 지켜야 할 것

- **DoD가 통과된 것을 확인하고 커밋한 뒤** 다음 세션으로 간다. 테스트가 깨진 채 넘어가지 않는다.
- 커밋 메시지는 `feat(M3): ...` 형태로 마일스톤 태그를 붙인다.
- 설계를 바꾸고 싶으면 **`docs/`를 먼저 고치고** 해당 프롬프트를 다시 실행한다. 문서가 곧 사양이다.
- 문서 간 충돌을 발견하면 우선순위: `02-business-rules` > `05-api-contract` > `06-ai-pipeline` > 나머지.
- 프론트 팀에는 **M1 완료 시점에** `docs/05-api-contract.md` + Swagger URL을 1차 전달, M4 완료 시 확정 공지.

## 자주 밟는 지뢰 (CLAUDE.md에도 있음)

- pgvector `<=>`는 **거리**다. 유사도는 `1 - (embedding <=> :q)`.
- **`register_vector`는 raw asyncpg 전용** — SQLAlchemy 경로에 등록하면 모든 벡터 바인딩이 `DataError`로 죽는다 (M2 실측, `03 §5.4` ③).
- GPT-5 계열에 `temperature` 전달 금지(400). `max_tokens`가 아니라 `max_completion_tokens`.
- BackgroundTasks에는 **UUID만** 전달. ORM 객체·세션을 넘기면 질문이 `processing`에 영구 정지한다.
- `--workers 1` 고정. SSE 큐가 인메모리고 APScheduler가 워커마다 중복 발화한다.
- strict Structured Outputs는 **기본값 있는 필드 금지**. `conflict_chunk_ids: []`가 400의 직접 원인.
- 부분 UNIQUE는 DEFERRABLE 불가 — 활성 버전·담당자 스왑은 한 트랜잭션 안에서 **2문으로 순서를 지켜** 처리.
  ⚠️ 단일 UPDATE는 **행 순서에 따라 통과하기도 한다**(M-1 실측). 항상 터지는 게 아니라서 테스트가 초록인 채로 운영에서 간헐 실패한다 — `04 §7`.
- **`created_at` 기본값은 `clock_timestamp()`다. `now()`를 쓰지 마라**(M7 실측). `now()`는 트랜잭션 시작 시각이라
  한 요청이 만든 행들이 **전부 같은 시각**이 되고 `ORDER BY created_at`이 순서를 못 만든다. 타임라인·큐 정렬이
  물리적 행 배치에 따라 달라지는데, **틀린 순서로도 조회는 성공**해서 우연히 초록으로 지나간다 — `04 §7`.
