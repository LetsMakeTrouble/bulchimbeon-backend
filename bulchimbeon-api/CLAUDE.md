# 불침번 (Bulchimbeon) — 백엔드 API

시차가 큰 글로벌 팀을 위한 비동기 Q&A 협업 서비스의 **FastAPI 백엔드**.
AI가 프로젝트 문서를 근거로 🟢즉답/🟡확인대기/🔴보류로 분기하고, 담당자 확인을 거쳐 지식으로 환류한다.
프론트엔드는 별도 팀 — `docs/05-api-contract.md`가 프론트와의 계약이다.

## 설계 문서 (구현 전 반드시 해당 문서 참조)

- `docs/02-business-rules.md` — **동작 규칙의 최우선 기준. 충돌 시 항상 이 문서가 이긴다**
- `docs/05-api-contract.md` — API 계약 (경로·스키마·에러코드를 여기 맞춘다)
- `docs/04-data-model.md` — 테이블·상태 전이 / `docs/06-ai-pipeline.md` — 파이프라인 단계
- `docs/03-tech-spec.md` — 폴더 구조·환경변수 / `docs/07-build-plan.md` — 마일스톤·DoD
- ⛔ **M0 착수 전 `prompts/00-calibration.md`(M-1 캘리브레이션 게이트, 15분)를 먼저 실행한다 — 절대 컷 불가**(`docs/07-build-plan.md` §컷라인).

## 스택 · 명령어

Python 3.12 + uv / FastAPI (네이티브 SSE) / SQLAlchemy 2.0 async + asyncpg / Alembic / PostgreSQL 16 + pgvector / OpenAI / APScheduler

```bash
uv sync                                            # 의존성 설치
docker compose up -d db                            # DB만 기동
uv run alembic upgrade head                        # 마이그레이션
uv run uvicorn app.main:app --reload --workers 1   # 개발 서버 (:8000)
uv run pytest                                      # 테스트
uv run ruff check . && uv run ruff format .        # 린트·포맷
uv run python scripts/seed.py --reset              # 데모 시드
```

## 아키텍처 규칙 (위반 금지)

1. **라우터는 얇게** — 요청 검증·권한 확인·서비스 호출만. 비즈니스 로직은 `app/services/`.
2. **LLM 호출은 `app/services/llm/` 프로바이더 인터페이스 경유만.** 라우터·서비스에서 openai SDK 직접 import 금지. 테스트는 `FakeLLMProvider` 사용 — 실 API 호출하는 테스트 금지.
3. **임계값(80/50/60/`s_floor`/`s_ceil`/`similarity_floor`/`reuse_threshold`/`similar_threshold`/72h/30개/`daily_llm_call_limit` 등) 하드코딩 금지** — 반드시 `projects.settings`에서 로드 (기본값은 `config.py`의 DEFAULT_SETTINGS 한 곳).
4. **모든 상태 변화는 `events` 테이블에 기록** — 지표·타임라인의 단일 원천.
5. **권한은 서버가 강제** — 담당자 전용/질문자 전용 액션은 의존성(`require_role`)으로 403. 프론트 신뢰 금지.
6. 답변 생성은 **근거 문서 내용만** 사용. 일반 상식 폴백 절대 금지 — 근거 없으면 🔴 보류가 정답이다.
7. 에러는 `{"error": {"code", "message"}}` 포맷 통일 (`app/core/errors.py`).
8. async 전면 사용. 동기 블로킹 호출(파일 파싱 등)은 `run_in_executor`.
9. **단일 프로세스 전제 — `--workers 1` 고정.** SSE 구독자 큐가 인메모리이고 APScheduler가 프로세스마다 중복 발화하므로 워커를 늘리면 깨진다. 중복 방지는 락이 아니라 제약으로(`briefing_runs`의 `UNIQUE(project_id, run_date)`).

## 코드 스타일

- 타입 힌트 필수, Pydantic v2 스키마는 `app/schemas/`에 계약서와 1:1.
- 주석·독스트링은 비즈니스 룰 번호를 인용 (예: `# 룰 4: 확정 ko 원문 재번역 금지`).
- 커밋: `feat(M3): ...` / `fix: ...` / `test: ...` — 마일스톤 태그 포함, 테스트 통과 후 커밋.

## 자주 틀리는 규칙 (구현 시 재확인)

- 매칭률은 **min(S, G)** — 평균 아님. **S는 `s_floor`~`s_ceil`로 리스케일한 값**이고, **G의 분모는 "생성 시점 원본 문장 수"로 고정**이다(프루닝해도 매칭률은 오르지 않는다).
- **pgvector `<=>`는 "거리"다.** 유사도는 `1 - (embedding <=> :q)`로 만든다. SQLAlchemy 헬퍼명도 `cosine_distance()` — 그대로 쓰면 등급이 뒤집힌다.
- 🟢 답변도 `draft`(참고)로 시작하고 **카드도 만든다**(`reason=green`). 단 **브리핑·알림에서는 제외**하고, "맞았다" 2건으로 `recommend_approve`가 될 때 브리핑에 등장한다. 자동 확정은 어떤 경로에도 없다.
- 재사용(≥`reuse_threshold`)은 확정 당시 **한국어 원문 그대로** — 재번역 금지. 재사용 답변만 `verified`로 시작하고 카드를 만들지 않는다.
- 🔴 답변은 질문자에게 발행하지 않지만 DB에는 초안으로 남겨 카드에 표시한다.
- DND 중 강등 대상은 **`low_confidence`뿐**이다. **강제 🔴 4종(`conflict`/`no_evidence`/`schema_failed`/`quota_exceeded`)은 DND에서도 🔴 유지** — 시각대 때문에 은폐되면 안 된다.
- **GPT-5 계열에 `temperature`·`top_p` 등을 전달하지 않는다**(400). `max_tokens` 대신 **`max_completion_tokens`**.
- **BackgroundTasks에는 UUID만 전달**한다. ORM 객체·`AsyncSession` 전달 금지 — 태스크 안에서 세션을 새로 연다.
- 질문 상태에 **`held → answered` 전이가 있다** (카드 approve/edit/answer-option으로 해소될 때. **`reject`는 `held`를 유지**하고 `card_status`만 `resolved`가 된다).
- 카드 중복 처리 = 409. 담당자 저장이 항상 우선, 미해소 피드백은 함께 resolved.
- 퇴근 모드 off / 담당자 교체 / 알림 실패 — 어떤 경우에도 **카드(인박스)는 사라지지 않는다**.
