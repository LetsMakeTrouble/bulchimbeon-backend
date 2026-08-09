# 09 — 배포 노트 (Railway)

> M9 배포 세션의 산출물. **URL·시드 실행 방법·롤백**을 한 곳에 모은다.
> 값이 바뀌면 이 문서를 고친다 — 대시보드만 고치고 여기를 두면 다음 사람이 틀린 값을 쓴다.

## 1. 구성

| 항목 | 값 |
| --- | --- |
| 배포처 | Railway |
| 서비스 | (배포 후 기입) |
| 공개 URL | (배포 후 기입) |
| DB | Railway Postgres **18.4** / pgvector **0.8.6** (M-1에서 확인, 배포 세션에서 재확인) |
| 이미지 | 레포 루트 `Dockerfile` (멀티스테이지 uv) |
| 배포 설정 | `railway.json` |

## 2. 시작 커맨드

```
sh -c 'alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1'
```

`railway.json`의 `deploy.startCommand`에 넣었다. **대시보드에서 직접 고치지 않는다** —
대시보드 값이 우선하므로, 거기서 바꾸면 레포의 이 값이 조용히 무시된다.

### ⛔ `sh -c '...'` 로 감싼 것은 취향이 아니다 — 벗기면 안 뜬다

**Railway는 `startCommand` 안의 `${...}` 를 셸보다 먼저 자기가 치환한다.**
`${PORT:-8000}` 을 "`PORT:-8000` 이라는 이름의 변수 참조"로 읽고, 그런 변수가 없으니
**빈 문자열로 바꿔 버린다.** 그러면 `--port` 뒤가 비어 uvicorn 이 죽는다.
작은따옴표 안에 넣으면 Railway 가 건드리지 않고 `sh` 가 제대로 펼친다.

배포 세션에서 실제로 3번 연속 실패했고, 증상이 **아무 로그도 안 남는 것**이라 진단이 어렵다:
`Starting Container` 다음 alembic 두 줄만 찍히고 끝난다. 헬스체크는 2분 뒤 "service
unavailable" 로만 실패해서 원인을 가리킨다. 같은 이미지를 로컬에서 돌리면 멀쩡히 뜨므로
"이미지는 정상, Railway 만 실패" 라는 신호가 나오면 **여기를 먼저 의심한다.**

`Railway` 가 `PORT` 를 자동 주입하지 않는다는 것도 함께 확인됐다 — `PORT=8000` 을
서비스 변수로 명시했고, 도메인의 target port 도 8000 으로 지정했다(둘 다 비어 있었다).

`exec` 를 붙인 이유는 uvicorn 이 PID 1 을 이어받아 SIGTERM 을 직접 받게 하려는 것이다.
없으면 `sh` 가 신호를 삼켜 재배포마다 graceful shutdown 이 아니라 강제 종료가 된다.

- **`--workers 1`은 협상 대상이 아니다** (`CLAUDE.md` 룰 9). SSE 구독자 큐가 인메모리라
  워커가 2개면 이벤트가 **절반만** 도착하고, APScheduler가 워커마다 발화해 브리핑이 두 번 나간다.
  `numReplicas: 1`도 같은 이유다 — 워커를 1로 묶어도 레플리카가 2면 똑같이 깨진다.
- `alembic upgrade head`를 앞에 두는 이유는 컨테이너가 스키마보다 먼저 뜨면 `/health`가
  DB ping에서 실패하고 헬스체크가 재시작 루프를 만들기 때문이다. alembic은 멱등이라
  재시작마다 돌아도 안전하다.

## 3. 환경변수 체크리스트

`app/config.py`의 `Settings`와 1:1이다. **기본값이 있는 것도 배포에서는 명시**한다 —
기본값에 기대면 로컬용 값(`change-me`, localhost)이 그대로 뜬다.

