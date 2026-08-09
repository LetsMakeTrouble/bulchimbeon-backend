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
| 12 | **클라우드 배포 + 리허설** — **✅ 완료 2026-08-09** | 체크리스트 전 항목 통과 · 시나리오 A·B 재현 · 🟢 표본 37건 | ⛔ 불가 |

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

> ## ✅ M9 완료. 데모는 지금 바로 시연 가능한 상태다.
>
> **배포 URL**: `https://bulchimbeon-api-production.up.railway.app`
> **데모 프로젝트**: `1ea9e9da-0d86-4d8c-878b-2163c43178a4` (GlobalMart JP Launch)
> ⚠️ `--reset` 은 프로젝트를 지우고 새로 만든다 — **재시드할 때마다 ID 가 바뀐다.**
> `docs/09-deploy-notes.md` 에 URL·환경변수·시드 방법·롤백·확인 결과가 전부 있다. **먼저 읽어라.**
>
> ### 지금 상태 — 손대지 않아도 시연된다
> - 체크리스트 **전 항목 통과** (`09 §7`)
> - **시나리오 A** Q2 → 🟢 + 인용 / **시나리오 B** Q8 → 🔴 `no_evidence` 재현 확인
> - **Q8·Q10·Q2 원문은 아직 소진되지 않았다** — 라이브 시연 경로가 살아 있다
>   *(Q7 도 `conflict` 🔴 로 보존 — 발표 시각 DND 확인에 쓸 질문이다)*
> - 지표: 자동응답률 **0.7236**(목표 0.7) · 🟢 **0.8125**(표본 32) ·
>   🟡 **0.8947**(표본 57) · 🔴 **0.4412**(표본 34) · 절약 대기시간 2136시간
> - **LLM 은 gpt-5.6 단계별 배정**이다 (`03 §4.1`) — 검증·재사용판정 `sol` /
>   답변·구조화·확정문번역 `terra` / 질문번역·교훈 `luna` · `reasoning_effort=low`
>
> ### ⛔ 발표 전에 반드시 할 것
> **리허설에서 Q8 을 확정(edit)했다면 재시드하라.** 확정하면 공식 Q&A 가 생겨
> 데모 당일 Q8 이 재사용 경로로 빠지고 **시나리오 B 가 통째로 사라진다.**
> 카드 상세(`GET /review-cards/{id}`)를 열어도 `first_viewed_at` 이 오염된다(M7 지표).
> ```bash
> railway ssh --service bulchimbeon-api -i ~/.ssh/id_ed25519 \
>   'su appuser -s /bin/sh -c "cd /app && /app/.venv/bin/python scripts/seed.py --reset --with-history"'
> ```
> 123건이라 **25~30분** 걸린다. 발표 직전에 시작하지 마라.
>
> ### ⚠️ 🟢 표본에 여유가 2건뿐이다
> 같은 질문셋 123건으로 실행마다 37 → 34 → 32 였다. 기준은 30 이다. 재시드에서 30 밑으로
> 떨어지면 **계열별 발행률을 먼저 보고**(`09 §7.2`) Q6·Q13 에만 변형을 더한다 —
> Q1·Q3·Q12 는 정교한 질문을 28건 넣어도 🟢 이 0건이었다.
>
> ### 남은 것 (셋 다 외부 입력 대기)
> 1. **CORS 프론트 도메인** — 아직 미정. 현재 `localhost:3000` 만 열려 있어 그 외 오리진은
>    전부 400 이다. 도메인 받으면 `railway variables --set "CORS_ORIGINS=..."` 로 끝난다
>    (재빌드 불필요).
> 2. **로컬 커밋 푸시** — `origin/main` 보다 앞서 있다(사용자 결정으로 보류 중).
> 3. **발표 시각 확정 시 Q7 재확인** — DND 는 **KST 11:00~20:00** 이라 한국 낮 발표는
>    거의 DND 안이다. 강제 🔴 유지는 실측 확인했지만(`09 §7.3`), 확정 시각에 한 번 더 던져라.
>
> ### ⚠️ 다음 사람이 반드시 알아야 할 것
> - **🟢 표본을 다시 늘려야 하면 계열별 발행률부터 봐라** (`09 §7.2`). 질문을 잘 쓰는
>   것으로는 안 된다 — Q1·Q3·Q12 에 정교한 질문 28건을 넣어도 🟢 이 **0건**이었고,
>   Q6 은 80% 가 🟢 이었다. 섹션이 임베딩 공간에서 분리되는 정도가 결정한다.
> - **`08 §3` 기대표와 실 LLM 결과는 여러 건 어긋난다.** FakeLLM 회귀 테스트는 초록이다 —
>   표는 FakeLLM 결정론 기준이고, 실 LLM 편차를 `eval_questions.py` 가 보고하는 것이 정상이다.
> - **Railway 함정 3개** (`09 §2`·§4·§5): `startCommand` 의 `sh -c '...'` 는 벗기면 안 뜬다 /
>   볼륨을 새로 만들면 `chown appuser:appuser /app/storage` 1회 필요 /
>   시드는 `railway run` 이 아니라 `railway ssh` + `su appuser`.
> - **Railway SSH 검증 서비스가 간헐적으로 죽는다.** "can't verify your SSH key" 는 키 문제가
>   아니라 Railway 쪽 일시 장애다. 1분 뒤 재시도하면 붙는다.

```
@prompts/AUTORUN.md @docs/09-deploy-notes.md

M9 는 끝났다. 배포·시드·체크리스트·시나리오 재현 전부 완료 상태다.
docs/09-deploy-notes.md 를 먼저 읽고 현재 상태를 파악해줘.

지금 필요한 게 있으면 말해줄 테니, 없으면 아래만 확인해줘:
1. 클라우드 URL /health 와 데모 시나리오 A·B 가 여전히 재현되는지
2. Q8·Q10·Q2 원문이 아직 소진되지 않았는지 (라이브 시연 경로 보존 확인)
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
