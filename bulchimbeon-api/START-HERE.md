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
| 3 | M2 문서 인제스트 | `@prompts/02-documents-ingest.md 실행해줘` | PDF/DOCX만 컷 |
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

> ✅ **M1 인증·프로젝트는 2026-08-08에 완료됐다** (`feat(M1): auth, projects, members`). DoD 7/7.
> `pytest` 83 passed, `ruff check`·`ruff format --check` 통과, 마이그레이션 head는 **0002**.
> 실서버(`uvicorn --workers 1`) 스모크로 계약서 §2·§3 **17개 엔드포인트 + /health**를 실커밋 경로에서 확인했다.
>
> **M2가 그대로 물려받는 것** (다시 만들지 마라)
> - `app/models/__init__.py`가 모델 등록의 **유일한 지점**이다. `alembic/env.py`와 `tests/conftest.py`가
>   이제 개별 모델이 아니라 **패키지를 import** 하므로, 새 모델은 `app/models/__init__.py`에만 추가하면 된다.
>   (`tests/test_models.py`가 등록 누락을 잡는다.)
> - `app/core/deps.py` — `get_current_user` · `require_member` · `require_answerer` · **`require_asker`**(D17용, M3 대기).
> - `tests/helpers.py` — `create_actor` / `create_project` / `join_project` / `error_code`.
> - `app/services/event_service.py`의 `record_event`. 타입 문자열은 `04 §5` 목록에서만 고른다.
> - **트랜잭션 경계는 라우터**다. 서비스는 `flush()`까지만 하고 커밋은 라우터가 한다.
>
> ⚠️ **M2가 반드시 밟는 지뢰 — `document_versions`의 부분 UNIQUE `(document_id) WHERE is_active`**
> M1의 `project_members` 담당자 스왑에서 **실측으로 재확인했다**(`04 §7`). 금지된 단일 `UPDATE … CASE`로
> 바꿔 넣고 돌려 보니, **한 방향만 검사하는 테스트는 그대로 통과했고** 앞뒤로 교체하는 테스트만 `23505`를 냈다.
> 즉 DoD 문구대로 "교체 후 담당자 1명"만 확인하면 **잘못된 구현이 초록으로 통과한다.**
> 활성 버전 전환 테스트는 **낮은→높은·높은→낮은 양방향**을 반드시 함께 돌려라
> (M1 구현: `app/services/project_service.py:transfer_answerer`, 회귀 테스트:
> `tests/test_projects.py:test_transfer_answerer_survives_both_swap_directions`).
>
> ⚠️ **ORM 벌크 UPDATE의 identity map 함정** — `update()`를 `synchronize_session=False`로 돌리면 같은 요청 안에서
> 이미 세션에 올라온 객체가 **낡은 값 그대로** 남아, 직후 조회가 DB가 아니라 그 객체를 돌려준다.
> M1에서 실제로 "교체했는데 역할이 안 바뀐 것처럼 보이는" 버그가 났고 `synchronize_session="fetch"`로 고쳤다.

```
@prompts/AUTORUN.md @prompts/02-documents-ingest.md

두 문서를 읽고 M2 문서 인제스트를 실행해줘.

AUTORUN.md가 실행 규약이다. 특히 §3 "반드시 질문해야 하는 상황"을 지켜줘 —
사양이 갈리거나, 실측이 문서를 뒤집거나, 외부 계정·비가역 작업이 필요하거나,
DoD가 안 닫히거나, 스코프가 애매하면 추측하지 말고 AskUserQuestion으로 물어봐.
반대로 §4에 있는 것들(문서에 답이 있는 것, 관례적 판단)은 묻지 말고 그냥 진행해.

M0·M1은 이미 끝났다. 임계값은 config.py의 DEFAULT_SETTINGS에만 있고, 에러 코드는
core/errors.py에 05 §1.4대로 있고, 권한 의존성은 core/deps.py에 있다 — 새로 만들지
말고 그대로 쓴다(룰 3, §7). 새 모델은 app/models/__init__.py에만 추가하면 된다.

document_versions의 부분 UNIQUE (document_id) WHERE is_active 활성 전환은
04 §7의 2문 절차로 구현하고, 테스트는 반드시 양방향(낮은→높은·높은→낮은)으로 돌려줘.
한 방향만 돌리면 잘못된 단일 UPDATE 구현이 초록으로 통과한다 — M1에서 실측했다.

M2 DoD를 하나씩 확인하고, 통과하면 커밋하고, START-HERE.md 세션표를 갱신한 뒤
보고하고 멈춰줘. M3는 새 세션에서 이어간다.
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
- GPT-5 계열에 `temperature` 전달 금지(400). `max_tokens`가 아니라 `max_completion_tokens`.
- BackgroundTasks에는 **UUID만** 전달. ORM 객체·세션을 넘기면 질문이 `processing`에 영구 정지한다.
- `--workers 1` 고정. SSE 큐가 인메모리고 APScheduler가 워커마다 중복 발화한다.
- strict Structured Outputs는 **기본값 있는 필드 금지**. `conflict_chunk_ids: []`가 400의 직접 원인.
- 부분 UNIQUE는 DEFERRABLE 불가 — 활성 버전·담당자 스왑은 한 트랜잭션 안에서 **2문으로 순서를 지켜** 처리.
  ⚠️ 단일 UPDATE는 **행 순서에 따라 통과하기도 한다**(M-1 실측). 항상 터지는 게 아니라서 테스트가 초록인 채로 운영에서 간헐 실패한다 — `04 §7`.
