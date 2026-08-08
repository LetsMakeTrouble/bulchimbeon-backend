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
| 4~5 | **M3 질문 파이프라인** ⭐ | `@prompts/03-question-pipeline.md 실행해줘` | ⛔ 불가 |
| 6 | **M4 확인 워크플로** ⭐ | `@prompts/04-review-workflow.md 실행해줘` | ⛔ 불가 |
| 7 | M5 알림·SSE | `@prompts/05-notifications-sse.md 실행해줘` | `answer.completed`만 필수 |
| 8 | **클라우드 배포 1차** | `@prompts/09-seed-deploy.md 의 배포 부분만 먼저 실행해줘` | ⛔ 불가 |
| 9 | M6 브리핑·교훈 | `@prompts/06-briefing-lessons.md 실행해줘` | 스케줄러·교훈 컷 가능 |
| 10 | M7 지표 | `@prompts/07-metrics-events.md 실행해줘` | 2종만 남기고 컷 가능 |
| 11 | M9 시드·리허설 | `@prompts/09-seed-deploy.md 실행해줘` | ⛔ 시드는 불가 |
| — | M8 외부 연동 | `@prompts/08-integrations.md 실행해줘` | **기본 제외** (여유 시만) |

**배포를 8번째 세션(6일차)로 앞당긴 것이 의도다.** Railway pgvector extension 권한·마이그레이션·SSE 프록시 같은 외부 마찰을 하루 먼저 노출시켜 복구 시간을 확보한다.

---

## 다음 세션 프롬프트 (그대로 복붙)

> ✅ **M2 문서 인제스트는 2026-08-08에 완료됐다** (`feat(M2): document ingest pipeline`). DoD 6/6.
> `pytest` 128 passed + slow 1 passed, `ruff check`·`ruff format --check` 통과, 마이그레이션 head는 **0003**.
> 실서버(`uvicorn --workers 1`) 스모크로 계약서 §4 **7개 엔드포인트**를 실커밋 경로에서 확인했다.
>
> ⚠️ **`auto_activate=true`는 "지금 활성화"가 아니라 "인제스트가 `ready`면 활성화"다** (`02 §5` 구현 노트에 반영).
> 업로드 201 응답의 `active_version`은 **아직 `null`**이고(새 버전이면 직전 버전이 그대로 활성),
> 활성 전환은 `pipeline/ingest.py`가 ready 커밋 직후에 `document_service.activate_version`을 불러 수행한다.
> 이유: 업로드 시점에 활성화하면 인제스트 실패 시 신버전(`active`+`failed`)도 구버전(`inactive`+`ready`)도
> 검색 범위에서 탈락해 **그 문서의 근거가 통째로 사라진다**. 회귀 테스트:
> `tests/test_documents.py:test_failed_new_version_does_not_take_down_the_previous_evidence`.
> **M4의 재검토 연쇄 훅은 `activate_version` 안에 붙는다** — 근거가 실제로 교체되는 순간 딱 한 번 발화한다.
>
> ⚠️⚠️ **M3가 가장 먼저 알아야 할 것 — `register_vector`는 제거됐다** (`03 §5.4` ③ 문서 갱신 완료)
> `pgvector.asyncpg.register_vector`는 **raw asyncpg 전용**이다. SQLAlchemy 경로에서 코덱을 등록하면
> `pgvector.sqlalchemy.Vector`의 문자열 변환과 이중 충돌해 **모든 벡터 바인딩**이 죽는다
> (`DataError: '[1.0, 0.0, ...]' (expected list or ndarray)`). ORM INSERT뿐 아니라
> `SELECT (:a)::vector <=> (:b)::vector` 같은 단순 캐스트도 함께 죽는다.
> M1이 초록이었던 건 벡터를 **한 번도 바인딩하지 않았기** 때문이다. M3가 이걸 되돌리면 파이프라인 전체가 멈춘다.
> 코덱 없이도 `<=>`는 그대로 **거리**다(직교 = 1.0, 실측 확인). 유사도는 여전히 `1 - (embedding <=> :q)`.
>
> **M3가 그대로 물려받는 것** (다시 만들지 마라)
> - **`app/services/pipeline/retrieval.py`가 검색 범위의 단일 원천**이다. `searchable_chunks_query()`에
>   `order_by(Chunk.embedding.cosine_distance(q))` + `limit(retrieval_top_k)`만 얹어라.
>   3중 조건(`documents.status='active'` + `is_active` + `ingest_status='ready'`)을 우회해 `chunks`를
>   직접 조회하면 D20(soft delete)·룰 5(구버전)·`05 §12.3`(인제스트 미완)이 한꺼번에 깨진다.
> - `app/services/llm/` — `get_provider()`가 프로바이더 선택의 **유일한 지점**이다. `LLM_PROVIDER` env를
>   호출 시점에 읽는다. `embed()`는 실구현·검증(1536 fail-fast)까지 끝났고,
>   **`complete_json`·`translate`는 M3가 채운다** (`responses.parse` 고정, `temperature` 금지,
>   `max_completion_tokens`). `FakeLLMProvider`의 `complete_json`은 스키마 필수 키만 채우는 더미이므로
>   M3에서 `06 §5`가 요구하는 **마커 기반 분기 응답**으로 확장해야 한다.
> - `app/services/sse_manager.py` — `publish()` 몸통만 M5에서 채우면 된다. **호출부는 건드리지 마라.**
>   M3의 `answer.completed`도 같은 훅을 거친다.
> - `tests/conftest.py`의 **`task_session_factory`** — BackgroundTasks가 여는 자체 세션을 테스트
>   트랜잭션에 물리는 픽스처다. M3 질문 파이프라인 테스트가 이것 없이는 개발 DB에 쓴다.
>   `fake_llm_provider`(세션 스코프 autouse)가 실 API 호출을 원천 차단한다.
> - `app/utils/parsing.py`·`chunking.py` — 파싱·청킹은 순수 함수다. `heading_path`/`page_no`가
>   `05 §6` `citations[]`의 원천이다.
> - **트랜잭션 경계는 라우터**다. 서비스는 `flush()`까지만 하고 커밋은 라우터가 한다.
>
> ⚠️ **부분 UNIQUE 스왑은 M2에서도 그대로 재현됐다** — `document_service.activate_version`이
> `04 §7`의 3문 절차(상위 행 `FOR UPDATE` → 전부 false → 하나만 true)를 쓴다.
> 회귀 테스트 `tests/test_documents.py:test_activate_survives_both_swap_directions`는
> **낮은→높은·높은→낮은을 4회 왕복**한다. 한 방향만 돌리면 잘못된 단일 UPDATE가 초록으로 통과한다.
> `synchronize_session="fetch"`도 그대로 필요하다(identity map이 낡은 `is_active`를 돌려준다).

