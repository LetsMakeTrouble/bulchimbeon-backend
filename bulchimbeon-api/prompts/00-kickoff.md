# M0 — 프로젝트 스캐폴딩

@docs/03-tech-spec.md @docs/07-build-plan.md 를 읽고 불침번 백엔드 레포를 스캐폴딩해줘.

> ⛔ **선행 조건**: `prompts/00-calibration.md`(M-1 캘리브레이션 게이트)가 완료되어
> `04 §3`의 ⚠️ 잠정값 표기가 사라진 상태여야 한다. 4번의 `DEFAULT_SETTINGS`는 그 실측값을 담는다.

## 작업

1. `uv init` 기반 `pyproject.toml` 작성 — Python 3.12
   - 런타임: **`fastapi>=0.135.0`**, `uvicorn[standard]`, `sqlalchemy[asyncio]`, `asyncpg`, `alembic`,
     `pydantic`, `pydantic-settings`, `pgvector`, `openai`, `apscheduler`,
     **`python-jose[cryptography]>=3.4.0`**, `pwdlib[argon2]`, `python-multipart`,
     `pypdf`, `python-docx`, `httpx`, `notion-client`, `cryptography`
   - dev: `pytest`, `pytest-asyncio`, `ruff`
   - ⚠️ **`sse-starlette`를 넣지 않는다.** FastAPI 0.135.0+ 네이티브 SSE(`fastapi.sse.EventSourceResponse`)를 쓴다.
     네이티브가 `X-Accel-Buffering: no` 헤더와 15초 ping을 자동 처리하므로 래퍼가 불필요하다 (`03 §1`).
     FastAPI의 `>=0.135.0` 하한 핀은 이것 때문이며 낮추면 안 된다.
   - ⚠️ `python-jose` **3.4.0 미만은 CVE-2024-33663 / CVE-2024-33664**다. 하한 핀 필수이며 "또는 pyjwt" 같은 양자택일을 두지 않는다.
   - ⚠️ **`testcontainers`·`aiosqlite`를 넣지 않는다.** 테스트 인프라는 7번에서 확정한다.
2. `docker-compose.yml` — `pgvector/pgvector:pg16` 이미지 DB + api 서비스, 볼륨·헬스체크 포함.
   **개발 표준은 `db`만 컨테이너**이고 `api` 서비스는 배포 이미지 검증용이다 (`03 §5.1`).
3. `Dockerfile`(uv 기반 멀티스테이지), `.env.example`(`03 §4` 그대로 — `TEST_DATABASE_URL`·`LLM_MODEL_VERIFY`·
   `LLM_REASONING_EFFORT`·`LLM_TIMEOUT_SECONDS`·`LLM_PIPELINE_DEADLINE_SECONDS` 포함), `.gitignore`,
   **`.gitattributes`에 `*.md text eol=lf`** — CRLF가 섞이면 시드 문서·교훈의 `content_hash`가 OS마다 달라져
   삭제 교훈 재생성 차단(D8)이 무력화된다 (`03 §5.2`)
4. `app/` 뼈대: `main.py`(앱 팩토리, CORS, 전역 예외 핸들러), `config.py`(pydantic-settings,
   `DEFAULT_SETTINGS` 딕셔너리 포함 — **`04 §3`의 16개 키 전부**), `database.py`(async engine/session),
   `core/errors.py`(`{"error":{"code","message"}}` 포맷 + AppError 계층)
   - `DEFAULT_SETTINGS`에 **M-1에서 새로 들어온 키를 빠뜨리지 말 것**:
     `s_floor` · `s_ceil` · `similarity_floor` · `daily_llm_call_limit` · `saved_wait_assumption_hours`
   - `retrieval_top_k`는 **6**이다(8 아님). **`briefing_timezone` 키는 존재하지 않는다** —
     브리핑·DND 시각 판정의 단일 원천은 담당자(`projects.answerer_id`)의 `users.timezone`이다.
   - `config.py` 기동 시 **`EMBEDDING_DIM != 1536`이면 fail-fast**. 이 env는 임베딩 응답 차원 검증과
     `dimensions` 파라미터 전달용이지 **컬럼 차원을 바꾸는 스위치가 아니다**.
   - `daily_llm_call_limit`은 **env가 아니라 `DEFAULT_SETTINGS`**에 둔다 (룰 1 일관성).
