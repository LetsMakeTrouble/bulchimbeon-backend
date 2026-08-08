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
| 8 | **클라우드 배포 1차** | `@prompts/09-seed-deploy.md 의 배포 부분만 먼저 실행해줘` | ⛔ 불가 |
| 9 | M6 브리핑·교훈 | `@prompts/06-briefing-lessons.md 실행해줘` | 스케줄러·교훈 컷 가능 |
| 10 | M7 지표 | `@prompts/07-metrics-events.md 실행해줘` | 2종만 남기고 컷 가능 |
| 11 | M9 시드·리허설 | `@prompts/09-seed-deploy.md 실행해줘` | ⛔ 시드는 불가 |
| — | M8 외부 연동 | `@prompts/08-integrations.md 실행해줘` | **기본 제외** (여유 시만) |

**배포를 8번째 세션(6일차)로 앞당긴 것이 의도다.** Railway pgvector extension 권한·마이그레이션·SSE 프록시 같은 외부 마찰을 하루 먼저 노출시켜 복구 시간을 확보한다.

---

## 다음 세션 프롬프트 (그대로 복붙)

> ✅ **M5 알림·SSE는 2026-08-08에 완료됐다** (`feat(M5): notifications, sse, expiry sweeper`).
> `pytest` **316 passed**(slow 포함), `ruff check`·`ruff format --check` 통과,
> 마이그레이션 head는 **0006**. `alembic check` = "No new upgrade operations detected".
> `grep -rn "TODO(M5)" app/` → **0건**.
>
> **M6·M8이 그대로 물려받는 것** (다시 만들지 마라)
> - **SSE 발행은 트랜잭션 아웃박스다** (`services/sse_manager.py`). 서비스는
>   `sse_manager.enqueue(db, ...)`(또는 `queue_*` 헬퍼)로 **세션에 적재만** 하고, 실제 발행은
>   SQLAlchemy `after_commit` 훅이 한다. 라우터에서 flush 를 부를 필요가 없고 **불러서도 안 된다** —
>   커밋 전에 발행하면 프론트의 재조회가 커밋 전 상태를 읽는다.
> - **`sse_manager.queue_briefing_ready` / `queue_sync_completed` 는 이미 있다.** 이벤트명과
>   payload 를 계약서(`05 §12.3`)에 맞춰 박아 둔 것이며 **호출부만 없다** —
>   M6 브리핑 스케줄러 / M8 동기화가 그 자리에서 부르면 된다.
> - **보류 알림 flush 가 M6 몫이다.** 비긴급 `card.created`·`feedback.different` 는
>   `notifications.deliver_after` = 다음 브리핑 시각으로 적재돼 있고 **목록·카운트에 나타나지
>   않는다**. 브리핑 발송 시 `deliver_after <= now()` 인 것을 발행해야 알림이 나간다.
>   지금은 시각이 지나면 자동으로 노출되므로 "영원히 안 나가는" 상태는 아니다.
> - **`notification_service.answerer_deliver_after(db, project=, immediate=)`** 가 룰 6 분기의
>   단일 구현이다(긴급→즉시 / 비긴급→브리핑 / DND→종료 이후). 브리핑 시각은
>   `dnd.next_briefing_at`, DND 종료는 `dnd.next_dnd_end_at` 이다.
> - **`accuracy_service.for_grade(db, project_id=, grade=, language=, window_days=)`** 가
>   등급별 실측 정확도(D25)의 유일한 구현이다. `05 §13` `grade_accuracy[]` 는 이것을 등급마다
>   불러 만들면 되고, 아이템 스키마는 `schemas/question.GradeAccuracy` 다(§6과 §13이 같은 shape).
> - **`sweeper_service`** 에 만료 스위퍼·좀비 회수가 있고 둘 다 `now=` 를 주입받는다.
>   잡 등록은 `core/scheduler.py` — M6 브리핑 잡을 여기에 더한다.
>
> ⚠️ **알림함 문자열은 수신자 `users.language` 로 만들어 저장한다** (`05 §1.5`).
> 문안 표는 `notification_service` 상단에 ko/en 쌍으로 모여 있다. 새 타입을 만들 때
> **계약서 어휘(`04 §4` 12종)를 벗어나지 마라** — `tests/test_notification_contract.py` 가
> 문서를 파싱해 막는다.
>
> ⚠️ **`event: ping` 은 우리가 직접 내보낸다** (`sse_stream_service.PING_INTERVAL_SECONDS`=14초).
> FastAPI 네이티브 keepalive 는 `: ping` **주석**이라 브라우저 `EventSource` 가 관측할 수 없고,
> `05 §12.2` 5번이 프론트에 요구한 "30초 넘게 ping 없으면 재연결"을 구현할 수 없다.
>
> ⚠️ **SSE 스트림은 요청 세션을 붙잡지 않는다.** FastAPI 의 `yield` 의존성은 스트리밍이 끝날
> 때까지 정리되지 않으므로 `Depends(get_db)` 를 쓰면 접속자 몇 명으로 커넥션 풀이 마른다.
> `sse_stream_service.session_factory` 로 자체 세션을 열고 즉시 닫는다(테스트 교체 지점).
>
> ⚠️ **httpx `ASGITransport` 는 스트리밍을 버퍼링한다**(실측 0.28.1) — 끝나지 않는 SSE 는
> `client.stream()` 으로도 한 줄을 못 읽고 테스트가 멈춘다. `tests/test_sse.py` 는 수명이 짧은
> access token 으로 서버가 스스로 끊게 만들어 검증한다.

```
@prompts/AUTORUN.md @prompts/09-seed-deploy.md

AUTORUN.md가 실행 규약이다. 특히 §3 "반드시 질문해야 하는 상황"을 지켜줘 —
특히 §3.3(외부 계정·비가역 작업)이 이번 세션의 핵심이다. 배포는 되돌리기 어렵고
Railway 계정·환경변수 설정이 필요하니 실행 전에 확인받아라.

09-seed-deploy.md 중 **배포 부분만 먼저** 실행해줘. 시드(scripts/seed.py)는 M9 세션이다.

M0~M5는 끝났다. 마이그레이션 head는 0006이고 pytest 316 passed다.
배포에서 특별히 확인할 것:
- Railway Postgres에 CREATE EXTENSION vector (M-1에서 권한 확인됨, 03 §6)
- 시작 커맨드에 --workers 1 고정 (SSE 인메모리 큐 + APScheduler 중복 발화)
- alembic upgrade head 선행
- SSE 프록시 통과 확인 — Railway는 15분에 강제 종료한다(정상 동작, 05 §12.2)
- STORAGE_DIR는 컨테이너 내 절대 경로

끝나면 클라우드 URL에서 /health와 /docs를 확인하고, START-HERE.md 세션표를
갱신한 뒤 보고하고 멈춰줘. 다음은 M6 브리핑·교훈이다(새 세션).
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
