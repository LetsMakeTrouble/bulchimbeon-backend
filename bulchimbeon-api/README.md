# 불침번 (Bulchimbeon) — 백엔드 API

시차가 큰 글로벌 팀을 위한 비동기 Q&A 협업 서비스의 FastAPI 백엔드.
설계 문서는 `docs/`, 마일스톤 프롬프트는 `prompts/`, 세션 진행표는 `START-HERE.md`에 있다.

## 실행 (개발 표준 — DB만 컨테이너, 앱은 호스트)

`03 §5.1`이 유일한 개발 표준이다. 호스트 실행 모드와 컨테이너 실행 모드를 섞지 않는다.

```bash
set -a; . ./.env; set +a          # 비밀값 로드 (.env는 git 추적 제외)
uv sync
docker compose up -d db           # DB만 기동 (pgvector/pgvector:pg18)
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --workers 1 --port 8000
```

- Swagger UI: <http://localhost:8000/docs>
- 헬스체크: `curl localhost:8000/health` → `{"status":"ok","db":"ok","version":"0.1.0"}`

> ⚠️ **`--workers 1`은 선택이 아니다.** SSE 구독자 큐가 인메모리이고 APScheduler가 프로세스마다
> 중복 발화하므로 워커를 늘리면 조용히 깨진다 (`CLAUDE.md` 룰 9, `03 §2` 원칙 5).

`docker compose up`(api까지 컨테이너)은 **배포 이미지 검증용**이며 개발 루프에서는 쓰지 않는다.

## 테스트 · 린트

테스트는 `docker compose up -d db`로 떠 있는 Postgres에 `TEST_DATABASE_URL`로 붙는다
(testcontainers 미도입, `03 §5.3`). 테스트용 DB와 `vector` 확장은 픽스처가 자동 생성한다.

```bash
uv run pytest                     # 전체
uv run pytest -m "not slow"       # 마이그레이션 검증 제외
uv run ruff check . && uv run ruff format --check .
```

## 환경변수

`.env.example`을 `.env`로 복사해서 채운다 (`03 §4`). `STORAGE_DIR`는 **절대 경로**여야 한다 —
상대 경로는 uvicorn 실행 위치·컨테이너 WORKDIR에 따라 다른 디렉터리를 가리킨다.

임계값(`s_floor` 등)은 env가 아니라 `projects.settings`에서 로드하며, 기본값은
`app/config.py`의 `DEFAULT_SETTINGS` **한 곳**에만 있다 (`CLAUDE.md` 룰 3, `04 §3`).
