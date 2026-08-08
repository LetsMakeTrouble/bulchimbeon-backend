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

> ✅ **M3 질문 파이프라인은 2026-08-08에 완료됐다** (`feat(M3): question answering pipeline with grading`). DoD 7/7.
> `pytest` 217 passed + slow 1 passed, `ruff check`·`ruff format --check` 통과, 마이그레이션 head는 **0004**.
> 실LLM 스모크(`scripts/smoke_pipeline.py`) 🟢 9.0s / 25s, 실서버(`uvicorn --workers 1`) 스모크로
> 업로드→인제스트→질문→🟢 발행까지 실커밋 경로에서 확인했다(S=93, `sim_raw` 0.649).
>
> ⚠️ **`answers`에 컬럼이 하나 늘었다 — `similar_official_qa_id`** (`04 §2`에 반영, 사용자 승인).
> `05 §6`의 `similar_official_qa`를 채울 자리가 데이터 모델에 없었다. 질문 임베딩을 저장하지 않으므로
> 조회 시점에 다시 계산할 수도 없다. **`official_qa_id`를 재활용하지 마라** — D22가 "`official_qa_id`가
> 있으면 맞았다 시 `correct_count++`"라서, 엉뚱한 Q&A의 카운트가 M4에서 올라간다.
>
> ⚠️ **`answers` ↔ `official_qas`는 순환 FK다.** 모델의 `use_alter=True`를 지우면
> `Base.metadata.create_all`이 `CircularDependencyError`로 죽어 **테스트가 통째로** 멈춘다 (`04 §1`).
>
> **M4가 그대로 물려받는 것** (다시 만들지 마라)
> - **`answers.question_struct`에 ⑦ 결과가 이미 들어 있다** (`{background, question, options[]}`).
>   M4는 카드 생성 시 이것을 `review_cards.question_struct`로 **복사**만 한다. 다시 생성하지 마라.
> - **`app/services/answer_service.py`** — `ensure_confirmable`(만료 답변 확정 차단, D13) ·
>   `ensure_feedback_allowed`(D12) 가드가 단위 테스트까지 끝나 있다. M4는 카드/피드백 엔드포인트에서
>   **부르기만** 하면 된다. `06 §5` 테스트 7의 e2e(카드 처리 → 409) 단언이 M4 몫이다.
> - **카드 생성 지점은 `pipeline/answer.py`의 `_publish_generated` 끝**에 `TODO(M4)`로 표시돼 있다
>   (🟢 `green`·🟡 `yellow`·🔴 `red`). 실패 카드는 `mark_question_failed` 안의 `TODO(M4)`다.
> - `question_service._DEFAULT_CARD_STATUS` — `held_info.card_status`/`failure_info.card_status`가
>   지금은 `"pending"` 고정이다. `review_cards`가 생기면 **실제 상태를 읽도록** 갈아끼운다.
> - `question_service._to_answer_out`의 `feedback_summary`는 지금 0 고정이다(`TODO(M4)`).
> - **`answer.reuse_missed` 이벤트가 이미 발행된다** (D26 분모). 후보 기준은 `similar_threshold` 이상이다.
>   M4가 공식 Q&A를 만들기 시작하면 이 지표가 비로소 의미를 갖는다.
>
> ⚠️ **`FakeLLMProvider`는 마커로 분기한다** — 질문 본문의 `[[fake:sentences=5,supported=2]]` 같은 토큰이다.
> 마커 표는 `app/services/llm/fake_provider.py` 독스트링에 있다. ① 번역이 `content_en = "[en] {원문}"`을
> 만들기 때문에 마커가 ②④⑦까지 그대로 실려 간다 — **① 의 user 프롬프트에 장식을 붙이면 이 규약이 깨진다.**
> 유사도에 의존하는 테스트는 `tests/pipeline_helpers.embedding_with_cosine`이 **목표 코사인을 갖는 벡터를
> 합성**해 심는다(해시 임베딩끼리는 코사인이 사실상 0이라 S가 항상 0이 된다).
>
> ⚠️ **`tests/conftest.py`의 `task_session_factory`가 `answer.session_factory`도 갈아끼운다.**
> 파이프라인은 실패 기록용 세션을 **따로** 연다(예외가 난 세션 위에서는 커밋할 수 없다) — 둘 다 같은
> 팩토리를 거치므로 한 곳만 바꾸면 된다. `reset_llm_quota`(autouse)는 프로세스 메모리 카운터를 비운다.
>
> ⚠️ **테스트에서 `db_session.expire_all()`을 함부로 부르지 마라.** 이미 돌려받은 ORM 객체까지 만료돼
> 다음 속성 접근이 동기 IO를 시도하다 `MissingGreenlet`으로 죽는다. 파이프라인은 다른 세션이지만 같은
> 커넥션을 공유하므로 **새 행은 그냥 보인다**. 만료가 필요한 건 테스트 세션이 직접 적재한 객체뿐이다.

```
@prompts/AUTORUN.md @prompts/04-review-workflow.md

두 문서를 읽고 M4 확인 워크플로를 실행해줘.

AUTORUN.md가 실행 규약이다. 특히 §3 "반드시 질문해야 하는 상황"을 지켜줘 —
사양이 갈리거나, 실측이 문서를 뒤집거나, 외부 계정·비가역 작업이 필요하거나,
DoD가 안 닫히거나, 스코프가 애매하면 추측하지 말고 AskUserQuestion으로 물어봐.
반대로 §4에 있는 것들(문서에 답이 있는 것, 관례적 판단)은 묻지 말고 그냥 진행해.
M4도 데모의 심장이라 위임하지 말고 직접 해줘(§5).

M0~M3은 이미 끝났다. questions/answers/answer_citations/official_qas 테이블과
질문 파이프라인(①~⑧)이 돌아간다. ⑦ 구조화 결과는 answers.question_struct에 이미
저장돼 있으니 카드로 복사만 해라 — 다시 생성하지 마라.
만료·피드백 상태 가드는 services/answer_service.py에 있다(부르기만 하면 된다).
카드 생성 지점은 services/pipeline/answer.py의 TODO(M4) 주석에 표시돼 있다.

⚠️ answers ↔ official_qas는 순환 FK다. 모델의 use_alter=True를 지우면
create_all이 CircularDependencyError로 죽어 테스트가 통째로 멈춘다.
⚠️ 유사도는 1 - (embedding <=> :q)다. <=>는 거리다.
⚠️ 확정 ko 원문은 재번역 금지(D5). 재사용 답변은 카드를 만들지 않는다(D11).

M4 DoD(시나리오 B 전 구간: 질문→🔴→수정→확정→재질문 즉답)를 하나씩 확인하고,
06 §5 테스트 7의 e2e(만료 답변 카드 처리 → 409)까지 닫고,
통과하면 커밋하고, START-HERE.md 세션표를 갱신한 뒤 보고하고 멈춰줘.
M5는 새 세션에서 이어간다.
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
