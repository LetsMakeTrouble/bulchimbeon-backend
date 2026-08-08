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
| 10 | M8 외부 연동 — **✅ 완료 2026-08-08** | ~~`@prompts/08-integrations.md`~~ DoD 5/5, `feat(M8): notion and github sync` | 컷 없음 (AUTORUN §1) |
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

> ✅ **M8 외부 연동은 2026-08-08에 완료됐다** (`feat(M8): notion and github sync`).
> `pytest` **459 passed**, `ruff check`·`ruff format --check` 통과, 마이그레이션 head는 **0010**.
> `alembic check` = "No new upgrade operations detected".
> 실 GitHub 공개 레포 e2e 1회 통과 — `probe-github-sync-2026-08-08.txt`.
> 별도 리뷰 패스(`code-reviewer`) — 지적 14건 중 **차단 2건 포함 12건 수정**, 2건은 사양대로
> 유지(아래 미해결). 리뷰가 잡은 진짜 결함 둘은 목킹 테스트로는 드러나지 않던 것이었다:
> Notion 페이지 하나의 실패가 **전체 동기화를 죽이던 것**(`08 §6` 위반), 그리고 인제스트가
> 실패한 문서가 다음 실행부터 `unchanged`로 보고돼 **검색에서 빠진 채 실패 목록에서도
> 사라지던 것**. 둘 다 회귀 테스트가 붙었다.
>
> ### ⚠️ M8이 남긴 것 중 **M9·배포에 영향을 주는 것**
> - ⭐ **`INTEGRATION_ENCRYPTION_KEY`가 `.env`에 생겼다**(2026-08-08 생성, 랜덤 Fernet 키).
>   `04 §2` `integrations.config`의 토큰은 이 키로 암호화돼 있다. **배포 환경에 같은 값을
>   넣지 않으면** 그 연동은 복호화에 실패해 다시 등록해야 한다(문서·지식은 그대로 남는다).
>   env 값은 정식 Fernet 키가 아니어도 되고(`03 §4` 예시값도 동작), SHA-256으로 파생된다.
> - **`04 §5` 이벤트 26종이 전부 기록된다.** 마지막 하나였던 `sync.run`을 M8이 채웠고
>   스코프는 `entity_type='integration'`, payload는 `{provider, status, scanned,
>   new_documents, new_versions, unchanged, skipped, repaired, restored, failed[]}`다
>   (열거 자체가 실패하면 `fatal`이 붙는다). **동기화 실패 목록이 사는 유일한 곳**이며
>   `integrations`에는 컬럼을 더하지 않았다(사용자 결정 2026-08-08).
> - **`documents`에 부분 UNIQUE `(project_id, source_type, source_ref)`가 생겼다**
>   (마이그 0010, 사용자 결정 2026-08-08). 외부 원본 1개 = 문서 1개를 DB가 강제한다 —
>   락이 아니라 제약으로 막는 방식은 `briefing_runs`(결정 1.11)와 같다.
>   업로드 문서는 `source_ref`가 NULL이라 대상 밖이다.
> - **`document_service`에 바이트 기반 진입점이 생겼다** — `create_version_from_bytes` ·
>   `create_synced_document` · `find_by_source_ref` · `latest_version`. 업로드 경로도 이제
>   `create_version_from_bytes`를 거치므로 버전 번호 잠금·저장 규칙의 구현은 한 곳뿐이다.
> - **시드는 연동을 만들지 않는다.** `08 §4` 데모 시나리오에 연동이 등장하지 않고, 시드가
>   외부 네트워크에 의존하는 순간 오프라인에서 재현이 깨진다.
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
> **M9가 그대로 물려받는 것** (다시 만들지 마라)
> - **지표·타임라인은 손대지 않는다.** `metrics_service`(비율 4종·시계열)와
>   `accuracy_service.for_grade`(D25)가 정의의 유일한 위치이고, `event_service.list_timeline`이
>   타임라인의 유일한 조회다. 새 이벤트를 기록하기만 하면 시계열·타임라인에 자동으로 실린다.
> - **활성 버전 교체는 3문 절차**다(`04 §7`, `document_service.activate_version`). 시드도
>   예외 없다 — 단일 UPDATE는 행 순서에 따라 통과하기도 해서 테스트가 초록인 채 운영에서
>   간헐 실패한다.
> - **시드는 서비스 레이어를 직접 부른다**(`09 §2`). HTTP를 태우지 않으므로 권한 의존성이
>   끼지 않고, 카드 상세를 호출하지 않아 `first_viewed_at`이 오염되지 않는다.
>
> ### ⚠️ M8이 겪은 것 — 테스트 DB는 **기존 테이블에 붙은 새 인덱스를 반영하지 않는다**
> `Base.metadata.create_all`은 테이블 단위로 `checkfirst`한다. 테이블이 이미 있으면 통째로
> 건너뛰므로 새 인덱스가 들어가지 않는다 — **새 테이블 추가는 멀쩡히 반영되기 때문에**
> 눈치채기 어렵다. M8의 `uq_documents_source_ref`를 넣었을 때 제약 검증 테스트가 "제약이
> 없어서" 실패했다. 기존 테이블에 제약·인덱스를 더했으면 테스트 DB를 한 번 지운다
> (명령은 `tests/conftest.py` 상단). 마이그레이션 자체는 `test_migrations.py`가 별도
> 스크래치 DB에서 검증하므로 영향이 없다.
>
> 🔧 **M8이 남긴 알려진 미해결 1건** (커밋했고, 급하지 않다):
> - **실패한 동기화에는 SSE가 없다.** `05 §12.3`의 sync 계열 이벤트가 `sync.completed`
>   하나뿐이라 계약을 지킨 결과다. 담당자는 `sync.failed` 알림과 `last_sync_status`로 안다.
>   ⚠️ **DND 구간(기본 22:00~07:00)에는 그 알림마저 다음 브리핑까지 보류된다**(룰 6) —
>   즉 밤에 실패한 동기화는 `GET /projects/{id}/integrations`의 `last_sync_status`를
>   보기 전까지 아무 신호가 없다. 프론트에 "이 버튼은 실패 시 조용할 수 있다"를 전달할 것.
>
> 🔧 **M7이 남긴 알려진 미해결 2건** (여전히 유효하다):
> - `card_handle_30s_rate`는 `payload->>'card_id'`로 조인한다 — JSONB 안이라 인덱스를 타지
>   않는다. 바깥 집합이 "창 안에 열람된 카드"로 좁혀져 있어 데모·운영 규모에서는 문제가 아니지만,
>   프로젝트 하나의 이벤트가 수십만 건이 되면 `events(type, (payload->>'card_id'))`가 필요하다.
> - `grade_accuracy[]`(D25)만 원천이 `answers` 행이고 나머지 지표는 전부 `events`다. 그래서
>   `reason='failed'` 카드를 담당자가 직접 써서 확정한 답변이 🔴 분모·분자에 함께 잡힌다
>   (자동응답률에서는 제외된다 — `question.graded`가 없으므로). D25의 "해당 등급 전체 발행 수"를
>   문자 그대로 따른 결과이며, 바꾸려면 D25를 먼저 고쳐야 한다.