5. Alembic 초기화 (async 템플릿) — **pgvector 함정 3종을 이번에 전부 처리한다** (`03 §5.4`)
   1. 최초 마이그레이션의 **첫 줄**에 `op.execute("CREATE EXTENSION IF NOT EXISTS vector")`.
      없으면 이후 `vector` 타입 컬럼 생성이 `UndefinedObject`로 죽는다.
   2. **`alembic/script.py.mako`에 `from pgvector.sqlalchemy import Vector` 추가.**
      autogenerate가 만든 리비전은 `Vector(1536)`를 뱉지만 import는 넣어주지 않아 실행 시 `NameError`가 난다.
   3. **`database.py`의 asyncpg 커넥션 init에서 `register_vector` 호출.**
      없으면 임베딩이 문자열로 왕복해 `<=>` 연산이 실패하거나 조용히 느려진다.
6. `GET /health` — DB ping 포함 `{"status":"ok","db":"ok","version":"0.1.0"}`
7. **테스트 인프라 (확정 — 다른 선택지를 만들지 말 것, `03 §5.3`)**
   - **testcontainers 미도입.** 이미 떠 있는 Postgres(`docker compose up -d db`)에
     **`TEST_DATABASE_URL`**(`…/bulchimbeon_test`)로 붙는다.
   - `tests/conftest.py`: 세션 스코프 픽스처에서 `CREATE EXTENSION IF NOT EXISTS vector` →
     `Base.metadata.create_all`. **테스트별 격리는 트랜잭션 롤백**(테스트마다 DROP/CREATE 금지 —
     느리고 HNSW 인덱스 재생성 비용이 크다). httpx AsyncClient 픽스처 포함.
   - `tests/test_health.py`
8. GitHub Actions CI (`.github/workflows/ci.yml`): ruff check + pytest, `services: pgvector/pgvector:pg16`만 사용.
   **마이그레이션 검증 테스트 1개만** `@pytest.mark.slow`로 분리한다(빈 DB에서 `alembic upgrade head` 통과 확인).
9. git init + 첫 커밋 `chore(M0): scaffold`

## 완료 기준 (전부 확인 후 보고)

- `docker compose up -d db` → `uv run alembic upgrade head` → `uv run uvicorn app.main:app --workers 1` 기동
- `curl localhost:8000/health` 가 ok 반환
- `uv run pytest`, `uv run ruff check .` 통과
- `DEFAULT_SETTINGS`의 키 집합이 `04 §3`의 16개와 정확히 일치하고 **`briefing_timezone`이 없다**
- `pyproject.toml`에 `sse-starlette`·`testcontainers`·`aiosqlite`가 **없다**

## ⚠️ 이 마일스톤에서 가장 자주 깨지는 것

- **`STORAGE_DIR`는 절대 경로**로 둔다(예: `/home/kancth03/bulchimbeon/bulchimbeon-api/storage`). 상대 경로는 uvicorn 실행 위치·컨테이너
  WORKDIR에 따라 다른 디렉터리를 가리켜 "업로드는 됐는데 파일이 없다"를 만든다.
- **호스트 실행 모드와 컨테이너 실행 모드를 섞지 않는다.** 개발 표준은 `docker compose up -d db` + 호스트 uvicorn 하나다.
- **`--workers 1`은 선택이 아니다.** SSE 구독자 큐가 인메모리이고 APScheduler가 프로세스마다 중복 발화한다.
  README·docker-compose·배포 시작 커맨드 어디에도 워커를 늘리는 예시를 남기지 않는다.

폴더 구조는 `03 §3`을 그대로 따르고, 아직 비는 모듈은 빈 파일로 만들지 말고 실제 필요해질 때 만들 것.