| 변수 | 배포 값 | 빠뜨리면 |
| --- | --- | --- |
| `APP_ENV` | `demo` | 동작은 하나 로그·표기가 local로 남는다 |
| `API_BASE_URL` | 공개 URL | 알림·연동 링크가 localhost를 가리킨다 |
| `CORS_ORIGINS` | 프론트 도메인 (쉼표 구분) | 프론트에서 **모든 요청이 CORS로 차단**된다 |
| `SECRET_KEY` | 랜덤 32B+ | ⛔ `change-me`면 누구나 토큰을 위조한다 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | 기본값과 같음 |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `14` | 기본값과 같음 |
| `DATABASE_URL` | `postgresql+asyncpg://…` | ⛔ **아래 §3.1 참조** |
| `LLM_PROVIDER` | `openai` | `fake`면 임베딩이 해시라 검색이 무의미해진다 |
| `OPENAI_API_KEY` | 실 키 | 전 파이프라인이 죽는다 |
| `LLM_MODEL_ANSWER` | `gpt-5-mini` | |
| `LLM_MODEL_VERIFY` | `gpt-5-mini` | |
| `LLM_MODEL_TRANSLATE` | `gpt-5-mini` | |
| `LLM_REASONING_EFFORT` | `minimal` | 지연 예산(🟢 25초)을 못 지킨다 |
| `LLM_TIMEOUT_SECONDS` | `45` | |
| `LLM_PIPELINE_DEADLINE_SECONDS` | `25` | |
| `LLM_PIPELINE_DEADLINE_RED_SECONDS` | `35` | |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | |
| `EMBEDDING_DIM` | `1536` | 다른 값이면 **기동 시점에 죽는다**(fail-fast) |
| `STORAGE_DIR` | `/app/storage` | 절대경로 아니면 기동 실패. §4 볼륨과 같은 경로여야 한다 |
| `INTEGRATION_ENCRYPTION_KEY` | 실 키 | ⭐ **M8 연동 토큰을 복호화 못 해 전부 재등록**해야 한다 |

`TEST_DATABASE_URL`은 배포에 넣지 않는다 — 테스트 전용이다.

### 3.1 `DATABASE_URL`은 Railway가 주는 값을 그대로 쓰면 안 된다

Railway Postgres가 주는 값은 `postgresql://…`인데, 이 앱의 SQLAlchemy 엔진과
`alembic/env.py`는 **async 드라이버**를 쓴다(`app/database.create_engine`).
스킴이 `postgresql+asyncpg://`가 아니면 기동과 마이그레이션이 둘 다 실패한다.

내부 네트워크 주소를 쓰되 스킴만 바꿔 넣는다(외부 프록시 주소는 지연·요금이 붙는다).

## 4. 스토리지 볼륨

`STORAGE_DIR=/app/storage`에 **영속 볼륨을 붙인다.** 안 붙이면 재배포·재시작마다
업로드 원본이 사라진다. 청크와 임베딩은 DB에 있어 검색은 계속 되지만,
문서 다운로드와 재인제스트가 깨지고 `app/services/sync/runner.py:193`이
"저장 파일이 사라졌다" 경로를 탄다.

### ⛔ 볼륨을 새로 만들면 **소유권을 반드시 고쳐야 한다**

Railway 볼륨은 **`root:root` 0755로 마운트**된다. 그런데 이 이미지는 `USER appuser`(UID 10001)로
돌기 때문에(Dockerfile), 앱이 `/app/storage`에 **한 글자도 쓸 수 없다.**
Dockerfile의 `chown -R appuser:appuser /app`은 **빌드 시점**이라 런타임 마운트가 그 위를 덮는다.

증상이 고약하다 — **앱은 정상 기동하고 `/health`도 200이다.** 깨지는 건 업로드뿐이라
시드를 돌려야 비로소 드러난다.

```bash
# 볼륨을 만든 직후 1회 (ssh 세션은 root 로 붙는다)
railway ssh --service bulchimbeon-api chown appuser:appuser /app/storage
```