```
@prompts/AUTORUN.md @prompts/09-seed-deploy.md

두 문서를 읽고 M9 중 **시드 부분(작업 1·2·3)** 을 실행해줘. 배포·리허설(4·5번)은
다음 세션이다 — 이번 세션에서 배포까지 하지 마라.

AUTORUN.md가 실행 규약이다. 특히 §3 "반드시 질문해야 하는 상황"을 지켜줘 —
사양이 갈리거나, 실측이 문서를 뒤집거나, DoD가 안 닫히거나, 스코프가 애매하면
추측하지 말고 AskUserQuestion으로 물어봐. 반대로 §4에 있는 것들(문서에 답이
있는 것, 관례적 판단)은 묻지 말고 그냥 진행해.

⚠️ 09-seed-deploy.md 상단의 "배포는 6일차로 앞당긴다"는 **이미 뒤집혔다** —
   사용자 결정 2026-08-08 로 개발 완주 후 배포다 (START-HERE 세션표 참조).

M0~M8은 끝났다. 마이그레이션 head는 0010, pytest 459 passed다.
M8이 남겨 둔 접점 (다시 만들지 마라):
- 04 §5 이벤트 26종이 전부 기록된다. 시드가 서비스 레이어를 그대로 부르면
  지표·타임라인·시계열이 자동으로 채워진다.
- document_service 에 바이트 기반 진입점이 있다 (create_version_from_bytes /
  create_synced_document). 시드가 파일을 넣을 때 새 경로를 만들지 마라.
- 시드는 연동(integrations)을 만들지 않는다. 08 §4 데모에 연동이 없고, 시드가
  외부 네트워크에 의존하면 오프라인 재현이 깨진다.

⚠️ 시드는 카드 상세(GET /review-cards/{id})를 호출하지 않는다 —
   first_viewed_at 이 오염되면 card_handle_30s_rate 가 무의미해진다.
⚠️ Q1·Q8·Q10 원문은 이력에 소진하지 않는다 (09 §2의 제외 목록·어서션).
⚠️ --with-history 는 실 LLM 이 아니라 FakeLLM 으로 돌릴지 먼저 확인해라.
   실 API 로 45~60건이면 비용이 발생한다 (AUTORUN §3.3).

M9 시드 DoD를 하나씩 확인하고, 통과하면 커밋하고, START-HERE.md 세션표를 갱신한 뒤
보고하고 멈춰줘. 다음은 클라우드 배포·리허설이다(새 세션).
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
