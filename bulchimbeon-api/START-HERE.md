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
| 12 | **클라우드 배포 + 리허설** | `@prompts/09-seed-deploy.md 의 배포·리허설 부분` | ⛔ 불가 |

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

> ✅ **M9 시드는 2026-08-09에 완료됐다** (`feat(M9): seed data, demo question set, regression tests`).
> `pytest` **482 passed**, `ruff check`·`ruff format --check` 통과. 마이그레이션 없음(head는 여전히 **0010**).
> `09` 작업 **1·2·3**(시드 파일·`seed.py`·회귀 테스트+`eval_questions.py`)까지가 이번 범위였고,
> **4·5번(배포·리허설)이 이번 세션이다.**
>
> ### ⛔ 배포 세션이 반드시 해야 하는 것 — **`--with-history`가 아직 한 번도 실행되지 않았다**
> `scripts/seed.py --with-history`는 코드만 완성됐고 **실 LLM으로 돌린 적이 없다**
> (사용자 결정 2026-08-09 — 로컬에 채워 봐야 배포 후 다시 채워야 하므로 미뤘다).
> FakeLLM으로 전 경로를 한 번 돌려 스크립트 자체는 검증했다.
>
> **배포 DB에 대고, 발표 전날에** 아래 순서로 실행한다:
> ```bash
> set -a; . ./.env; set +a
> uv run python scripts/seed.py --reset --with-history   # 58건 · 약 175~235 LLM 호출
> uv run python scripts/eval_questions.py                # 12건 대조 · 약 36~48 호출
> ```
> - `--with-history`는 🟢 발행이 **30건 미만이면 종료 코드 1**로 끝나고 이유를 찍는다.
>   그때는 `scripts/demo_questions.py`의 🟢 계열 변형을 늘리고 다시 채운다.
> - **안 돌리면 `grade_accuracy`가 전 등급 "표본 부족"으로 떠서 데모 6단계 화면이 빈다**(D25).
> - `eval_questions.py`는 측정 후 자기가 만든 질문을 **원상복구**한다(`--keep`으로 남길 수 있다).
>   Q10은 일부러 건너뛴다 — 확정을 스크립트가 대신하면 일본 환불 Q&A가 미리 생겨
>   **시나리오 B가 통째로 사라진다.**
>
> ### ⚠️ 로컬 DB의 시드는 **FakeLLM 임베딩**이다
> 지금 로컬에 있는 `GlobalMart JP Launch` 프로젝트는 무료 점검용으로 fake 프로바이더로 만들었다.
> 청크 22개는 맞지만 **임베딩이 해시라서 검색이 무의미하다.** 실 프로바이더로 다시 시드해야
> `eval_questions.py`가 의미를 갖는다.
>
> ### ⚠️ M9가 **구현을 고친 것 하나** — 청커
> `08 §2`는 "`##` 섹션 하나가 청크 하나 = 총 22청크"라고 적었지만 실측은 **26청크**였다.
> 초과분 4개는 파일 맨 위 제목 줄(`# Refund Policy v1` 18자)만 든 빈 청크였다.
> **사용자 결정 2026-08-08로 청커를 고쳤다** — `utils/chunking._has_prose`가 헤딩 밖에 아무것도
> 없는 섹션을 버린다. 이제 정확히 22청크이고 문서는 손대지 않았다.
> ⚠️ **대가가 하나 있다**: 버려진 제목이 `heading_path`에 남는 건 그것이 **조상일 때뿐**이다.
> 본문 없는 `## Japan` 다음에 형제 `## Korea`가 오면 `Japan`은 통째로 사라진다
> (`del heading_stack[level - 1:]` 때문). 시드 4문서는 본문 없는 섹션이 H1 넷뿐이고 전부
> 조상이라 해당 없다. `tests/test_ingest.py::test_heading_only_sibling_disappears_entirely`가
> 그 경계를 못박아 두었다.
>
> ### ⚠️ M9가 **테스트를 고친 것** — 하루 9시간 동안만 깨지던 CI
> M8 커밋 상태에서 `test_pipeline` 2건 + `test_sync_github` 1건이 **04:14 UTC에 실패**했다.
> 원인은 코드가 아니라 테스트다: 담당자 타임존(UTC) 기준 기본 DND `22:00~07:00` 안에서 돌면
> (a) `low_confidence` 🔴이 🟡로 강등되고(룰 6·D2) (b) 알림이 `deliver_after`로 보류돼
> 알림함에서 사라진다. **CI는 하루 중 아무 때나 돌므로 9/24 확률로 빨간불이었다.**
> M8 커밋을 워크트리로 꺼내 같은 3건이 그대로 실패하는 것을 확인했다(내 변경 탓이 아니다).
> 조치: `tests/helpers.close_dnd_window`를 만들어 `build_team`·`test_pipeline` 픽스처가 DND를
> 꺼 두고, **DND 동작 자체를 보는 테스트는 자기 창을 명시적으로 세운다.**
> M5의 `notification_helpers`가 같은 함정을 이미 그렇게 피하고 있었다.
>
> ### M9가 남긴 것 (배포 세션이 그대로 쓴다)
> - **`scripts/demo_questions.py`가 질문셋의 단일 원천**이다. `seed.py`·`eval_questions.py`·
>   `tests/test_demo_scenarios.py` 셋이 같은 표를 import 한다. 질문 문안을 세 곳에 복붙하면
>   한쪽만 고쳐진 채 회귀 테스트가 초록으로 통과한다.
> - **이력 58건 구성**: 🟢 기대 49건 + 🔴 기대 9건. M-1 실측 S가 높은 계열(Q2 88·Q4 90·
>   Q6 100·Q11 91·Q13 91)에 변형을 8건씩 몰아 두었다. Q1(65)·Q3(56)·Q5(54)·Q12(66)는
>   정답 청크를 top-1으로 잡고도 매칭률이 S에 막혀 🟡이 될 수 있어 변형을 적게 뒀다.
> - **Q8·Q10 원문은 이력에 없고, Q1·Q8·Q10 계열은 승인 주입에서 통째로 제외**된다(`09 §2`).
>   `assert_live_questions_not_reusable`이 ① 번역문 임베딩으로 최근접 공식 Q&A를 조회해
>   `similar_threshold` 미만인지 확인하는 **2차 안전망**이고, 뚫렸을 때 실제로 잡는지는
>   `tests/test_seed.py`가 검증한다.
> - **시드는 카드 상세를 부르지 않는다** — `resolve_card`만 쓰므로 `first_viewed_at`이
>   오염되지 않는다(M7 `card_handle_30s_rate` 보호).
> - **시드는 연동(integrations)을 만들지 않는다** (M8이 정한 대로).
>
> 🔧 **M9가 남긴 알려진 미해결 1건 — ⭐ 배포 세션이 가장 먼저 확인할 것**
> - **M-1 임계값은 운영이 실제로 임베딩하는 것과 *다른 텍스트*로 측정됐다.**
>   `scripts/probe_calibration.py`는 `embed([c["text"] for c in SEED_CHUNKS])`(919행)로
>   **헤딩 줄이 빠진 본문만** 임베딩한다. 반면 운영 청커는 헤딩 줄을 청크 본문 앞에 남기므로
>   (`06 §2` ④ EVIDENCE 포맷의 요구다) `chunks.content`는 `## Sandbox\n...`로 시작하고
>   인제스트는 그 전체를 임베딩한다. 즉 **`s_floor 0.25`·`s_ceil 0.679`·`similarity_floor
>   0.423`은 헤딩 없는 텍스트로 뽑은 값**이다.
>   - 청크 **개수**(22)와 **본문 텍스트**는 이제 일치한다. 어긋난 건 헤딩 줄 유무와
>     헤딩 라벨 2건(`Partner Sync Notes - July 2026` vs 실제 `— July 2026`)뿐이다.
>   - 파급: `08 §4` 1단계 대사 **"🟢 87%"**와 `08 §3` M-1 대조표(Q8 0.4202 / Q9 0.3645)가
>     실측과 어긋날 수 있다. Q8은 차단선과 0.003 차이라 특히 민감하다.
>   - **조치: 배포 후 `eval_questions.py`를 `--with-history`보다 먼저 돌려라**(12건이라 싸다).
>     어긋나면 (a) probe의 `SEED_CHUNKS[i]["text"]`에 헤딩 줄을 붙여 재측정하거나
>     (b) `08 §3` 대조표를 실측으로 갱신한다. **M9에서 고치지 않은 이유**는 M-1 판정 표와의
>     대조 기준선이 흔들리고, 임계값 재산출은 실 API 재측정이 필요해 시드 범위를 넘기 때문이다.
>   - 이 결함은 M9가 만든 것이 아니다(M-1/M2 유래). M9의 `eval_questions.py`가 그것을
>     드러내는 도구다.

