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
| 2 | M1 인증·프로젝트 | `@prompts/01-auth-projects.md 실행해줘` | ⛔ 불가 |
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

> ✅ **M0 스캐폴딩은 2026-08-07에 완료됐다** (`chore(M0): scaffold`). DoD 5/5 실측 통과.
> 빈 볼륨에서 `compose up -d db` → `alembic upgrade head` → `uvicorn --workers 1` → `/health` ok 재현 확인,
> `pytest` 15 passed, `ruff check`·`ruff format --check` 통과. 배포 이미지 빌드·기동까지 선검증했다.
>
> **M1이 시작하자마자 해야 할 것** — `app/models/`를 만들면 **두 곳에 import를 추가**한다:
> `alembic/env.py`(주석 자리표시자)와 `tests/conftest.py`의 `Base.metadata.create_all` 경로.
> 빠뜨리면 `alembic revision --autogenerate`가 **에러 없이 빈 리비전**을 뱉고 테스트도 초록이다.

```
@prompts/AUTORUN.md @prompts/01-auth-projects.md

두 문서를 읽고 M1 인증·프로젝트를 실행해줘.

AUTORUN.md가 실행 규약이다. 특히 §3 "반드시 질문해야 하는 상황"을 지켜줘 —
사양이 갈리거나, 실측이 문서를 뒤집거나, 외부 계정·비가역 작업이 필요하거나,
DoD가 안 닫히거나, 스코프가 애매하면 추측하지 말고 AskUserQuestion으로 물어봐.
반대로 §4에 있는 것들(문서에 답이 있는 것, 관례적 판단)은 묻지 말고 그냥 진행해.

M0은 이미 끝났다. 임계값은 config.py의 DEFAULT_SETTINGS에만 있고 에러 코드는
core/errors.py에 05 §1.4대로 들어 있다 — 새로 만들지 말고 그대로 쓴다(룰 3, §7).
모델을 만들면 alembic/env.py와 tests/conftest.py 양쪽에 import를 추가해야 한다.

M1 DoD를 하나씩 확인하고, 통과하면 커밋하고, START-HERE.md 세션표를 갱신한 뒤
보고하고 멈춰줘. M2는 새 세션에서 이어간다.
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