소유권은 **볼륨에 저장되므로 재시작·재배포를 넘어 유지된다.** 다시 해야 하는 경우는
**볼륨을 새로 만들 때뿐**이다. 확인:

```bash
railway ssh --service bulchimbeon-api \
  'su appuser -s /bin/sh -c "touch /app/storage/.t && echo OK && rm /app/storage/.t"'
```

⚠️ **같은 이유로 시드도 `appuser`로 돌려야 한다.** `railway ssh` 는 root 로 붙으므로
그냥 돌리면 업로드 파일이 root 소유로 깔리고, 그 뒤 앱이 그 파일을 지우거나 다시 쓰지 못한다.
§5의 명령이 전부 `su appuser -s /bin/sh -c "..."` 를 거치는 이유다.

## 5. 시드 실행 방법

⚠️ **컨테이너 안에서 돌린다.** 로컬에서 배포 DB에 대고 돌리면 청크·임베딩은 배포 DB에
들어가지만 **업로드 원본은 이 머신에 남고 배포 볼륨은 빈 채**가 된다.

⚠️ **`railway run`이 아니라 `railway ssh`다.** `railway run`은 원격 환경변수만 빌려 와
**로컬에서** 명령을 실행한다. 그러면 (a) 업로드 원본이 로컬에 남고 (b) `DATABASE_URL`이
`postgres.railway.internal`을 가리키는데 이 주소는 **바깥에서 접속되지 않아** 어차피 실패한다.

```bash
# 1) 기본 시드 — 문서 4개 + 인제스트만
railway ssh --service bulchimbeon-api python scripts/seed.py --reset

# 2) 실 LLM 대조 (12건, 저렴) — 이력보다 먼저 돌린다
railway ssh --service bulchimbeon-api python scripts/eval_questions.py

# 3) 이력 주입 (58건, 약 175~235 LLM 호출)
railway ssh --service bulchimbeon-api python scripts/seed.py --reset --with-history
```

`seed/*.md` 4개는 이미지 안에 있어야 한다. Dockerfile이 `COPY seed ./seed`를 빠뜨리면
`scripts/seed.py:100`의 `SEED_DIR`이 없어서 `FileNotFoundError`로 죽는다 (배포 세션에서 실제로 났다).

- 2번을 1번과 3번 **사이**에 두는 이유는 M-1 임계값이 헤딩 줄 없는 텍스트로 측정됐다는
  미해결 건(`START-HERE.md`) 때문이다. 12건은 싸므로, 어긋나면 58건을 채우기 전에 잡는다.
- 3번은 🟢 발행이 **30건 미만이면 종료 코드 1**로 끝난다. 그때는
  `scripts/demo_questions.py`의 🟢 계열 변형을 늘리고 다시 돌린다.
- ⛔ **리허설에서 Q8을 확정(edit)했다면 발표 전에 3번을 다시 돌린다.** 확정하면 공식 Q&A가
  생겨 데모 당일 시나리오 B가 "재사용"으로 빠져 통째로 사라진다.

## 6. 롤백

| 상황 | 조치 |
| --- | --- |
| 배포본이 뜨지 않음 | Railway 대시보드 > Deployments > 직전 성공 배포 **Redeploy** |
| 마이그레이션이 잘못 올라감 | `railway ssh --service bulchimbeon-api alembic downgrade -1` |
| DB 데이터가 오염됨 | `seed.py --reset --with-history` 재실행 (데모 데이터는 전부 재생성 가능하다) |
| 클라우드가 통째로 불안정 | 플랜B — 로컬 `docker compose up -d db` + 호스트 uvicorn (`08 §4` 플랜B) |

`--reset`은 프로젝트 삭제 API가 MVP 범위 밖이라 **DB 레벨에서** 정리한다(D19).
데모 데이터 외의 것을 지우지 않는지 확인하고 쓴다.

## 7. 배포 후 확인한 것

(배포 세션 실행 결과로 채운다)
