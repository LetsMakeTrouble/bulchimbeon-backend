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
| 9 | M7 지표 | `@prompts/07-metrics-events.md 실행해줘` | 컷 없음(4종+정확도) |
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

> ✅ **M6 브리핑·교훈은 2026-08-08에 완료됐다** (`feat(M6): briefing scheduler and lesson memory`).
> `pytest` **356 passed**(slow 포함), `ruff check`·`ruff format --check` 통과,
> 마이그레이션 head는 **0007**. `alembic check` = "No new upgrade operations detected".
> `grep -rn "TODO(M6)" app/` → **0건**. 별도 리뷰 패스(`code-reviewer`+`verifier`) 통과.
>
> **M7이 그대로 물려받는 것** (다시 만들지 마라)
> - ⭐ **`metrics_service.auto_answer_rate(db, project_id=, window_days=)` 가 이미 있다.**
>   `05 §13`의 `auto_answer_rate`는 이것을 **그대로** 쓴다 — 두 벌 만들면 브리핑
>   `stats_snapshot`과 대시보드가 다른 숫자를 보여 준다. `grade_counts()`가 등급 분포
>   `{green, yellow, red}`를 주므로 §13의 `{value, target, green, yellow, red}`가 바로 나온다.
> - ⭐ **집계 원천은 `answers` 행이 아니라 `question.graded` 이벤트다** (룰 4 · `02 §10` ·
>   `04 §5`). M6가 처음에 `answers.grade`로 짰다가 리뷰에서 되돌렸다. **갈리는 지점이 실재한다**:
>   `reason='failed'` 카드에 `edit`하면 `_apply_edit`이 `grade=red`인 **새 Answer 행**을 만드는데
>   그 답변에는 `question.graded`가 없다. M7 작업 1(이벤트 감사)이 정확히 이런 구멍을 찾는 일이다.
> - **`accuracy_service.for_grade(db, project_id=, grade=, language=, window_days=)`** 가
>   등급별 실측 정확도(D25)의 유일한 구현이다. `05 §13` `grade_accuracy[]`는 이것을 등급마다
>   불러 만들고, 아이템 스키마는 `schemas/question.GradeAccuracy` 다(§6과 §13이 같은 shape).
> - **`lesson.candidate` / `lesson.approved` / `lesson.deleted` 이벤트는 이미 기록된다.**
>   `05 §13` timeseries의 `lessons_approved`가 여기 기댄다. `approve` 재호출 시 이벤트가
>   중복되지 않도록 상태 가드가 들어가 있다 — 없으면 학습 곡선이 부풀려진다.
> - **`review_card_service.to_list_item(...)`** 은 이제 public 이다. `05 §7` 큐 아이템과
>   `05 §8` 브리핑 네 배열이 같은 함수를 쓴다.
> - **`dnd.zone(timezone_name)` / `dnd.FALLBACK_TIMEZONE`** 이 public 이다. 깨진 타임존을
>   UTC로 떨어뜨리는 판정은 이 하나뿐이다 — 복제하면 DND와 브리핑이 다른 날짜를 본다.
> - **`briefing_dispatch_service`** 는 `now=`를 주입받는다. 잡 등록은 `core/scheduler.py`
>   (만료 스위퍼 10분 · 좀비 회수 5분 · 브리핑 60분).
>
> ⚠️ **이벤트·알림 타입·SSE 이벤트·`settings` 키는 전부 닫힌 집합이다.**
> `tests/test_notification_contract.py`가 `04 §4`와 `05 §12.3`을 **정규식으로 파싱해** 막고,
> `tests/test_config.py`가 `DEFAULT_SETTINGS` 16개를 고정한다. 새로 만들려면 문서를 먼저 고쳐야 한다.
>
> ⚠️ **브리핑 중복 발송은 락이 아니라 제약이다** — `briefing_runs`의 `UNIQUE(project_id, run_date)`
> + `ON CONFLICT DO NOTHING` → `rowcount == 1`일 때만 발송. `SELECT`로 "오늘 보냈나"를 먼저 보고
> 분기하는 코드를 넣지 마라(TOCTOU).
>
> ⚠️ **보류 알림 flush는 `deliver_after`를 `NULL`로 만든다.** 그게 "SSE 발행 완료" 마커다.
> 안 지우면 `deliver_after <= now()`가 영원히 참이라 매일 같은 알림에 SSE를 재발행한다.
> 가시성은 `_deliverable`이 이미 처리하므로 NULL화는 순수하게 발행 마커다.
>
> ⚠️ **`content_hash` 정규화 구현은 `app/utils/hashing.py` 하나뿐이다** (D8).
> 두 번째 구현이 생기면 "지운 교훈이 다시 올라온다"가 조용히 부활한다.
>
> 🔧 **M6가 남긴 알려진 미해결 2건** (커밋했고, 급하지 않다):
> - `dispatch_due_briefings`가 전 프로젝트를 **한 트랜잭션**에서 돌고 커밋은 잡이 한 번 한다.
>   한 프로젝트가 터지면 그 틱의 나머지도 롤백된다(다음 틱에 자가치유). 같은 함수에 N+1도 있다
>   (프로젝트마다 `db.get(User, ...)`). 프로젝트 수가 적어 지금은 문제가 아니다.
> - DND 때문에 건너뛴 브리핑은 **다음 날 `run_date`로 합쳐진다** — 그날 몫의 `briefing.ready`가
>   따로 나가지 않는다. 밀린 알림은 그때 함께 flush되므로 유실은 없다.
>   (`briefing_dispatch_service.py`에 근거가 주석으로 남아 있다.)

