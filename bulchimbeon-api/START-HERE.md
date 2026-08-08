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
| 7 | M5 알림·SSE | `@prompts/05-notifications-sse.md 실행해줘` | `answer.completed`만 필수 |
| 8 | **클라우드 배포 1차** | `@prompts/09-seed-deploy.md 의 배포 부분만 먼저 실행해줘` | ⛔ 불가 |
| 9 | M6 브리핑·교훈 | `@prompts/06-briefing-lessons.md 실행해줘` | 스케줄러·교훈 컷 가능 |
| 10 | M7 지표 | `@prompts/07-metrics-events.md 실행해줘` | 2종만 남기고 컷 가능 |
| 11 | M9 시드·리허설 | `@prompts/09-seed-deploy.md 실행해줘` | ⛔ 시드는 불가 |
| — | M8 외부 연동 | `@prompts/08-integrations.md 실행해줘` | **기본 제외** (여유 시만) |

**배포를 8번째 세션(6일차)로 앞당긴 것이 의도다.** Railway pgvector extension 권한·마이그레이션·SSE 프록시 같은 외부 마찰을 하루 먼저 노출시켜 복구 시간을 확보한다.

---

## 다음 세션 프롬프트 (그대로 복붙)

> ✅ **M4 확인 워크플로는 2026-08-08에 완료됐다** (`feat(M4): review workflow and knowledge loop`).
> `pytest` 269 passed + slow 1 passed, `ruff check`·`ruff format --check` 통과, 마이그레이션 head는 **0005**.
> `alembic check` = "No new upgrade operations detected" (모델 ↔ 마이그레이션 일치 확인).
>
> ⚠️ **`review_cards`에 컬럼이 하나 늘었다 — `resolved_by`** (`04 §2`에 반영, 사용자 승인).
> `05 §1.4`의 409 `ALREADY_RESOLVED` body가 `resolved_by: {id, name}`를 요구하는데 데이터 모델에
> 저장할 자리가 없었다. 담당자는 교체되므로(D16) "현재 담당자"로 대체할 수 없다.
>
> **M5가 그대로 물려받는 것** (다시 만들지 마라)
> - **알림을 붙일 자리는 전부 `TODO(M5)` 주석으로 표시돼 있다.** `grep -rn "TODO(M5)" app/` 하면
>   9곳이 나온다: 파이프라인 발행/실패(`pipeline/answer.py`), 피드백 `different`
>   (`feedback_service.py`), 재검토 연쇄 `doc.review_needed`(`review_cascade_service.py`),
>   D21 재사용 답변 알림·`answer.corrected`(`official_qa_service.py`), SSE 배선(`sse_manager.py`).
> - **`review_card_service.notifies_answerer(card)`** — 룰 1의 "🟢 카드는 알림 대상이 아니다"
>   판정이 이미 있다. 알림 코드에서 `reason == 'green'`을 다시 쓰지 마라. M6 브리핑도 이걸 쓴다.
> - **`dnd.next_briefing_at(now, timezone_name=, briefing_hour=)`** — `defer` 기본 만기(D15)가
>   쓰고 있다. M6 브리핑 스케줄러가 같은 함수를 써야 담당자 교체 시 어긋나지 않는다.
> - **만료 스위퍼(D14)의 조건 절반이 이미 있다** — `review_card.CARD_OPEN_STATUSES`
>   (`pending`·`deferred`)가 "살아 있는 카드"의 정의다. 스위퍼는
>   `state='draft' AND expires_at < now() AND 이 상태의 카드가 없을 것`이다.
> - **좀비 회수 잡(`06 §4`)은 실패 카드를 직접 만들지 마라** —
>   `review_card_service.create_card(reason=CARD_REASON_FAILED)`를 부른다. 파이프라인 총 실패
>   경로(`mark_question_failed`)가 이미 그 함수를 쓴다.
> - **확정 답변은 `expires_at`이 `None`이 된다**(`_confirm`). 스위퍼가 `draft`만 보므로 중복
>   방어이지만 `05 §6`의 확정 답변 예시가 `expires_at: null`이라 화면 계약이기도 하다.
>
> ⚠️ **카드·공식 Q&A 이벤트는 전부 질문 스코프다** (`entity_type='question'`, 카드/Q&A id는
> payload). `05 §13` 타임라인이 `?entity_type=question&entity_id=q-9`이기 때문이다.
> M7 지표(`card_handle_30s_rate`)는 `card.viewed` → `card.*`를 **`payload.card_id`로** 짝짓는다.
>
> ⚠️ **`different` 피드백은 살아 있는 카드가 있으면 새 카드를 만들지 않는다** (룰 9).
> 기존 카드의 `pending_feedbacks`로 붙고 담당자가 저장할 때 함께 해소된다. 알림은 그래도
> 보내야 한다 — "카드가 안 생겼으니 알릴 것도 없다"가 아니다.
>
> ⚠️ **재사용 답변은 원본의 사본이다** (D21). 공식 Q&A가 `under_review`로 내려가면 사본도
> 내려가고, 해소되면 사본의 **본문까지 원본의 현재 값으로 맞춘다**(`official_qa_service.restore`).
> 본문이 바뀐 사본의 질문자에게 `answer.corrected`를 보내는 것이 M5 몫이다.

```
@prompts/AUTORUN.md @prompts/05-notifications-sse.md

두 문서를 읽고 M5 알림·SSE를 실행해줘.

AUTORUN.md가 실행 규약이다. 특히 §3 "반드시 질문해야 하는 상황"을 지켜줘 —
사양이 갈리거나, 실측이 문서를 뒤집거나, 외부 계정·비가역 작업이 필요하거나,
DoD가 안 닫히거나, 스코프가 애매하면 추측하지 말고 AskUserQuestion으로 물어봐.
반대로 §4에 있는 것들(문서에 답이 있는 것, 관례적 판단)은 묻지 말고 그냥 진행해.

M0~M4는 이미 끝났다. 카드 큐·피드백·공식 Q&A·재검토 연쇄가 돌아간다.
알림을 붙일 자리는 grep -rn "TODO(M5)" app/ 로 전부 나온다 — 그 자리에 넣어라.
🟢 카드 알림 제외 판정은 review_card_service.notifies_answerer 가 이미 한다.
브리핑 시각 계산은 dnd.next_briefing_at 을 써라(M6와 공유한다).
좀비 회수 잡의 실패 카드는 review_card_service.create_card 로 만든다.

⚠️ --workers 1 고정. SSE 큐가 인메모리고 APScheduler가 워커마다 중복 발화한다.
⚠️ 만료 스위퍼는 살아 있는 카드(pending·deferred) 밑의 답변을 죽이지 않는다(D14).
⚠️ DND 강등 대상은 low_confidence 뿐이다. 강제 🔴 4종은 DND에서도 🔴 유지(D2).
⚠️ 알림 title/body는 수신자 users.language로 서버가 만든다(05 §1.5).

M5 DoD(SSE 이벤트 수신 테스트, DND 강등 테스트 — 강제 🔴 4종 비강등 포함)를
하나씩 확인하고, 통과하면 커밋하고, START-HERE.md 세션표를 갱신한 뒤
보고하고 멈춰줘. 다음은 클라우드 배포 1차다(새 세션).
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