```
@prompts/AUTORUN.md @prompts/09-seed-deploy.md

두 문서를 읽고 M9 중 **배포·리허설(작업 4·5번)** 을 실행해줘. 시드(1·2·3)는 끝났다.

AUTORUN.md가 실행 규약이다. 특히 §3.3 "외부·비가역·비용" 을 지켜줘 —
배포, 원격 푸시, 파괴적 마이그레이션은 **실행 전에 확인받는다**.

⚠️ 09-seed-deploy.md 상단의 "배포는 6일차로 앞당긴다"는 **이미 뒤집혔다** —
   사용자 결정 2026-08-08 로 개발 완주 후 배포다 (위 세션표 참조).

## 이번 세션의 순서

1. **배포** (09 작업 4번) — Railway/Render
   - 시작 커맨드에 `--workers 1` 이 실제로 들어갔는지 대시보드에서 눈으로 확인
   - `alembic upgrade head` 선행 (head=0010)
   - `CREATE EXTENSION vector` 권한 → `SELECT extversion FROM pg_extension WHERE extname='vector'`
     가 0.8.0 이상인지 **배포 DB에서** 확인 (M-1은 로컬만 봤다)
   - `.env` 체크리스트 출력 — ⭐ `INTEGRATION_ENCRYPTION_KEY` 를 빠뜨리면 M8 연동이
     복호화에 실패해 다시 등록해야 한다
   - `STORAGE_DIR` 볼륨이 재시작을 넘어 살아남는지 (업로드가 사라지면 근거가 빈다)
   - `docs/09-deploy-notes.md` 작성 (URL·시드 실행 방법·롤백)

2. **기본 시드** — 배포 DB에 대고 `uv run python scripts/seed.py --reset`
   (문서 4개 + 인제스트만. 이력은 아직 채우지 않는다)

3. ⭐ **실 LLM 대조를 이력보다 먼저** — `uv run python scripts/eval_questions.py`
   → M-1 `probe_calibration.py` 판정 1 표와 나란히 놓고 임계값이 아직 유효한지 본다.
   위의 '알려진 미해결 1건'(헤딩 줄) 때문에 **`--with-history`보다 먼저** 돌려라 —
   12건이라 싸고, 여기서 어긋나면 58건을 채우기 전에 임계값이나 대조표를 먼저 정리해야 한다.

4. **이력 주입** — `uv run python scripts/seed.py --reset --with-history`
   → 🟢 발행 30건 미만이면 종료 코드 1이다. 그 경우 질문셋을 늘리고 다시.

5. **최종 점검 체크리스트** (09 작업 5번) 전 항목을 결과와 함께 보고.
   특히 **SSE를 16분 이상 열어 두고** 재연결·`notification.unread_count` 1회 수신 확인.
   짧게 열어보고 넘기면 발표 중에 조용히 죽는다.

6. **시나리오 A·B 재현 증적**(응답 JSON) 제시 후 커밋.

⚠️ Q1·Q8·Q10 원문은 라이브 시연용이다. 리허설에서 Q8을 확정(edit)해 버리면
   공식 Q&A가 생겨 **데모 당일 시나리오 B가 재사용으로 빠진다.** 리허설로 확정했다면
   발표 전에 `--reset --with-history` 로 다시 채워라.

DoD를 하나씩 확인하고, 통과하면 커밋하고, START-HERE.md 세션표를 갱신한 뒤 보고해줘.
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