```
@prompts/AUTORUN.md @prompts/07-metrics-events.md

두 문서를 읽고 M7 이력 타임라인·지표를 실행해줘.

AUTORUN.md가 실행 규약이다. 특히 §3 "반드시 질문해야 하는 상황"을 지켜줘 —
사양이 갈리거나, 실측이 문서를 뒤집거나, DoD가 안 닫히거나, 스코프가 애매하면
추측하지 말고 AskUserQuestion으로 물어봐. 반대로 §4에 있는 것들(문서에 답이
있는 것, 관례적 판단)은 묻지 말고 그냥 진행해.

⚠️ 07-metrics-events.md 상단의 ✂️ 컷라인은 **적용하지 않는다.** 데드라인이
없으므로 지표 4종 + 등급별 정확도를 전부 구현한다 (AUTORUN §1).

M0~M6은 끝났다. 마이그레이션 head는 0007, pytest 356 passed다.
M6이 남겨 둔 접점 (다시 만들지 마라):
- auto_answer_rate 는 metrics_service 에 이미 있다. §13 은 그것을 그대로 쓴다.
  grade_counts() 가 {green, yellow, red} 를 준다.
- 집계 원천은 answers 행이 아니라 question.graded 이벤트다 (룰 4).
  M6 가 answers 로 짰다가 리뷰에서 되돌렸으니 다시 되돌리지 마라.
- grade_accuracy[] 는 accuracy_service.for_grade 를 등급마다 부른다.
- lesson.candidate/approved/deleted 이벤트는 이미 기록된다(시계열 학습 곡선용).

⚠️ 작업 1(이벤트 기록 감사)이 이 마일스톤에서 제일 중요하다. 이벤트가 비면
   지표를 나중에 되살릴 수 없다 — 과거 데이터는 소급 생성되지 않는다.
   실제 구멍 하나를 M6 이 이미 찾아 뒀다: reason='failed' 카드를 edit 하면
   grade=red 인 새 Answer 행이 생기는데 question.graded 이벤트가 없다.
⚠️ requestion_instant_rate 의 분모는 reused + reuse_missed 다 (D26).
   reuse_missed 를 빼면 재사용 1건만 일어나도 100% 가 되어 실패를 은폐한다.
⚠️ grade_accuracy 는 표본 30 미만이면 값 대신 sufficient:false + message 다.
   데모 규모에서 🟡·🔴 이 "표본 부족"으로 뜨는 건 버그가 아니라 D25 의 의도다.

M7 DoD를 하나씩 확인하고, 통과하면 커밋하고, START-HERE.md 세션표를 갱신한 뒤
보고하고 멈춰줘. 다음은 M8 외부 연동이다(새 세션).
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
