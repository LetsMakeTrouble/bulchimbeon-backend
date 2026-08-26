# 불침번 (Bulchimbeon) — 백엔드

> [!IMPORTANT]
> **이 저장소는 2026-08-27 에 아카이브되었습니다.** 더 이상 개발·배포되지 않으며 읽기 전용입니다.
> 마지막 동작 커밋은 2026-08-20 이고, 그 시점의 코드·설계 문서·측정값이 그대로 보존되어 있습니다.
> 클론해서 로컬에서 띄우는 것은 지금도 됩니다 (아래 [로컬에서 띄우기](#로컬에서-띄우기)).

시차가 큰 글로벌 팀을 위한 비동기 Q&A 협업 서비스 **불침번**의 API 서버입니다.

한국어 질문을 받아 영어 프로젝트 문서에서 근거를 찾고, **매칭률(0~100)** 을 계산해
🟢 즉답 · 🟡 확인 대기 · 🔴 보류로 나눕니다. 확신이 부족하면 지어내지 않고 담당자에게 넘기고,
담당자가 확정한 답은 공식 Q&A 로 편입되어 다음 질문에 재사용됩니다.

제품 전체 이야기는 [`bulchimbeon-infra`](https://github.com/LetsMakeTrouble/bulchimbeon-infra) 의 README 에 있습니다.

---

## 이 저장소에는 두 개가 들어 있습니다

```
bulchimbeon-backend/
├── bulchimbeon/          ← 설계 킷 — 문서와 프롬프트만 (코드 없음)
├── bulchimbeon-api/      ← 구현 — FastAPI 서버 본체
└── .github/workflows/    ← CI (working-directory 가 bulchimbeon-api/ 를 가리킵니다)
```

### [`bulchimbeon/`](bulchimbeon/) — 설계 킷

- 설계 문서 9개 — 기획서 · 비즈니스 룰 · 데이터 모델 · API 계약서 등
- 프롬프트 11개 — 그 문서를 근거로 마일스톤 단위 구현을 굴리는 세트
- 코드는 없습니다

📖 **"무엇을 왜 그렇게 정했나"** 를 볼 때 여기를 봅니다.

### [`bulchimbeon-api/`](bulchimbeon-api/) — 구현 레포

- 킷을 복사해 실제로 코드를 채운 쪽
- 문서 · 프롬프트도 함께 복사되어 있습니다
- 구현 과정에서 갱신된 것은 **이쪽이 최신**입니다

💻 **코드를 읽거나 돌릴 때** 여기를 봅니다.

### 왜 한 저장소에 둘 다 두었나요

사양(킷)과 그 사양이 실제로 무엇이 되었는지(구현)를 **같은 히스토리에서 대조할 수 있게**
하기 위해서입니다. 설계가 바뀐 지점은 `bulchimbeon-api/docs/` 와 `bulchimbeon/docs/` 의
차이로 남아 있습니다.

> 문서가 서로 어긋날 때의 우선순위: `02-business-rules` > `05-api-contract` > `06-ai-pipeline` > 나머지.

## 무엇이 만들어졌나요

| 항목 | 규모 |
|---|---|
| 애플리케이션 코드 | 약 18,400 줄 (`bulchimbeon-api/app/`) |
| 테스트 | 455개 · 약 14,400 줄 (`bulchimbeon-api/tests/`) |
| API 엔드포인트 | 61개 (전부 `/api/v1` 아래) |
| DB 마이그레이션 | 16개 (`alembic/versions/0001` ~ `0016`) |
| 설계 문서 | 10개 (`docs/00` ~ `09`) |

마일스톤 M-1(캘리브레이션) ~ M9(시드)와 클라우드 배포·리허설까지 전부 완료된 상태입니다.
각 단계의 진행 기록과 완료 근거는 [`bulchimbeon-api/START-HERE.md`](bulchimbeon-api/START-HERE.md) 에 있습니다.

## 스택

Python 3.12 · **FastAPI** · SQLAlchemy 2.0 (async) · Alembic · Pydantic v2 ·
**PostgreSQL 18 + pgvector 0.8.6** · OpenAI (프로바이더 추상화) · APScheduler · JWT · argon2 · uv

의존성 선택에는 이유가 붙어 있습니다 — 예를 들어 `sse-starlette` · `testcontainers` · `aiosqlite`
셋은 **의도적으로 넣지 않았습니다** (`bulchimbeon-api/pyproject.toml` 주석 참조).

## 핵심 설계 세 가지

### 1. 매칭률은 `min(S, G)` 입니다

- **S** — 근거를 얼마나 잘 찾았는지 (검색)
- **G** — 답이 그 근거 안에 머물렀는지 (접지)

평균이 아니라 최솟값입니다. 문서를 잘 찾아도 답이 문서를 벗어났으면 낮게 나와야 하니까요.

> 무너지면 안 되는 조건 셋 — `bulchimbeon-api/app/services/pipeline/grading.py` 상단 독스트링

### 2. 임계값의 정의 위치는 한 곳뿐입니다

- **기본값** — `app/config.py` 의 `DEFAULT_SETTINGS`
- **런타임 원천** — DB 의 `projects.settings`
- 숫자를 다른 파일에 복사하지 않습니다

값은 M-1 캘리브레이션에서 **실측으로** 정했습니다.

> 판정 근거 — `bulchimbeon-api/calibration-2026-08-07*.txt`

### 3. `--workers 1` 은 선택이 아닙니다

- SSE 구독자 큐가 인메모리
- APScheduler 가 프로세스마다 중복 발화

워커를 늘리면 에러 없이 **조용히** 깨집니다.

> 확장이 필요할 때의 정답 — `app/core/scheduler.py` 독스트링

## 로컬에서 띄우기

개발 표준은 **DB 만 컨테이너, 앱은 호스트**입니다. 두 모드를 섞지 않습니다.

```bash
cd bulchimbeon-api
cp .env.example .env              # OPENAI_API_KEY 등을 채웁니다
set -a; . ./.env; set +a
uv sync
docker compose up -d db           # pgvector/pgvector:pg18
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --workers 1 --port 8000
```

- Swagger UI — <http://localhost:8000/api/docs>
- 헬스체크 — `curl localhost:8000/api/health`

프론트까지 함께 띄우려면 이 저장소 대신 [`bulchimbeon-infra`](https://github.com/LetsMakeTrouble/bulchimbeon-infra) 를 클론하세요.

### 테스트

테스트는 떠 있는 Postgres 에 `TEST_DATABASE_URL` 로 붙습니다. 테스트 DB 와 `vector` 확장은
픽스처가 자동으로 만듭니다.

```bash
uv run pytest                     # 전체 455개
uv run pytest -m "not slow"       # 마이그레이션 검증 제외
uv run ruff check . && uv run ruff format --check .
```

## 더 읽을 거리

| 무엇 | 어디 |
|---|---|
| 제품 정의 · 확정 결정사항 | [`bulchimbeon-api/docs/00-project-brief.md`](bulchimbeon-api/docs/00-project-brief.md) |
| 동작 규칙 (충돌 시 최우선) | [`bulchimbeon-api/docs/02-business-rules.md`](bulchimbeon-api/docs/02-business-rules.md) |
| API 계약서 | [`bulchimbeon-api/docs/05-api-contract.md`](bulchimbeon-api/docs/05-api-contract.md) |
| RAG · 매칭률 · 등급 분기 | [`bulchimbeon-api/docs/06-ai-pipeline.md`](bulchimbeon-api/docs/06-ai-pipeline.md) |
| 배포 중 만난 문제들 | [`bulchimbeon-api/docs/09-deploy-notes.md`](bulchimbeon-api/docs/09-deploy-notes.md) |
| 에이전트 상시 지침 | [`bulchimbeon-api/CLAUDE.md`](bulchimbeon-api/CLAUDE.md) |

## 만든 사람

### 장태희 — 백엔드

> [@TaeHee00](https://github.com/TaeHee00)

**API 서버 본체를 처음부터 끝까지**

- M-1 캘리브레이션 게이트 ~ M9 시드, 마일스톤 열한 단계 완주
- 임계값 5종을 실 임베딩 호출로 실측해 확정

**운영 전환 3종**

- 동시 파이프라인 상한 — 15건이 몰리면 접수까지 막히던 문제
- LLM 사용량·비용 DB 적재 — 한도가 재시작을 견딤
- 예약 작업 벽시계 트리거 — 재배포 직후에도 돌게

**배포** — 클라우드 배포와 리허설

### 홍석영 — 팀장 · 설계

> [@Seokyoung-Hong](https://github.com/Seokyoung-Hong)

**설계 킷**

- 설계 문서 9종 (`docs/00` ~ `08`)
- 마일스톤 프롬프트 11종 (`prompts/`)

**검색 품질** — 구현이 돌기 시작한 뒤 파고든 영역

- 청킹 2차 분할 — 섹션을 문장 3개씩 창으로
- 임베딩을 `text-embedding-3-large` 로 교체
- 문서 언어를 판별해 검색 축을 언어별로 분리

**그 밖에**

- Swagger 문서 상세화 · 전 경로 `/api` 하위 이전
- 데모 프로필 · 이력 픽스처

---

팀 전체와 다른 저장소의 역할은 [조직 프로필](https://github.com/LetsMakeTrouble)에 정리해 두었습니다.

## 관련 저장소

- [`bulchimbeon-frontend`](https://github.com/LetsMakeTrouble/bulchimbeon-frontend) — React 웹 화면
- [`bulchimbeon-infra`](https://github.com/LetsMakeTrouble/bulchimbeon-infra) — 둘을 묶어 함께 띄우는 배포 설정
