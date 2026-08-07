# START HERE — 세션별 복붙 프롬프트

> 이 레포는 `bulchimbeon` 킷을 복사한 **구현 레포**다. 원본 킷(`../bulchimbeon`)은 참조용으로 보존한다.
> **프롬프트 1개 = Claude Code 세션 1개.** 완료·커밋 후 `/clear` 하고 다음으로 넘어간다.

## 실행 전 1회

```powershell
$env:OPENAI_API_KEY = "sk-..."
```

M-1 캘리브레이션은 **실 임베딩 호출이 필수**라 FakeLLM으로 대체 불가하다. 키가 없으면 시작할 수 없다.

---

## 세션 순서

| # | 세션 | 프롬프트 | 컷 가능? |
| --- | --- | --- | --- |
| 0 | **M-1 캘리브레이션 게이트** | `@prompts/00-calibration.md 실행해줘` | ⛔ 불가 |
| 1 | M0 스캐폴딩 | `@prompts/00-kickoff.md 실행해줘` | ⛔ 불가 |
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

## 첫 세션 프롬프트 (그대로 복붙)

```
@prompts/00-calibration.md 이 프롬프트를 실행해줘.

이건 M-1 캘리브레이션 게이트야. M0 스캐폴딩보다 먼저 실행해야 하고 절대 건너뛸 수 없어.
이유: 문서의 임계값 세트(s_floor 0.25 / s_ceil 0.65 / reuse_threshold 0.92 / similar_threshold 0.85)는
ada-002 시대 기준이라 text-embedding-3-small에서 성립하지 않을 수 있는 잠정값이야.
실측 없이 M3를 시작하면 잘못된 임계값 위에 테스트 7종을 작성하게 되고,
FakeLLM 테스트를 전부 통과한 채로 M9 클라우드 데모에서 처음 터져.

산출된 실측값을 docs/04 §3 · docs/05 §3 · docs/08 §1 설정값 표와
02:30 · 02:95 · 02:98 · 02:101 · 06:63 · 06:96 · 06:109 의 인용 기본값까지 전부 반영하고,
프롬프트의 DoD 체크리스트를 하나씩 확인해줘.
```

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