```
@prompts/AUTORUN.md @prompts/03-question-pipeline.md

두 문서를 읽고 M3 질문 파이프라인을 실행해줘.

AUTORUN.md가 실행 규약이다. 특히 §3 "반드시 질문해야 하는 상황"을 지켜줘 —
사양이 갈리거나, 실측이 문서를 뒤집거나, 외부 계정·비가역 작업이 필요하거나,
DoD가 안 닫히거나, 스코프가 애매하면 추측하지 말고 AskUserQuestion으로 물어봐.
반대로 §4에 있는 것들(문서에 답이 있는 것, 관례적 판단)은 묻지 말고 그냥 진행해.
M3는 데모의 심장이라 위임하지 말고 직접 해줘(§5).

M0·M1·M2는 이미 끝났다. 임계값은 config.py의 DEFAULT_SETTINGS에만 있고, 에러 코드는
core/errors.py에 05 §1.4대로 있고, 권한 의존성은 core/deps.py(require_asker 포함)에 있다.
LLM 호출은 services/llm/get_provider() 경유만이고, 검색 범위는
services/pipeline/retrieval.py의 searchable_chunks_query()가 단일 원천이다 —
새로 만들지 말고 그대로 쓴다(룰 2·3, §7).

⚠️ app/database.py의 register_vector 제거를 되돌리지 마라. SQLAlchemy 경로에서 코덱을
등록하면 모든 벡터 바인딩이 DataError로 죽는다(M2 실측, 03 §5.4 ③에 반영됨).
유사도는 1 - (embedding <=> :q)다. <=>는 거리다.

M3 DoD를 하나씩 확인하고(06 §5의 1~7번 테스트 포함), 실LLM 스모크 1회까지 돌리고,
통과하면 커밋하고, START-HERE.md 세션표를 갱신한 뒤 보고하고 멈춰줘.
M4는 새 세션에서 이어간다.
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
