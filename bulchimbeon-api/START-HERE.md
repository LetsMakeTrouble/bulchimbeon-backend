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
| 11 | M9 시드 — **✅ 완료 2026-08-09** | ~~`@prompts/09-seed-deploy.md 의 시드 부분`~~ 작업 1·2·3, `feat(M9): seed data, demo question set, regression tests` | ⛔ 시드는 불가 |
| 12 | **클라우드 배포 + 리허설** — **🔶 대부분 완료 2026-08-09** | `feat(M9): cloud deploy, heading-free embedding, demo step-1 swap` · 남은 것은 아래 §다음 세션 | ⛔ 불가 |

> ### ⛔ 배포 세션이 **반드시** 해야 하는 것 — `--with-history` 실행
> M9 는 시드 **코드**까지만 했다. 질문 58건을 실제 LLM 으로 돌리는
> `uv run python scripts/seed.py --reset --with-history` 는 **아직 한 번도 실행되지 않았다**
> (사용자 결정 2026-08-09 — 로컬 DB 에 채워 봐야 배포 후 다시 채워야 하므로 미뤘다).
> 안 돌리면 `grade_accuracy` 가 전 등급 "표본 부족"으로 떠서 데모 6단계 화면이 빈다 (D25).
> **배포 DB 에 대고, 발표 전날에** 돌린다.
>
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

> ## 🔶 배포는 끝났다. 남은 것은 **지표 표본 결정 + 발표 전 재시드**다.
>
> **배포 URL**: `https://bulchimbeon-api-production.up.railway.app` (Railway `prolific-inspiration`)
> `docs/09-deploy-notes.md` 에 URL·환경변수·시드 방법·롤백·확인 결과가 전부 있다. **먼저 읽어라.**
>
> ### ✅ 이번 세션이 끝낸 것
> - Railway 배포 + 마이그레이션(0001→0010) + 볼륨 + 도메인
> - 체크리스트 대부분 통과 — `--workers 1` PID 실측 · **SSE 정확히 900초 끊김 → 재연결 200 +
>   `unread_count` 1회** · `x-accel-buffering: no` · CORS · pgvector 0.8.6 · 청크 22
> - **시나리오 A·B 전 구간 재현** (`09 §7.1` 에 증적). Q10 이 `official_qa.reuse_count=1` 로
>   확정 원문 그대로 재사용되는 것까지 확인했다.
> - **임베딩에서 헤딩 줄 제외** — M-1 임계값이 측정한 텍스트와 운영이 임베딩하는 텍스트를
>   일치시켰다. Q8 sim 0.4312 → 0.4112 로 내려가 **강제 🔴 이 복원**됐고 시나리오 B 가 살아났다.
> - **데모 1단계 Q1 → Q2** (사용자 결정). Q1 은 실측 66 🟡, Q2 는 84 🟢.
>
> ### ⛔ 남은 것 1 — 🟢 표본 16건 (기준 30건). **결정이 필요하다**
> `--reset --with-history` 는 돌렸다. 결과: 질문 58건 · 발행 42건 · **🟢 16 · 🟡 26 · 🔴 16**.
> D25 기준 미달로 시드가 **종료 코드 1**로 끝났고 `grade_accuracy` 가 전 등급 "표본 부족"이다.
>
> ⚠️ **"질문셋을 늘리고 다시"로는 닫히지 않는다.** 실측 🟢 발생률이 **27.6%** 라 30건을 채우려면
> 질문이 **110건쯤** 필요한데 `08 §5` 는 45~60건으로 못박혀 있고 테스트가 강제한다
> (`test_history_size_and_green_weighting`). 셋 중 하나를 **사용자에게 물어서** 정해라:
> 1. `08 §5` 상한을 올린다 (테스트도 함께) — 이력 재실행 약 20분
> 2. `accuracy_service.MIN_SAMPLE`(30) 을 낮춘다 — D25 변경
> 3. `grade_accuracy` 만 "표본 부족"으로 두고 발표한다 — 나머지 지표는 이미 나온다
>    (`auto_answer_rate` **0.719** / 목표 0.7 · `saved_wait_hours` 1104)
>
> ### ⛔ 남은 것 2 — **발표 전날 반드시 재시드**
> 리허설에서 Q8 을 확정해 **공식 Q&A 가 이미 생겼다**. 그대로 두면 데모 당일 Q8 이 재사용으로
> 빠져 시나리오 B 가 사라진다. 카드 상세를 호출해 `first_viewed_at` 도 오염됐다(M7
> `card_handle_30s_rate`). 둘 다 재시드로 해소된다:
> ```bash
> railway ssh --service bulchimbeon-api 'su appuser -s /bin/sh -c "cd /app && python scripts/seed.py --reset --with-history"'
> ```
>
> ### ⛔ 남은 것 3 — 체크리스트 미확인 2건
> - **발표 시각 DND 판정** — 발표 시각(KST)이 담당자 Mike(`America/New_York`)의 22:00~07:00 에
>   걸리는지 계산하고, **그 시각에 실제로 Q7·Q8 을 던져 🔴 이 유지되는지** 확인한다.
>   🟡 로 강등되면 D2 의 강제 🔴 예외가 구현 안 된 것이다.
> - **일일 LLM 한도 동작 · 토큰 마스킹 · 비밀값 로그 미출력**
>   ⚠️ `quota.py` 카운터는 **프로세스 메모리**다. 스크립트 실행은 서버 카운터와 공유되지 않으므로
>   한도 확인은 **API 서버에 대고** 해야 한다.
>
> ### ⚠️ 알아 둘 것 — 실측이 `08 §3` 기대표와 여러 건 어긋난다
> 실 LLM 12건 대조에서 **7/12 일치**. Q1(66)·Q3(53)·Q4(50)·Q11(G=67)·Q12(67) 가 🟡 인데
> 표는 🟢 을 기대한다. **FakeLLM 회귀 테스트는 초록이다** — 표는 FakeLLM 결정론 기준이라
> 그대로 두었다. 실 LLM 편차는 `eval_questions.py` 가 보고하는 것이 정상 동작이다.
> 헤딩 제거는 공짜가 아니었다 — Q3·Q4 는 헤딩이 곧 판별 신호였던 계열이라 **나빠졌다**.
>
> ### 🔧 Railway 함정 3개 (다시 밟지 마라 — `09 §2`·§4 에 상세)
> 1. **`startCommand` 를 `sh -c '...'` 로 감싼 것은 필수다.** Railway 가 `${...}` 를 셸보다 먼저
>    치환해 `${PORT:-8000}` 을 빈 문자열로 만든다. **로그를 한 줄도 안 남기고** 죽는다.
> 2. **볼륨은 `root:root` 로 마운트되는데 컨테이너는 `appuser` 로 돈다.** 볼륨을 새로 만들면
>    `chown appuser:appuser /app/storage` 1회 필요. 앱은 정상 기동하고 `/health` 도 200이라
>    업로드를 해봐야 드러난다.
> 3. **시드는 `railway run` 이 아니라 `railway ssh` 로, 그리고 `su appuser` 로 돌린다.**
>    `railway run` 은 로컬 실행이라 내부 DB 주소에 닿지 못한다. root 로 돌리면 업로드 파일이
>    root 소유가 된다.

```
@prompts/AUTORUN.md @docs/09-deploy-notes.md

배포는 끝났다 (`https://bulchimbeon-api-production.up.railway.app`).
START-HERE.md 의 "다음 세션 프롬프트" 를 읽고 남은 3가지를 마무리해줘.

1. ⭐ **🟢 표본 16건 문제** — 질문셋을 늘리는 것만으로는 08 §5 의 45~60 상한 안에서
   닫히지 않는다. AskUserQuestion 으로 선택지 3개를 물어서 정한 뒤 실행해줘.
2. **발표 시각 DND 판정** — 발표 시각을 물어보고, 그 시각에 Q7·Q8 을 실제로 던져
   🔴 이 유지되는지 확인.
3. **일일 LLM 한도 · 토큰 마스킹 · 비밀값 로그 미출력** 확인.

그리고 발표 전날에는 반드시 `--reset --with-history` 재시드다 —
리허설이 Q8 을 확정해서 공식 Q&A 가 이미 생겼다.
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
