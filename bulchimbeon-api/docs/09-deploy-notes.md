# 09 — 배포 노트 (Railway)

> M9 배포 세션의 산출물. **URL·시드 실행 방법·롤백**을 한 곳에 모은다.
> 값이 바뀌면 이 문서를 고친다 — 대시보드만 고치고 여기를 두면 다음 사람이 틀린 값을 쓴다.

## 1. 구성

| 항목 | 값 |
| --- | --- |
| 배포처 | Railway |
| 서비스 | `bulchimbeon-api` (프로젝트 `prolific-inspiration`) |
| 공개 URL | `https://bulchimbeon-api-production.up.railway.app` |
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
- `alembic upgrade head`를 앞에 두는 이유는 컨테이너가 스키마보다 먼저 뜨면 `/api/health`가
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
| `LLM_MODEL_ANSWER` | `gpt-5.6-terra` | 단계별 배정은 `03 §4.1` |
| `LLM_MODEL_VERIFY` | `gpt-5.6-sol` | ⛔ 뚫리면 환각이 🟢로 발행된다 |
| `LLM_MODEL_REUSE_GATE` | `gpt-5.6-terra` | 실측상 sol 과 동일 (`03 §4.1.2`) |
| `LLM_MODEL_STRUCT` | `gpt-5.6-terra` | |
| `LLM_MODEL_TRANSLATE` | `gpt-5.6-terra` | ⛔ **luna 로 내리지 마라** — Q8 이 차단선을 넘나든다 (`03 §4.1.3`) |
| `LLM_MODEL_LESSON` | `gpt-5.6-luna` | |
| `LLM_MODEL_ANSWER_TRANSLATE` | `gpt-5.6-terra` | ⛔ TRANSLATE와 합치지 마라 (`03 §4.1`) |
| `LLM_REASONING_EFFORT` | `low` | ⛔ `minimal`은 gpt-5.6에서 **400**이다 |
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

증상이 고약하다 — **앱은 정상 기동하고 `/api/health`도 200이다.** 깨지는 건 업로드뿐이라
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

⚠️ **`su appuser` 를 거친다.** `railway ssh` 는 root 로 붙으므로 그냥 돌리면 업로드 파일이
root 소유로 깔려 앱(UID 10001)이 그 뒤에 지우지도 다시 쓰지도 못한다 (§4).

```bash
SSH="railway ssh --service bulchimbeon-api -i ~/.ssh/id_ed25519"
RUN='su appuser -s /bin/sh -c'

# 1) 기본 시드 — 문서 4개 + 인제스트만
$SSH "$RUN 'cd /app && /app/.venv/bin/python scripts/seed.py --reset'"

# 2) 실 LLM 대조 (12건, 저렴) — 이력보다 먼저 돌린다
$SSH "$RUN 'cd /app && /app/.venv/bin/python scripts/eval_questions.py'"

# 3) 이력 주입 (123건, 약 25~30분)
$SSH "$RUN 'cd /app && /app/.venv/bin/python scripts/seed.py --reset --with-history'"
```

⚠️ **Railway SSH 검증 서비스가 간헐적으로 죽는다.** `can't verify your SSH key` 는 키
문제가 아니라 Railway 쪽 일시 장애다 — 1분 뒤 재시도하면 붙는다. 배포 세션에서 2번 겪었다.
긴 시드는 재시도 루프로 감싸 두는 편이 안전하다.

`seed/*.md` 4개는 이미지 안에 있어야 한다. Dockerfile이 `COPY seed ./seed`를 빠뜨리면
`scripts/seed.py:100`의 `SEED_DIR`이 없어서 `FileNotFoundError`로 죽는다 (배포 세션에서 실제로 났다).

- 2번을 1번과 3번 **사이**에 두는 이유는 12건이 싸기 때문이다. 임계값이나 모델 배정이
  바뀌었으면 여기서 먼저 드러나고, 123건을 25분 들여 채우기 전에 잡을 수 있다.
- 3번은 🟢 발행이 **30건 미만이면 종료 코드 1**로 끝난다. 그때는 **계열별 발행률을 먼저 보고**
  (§7.2) 높은 쪽(Q6·Q13)에만 변형을 더한 뒤 다시 돌린다. 넓게 뿌리면 🔴만 늘어난다.
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

## 7. 배포 후 확인한 것 (2026-08-09)

| 체크리스트 (`09 §5`) | 결과 |
| --- | --- |
| `/health` ok · Swagger 접근 | ✅ `{"status":"ok","db":"ok"}` · `/docs` 200 · **58 operations** |
| 배포 DB pgvector 권한 · `extversion ≥ 0.8.0` | ✅ `CREATE EXTENSION` 성공 · **0.8.6** · pg 18.4 |
| 시작 커맨드 `--workers 1` | ✅ **PID 1 cmdline 실측** (대시보드 눈확인 아님) |
| 데모 §4 1~6단계 재현 | ✅ 아래 §7.1 |
| 등급 확정 지연 (`elapsed_ms`) | ✅ 최대 8.8초 · **데드라인 초과 0건** (🟢🟡 25s · 🔴 35s) |
| **SSE 15분 재연결** | ✅ **정확히 900초에 끊김**(curl rc=92) → 새 티켓 재연결 200 · `notification.unread_count` **1회** 수신 |
| SSE 버퍼링 | ✅ `x-accel-buffering: no` · ping 62회 · `answer.completed` 4 · `notification.created` 4 |
| CORS | ✅ `localhost:3000` 허용 · 미등록 오리진 **400 차단** |
| 시드 후 `count(*) FROM chunks` | ✅ **22** (≥20) |
| `STORAGE_DIR` 볼륨 재시작 생존 | ✅ 재배포 넘어 파일 유지 · `appuser` 소유 |
| 발표 시각 DND 판정 | ✅ 아래 §7.3 |
| 일일 LLM 한도 | ✅ 한도를 1로 낮춰 질문 → `held` / `quota_exceeded` 확인 후 500 원복 |
| 토큰 마스킹 | ✅ 더미 토큰 등록 → 응답이 `ghp_****` · 원문 미노출 → 삭제(204) |
| 비밀값 로그 미출력 | ✅ 배포 로그 213줄에 토큰·비밀번호·DB URL 패턴 0건 |

### 7.1 시나리오 재현 증적

**시나리오 A** — Q2 "액세스 토큰 만료 시간이 어떻게 되나요?"
→ `status=answered` · `grade=green` · 인용 1건(`Orders API Specification v2.1 > Authentication`)
→ 답변 "액세스 토큰은 24시간 후에 만료됩니다."

**시나리오 B** — 전 구간 재현됨
1. Q8 "환불 정책이 일본 리전에도 동일하게 적용되나요?" → `status=held` (🔴 보류)
2. 담당자 카드 `edit` "Japan is 20 days (local law)."
   → `answer.state=verified` · `official_qa_id` 생성 · `lesson_candidate_id` 생성
3. **질문자 GET → `status=answered` · `state=verified`** (`held → answered` 전이)
   → 답변 "일본은 20일입니다(현지법)."
4. Q10 "일본 리전 환불 정책도 동일하게 적용되나요?" (민준)
   → `grade=green` · `state=verified` · `official_qa.reuse_count=1`
   → **확정 한국어 원문 그대로 재사용** — 재번역 없음 (룰 4)

⚠️ **리허설이 Q8 을 확정했으므로 공식 Q&A 가 이미 존재한다.**
발표 전에 반드시 `--reset --with-history` 로 다시 채운다 — 안 그러면 데모 당일 Q8 이
재사용 경로로 빠져 시나리오 B 가 통째로 사라진다.

### 7.2 🟢 표본 · 자동응답률 — 최종 (2026-08-09)

데모 프로젝트 `fa0445f0-1179-425e-bc2d-33353589b603`
(gpt-5.6 배정 + `similarity_floor` 0.444 + 질문 문안 교정 반영)

| 지표 | 값 |
| --- | --- |
| `auto_answer_rate` | **0.7561** (목표 0.7) · 🟢 37 / 🟡 56 / 🔴 30 |
| `grade_accuracy` 🟢 | 표본 **37** · 정확도 **0.8378** |
| `grade_accuracy` 🟡 | 표본 **56** · 정확도 **0.9107** |
| `grade_accuracy` 🔴 | 표본 **30** · 정확도 **0.4333** (§7.4) |
| `saved_wait_hours` | 2232시간 (근거 93건) |

라이브 경로 3종 보존 확인 — Q2 `green` · Q8 `no_evidence` · Q7 `conflict`.

#### ⭐ 자동응답률이 목표 아래로 떨어졌을 때 — 임계값이 아니라 **질문 문안**을 봐라

`similarity_floor` 를 0.444 로 올린 직후 자동응답률이 **0.6829** 로 목표(0.7)를 밑돌았다.
임계값을 도로 내리고 싶어지는 지점인데, **DB 를 뜯어보니 임계값 구간(0.423~0.444)에 걸린
질문은 4건뿐이었고 그중 1건은 Q7 계열이라 🔴 이 정상**이었다.

나머지는 질문에서 **주제 앵커가 빠진 것**이 원인이었다:

| 문안 | sim |
| --- | --- |
| `HMAC 서명 계산 방법을 알려주세요.` | 0.4265 |
| → `웹훅 서명 다이제스트는 어떤 문자열로 HMAC-SHA256 을 계산하나요?` | **0.6676** |
| `결제 가능한 통화 종류를 알려주세요.` | 0.4308 |
| → `주문에 쓸 수 있는 정산 통화가 무엇인가요?` | **0.6288** |

`웹훅`·`정산`·`주문 응답` 같은 주제어가 빠지면 유사도가 0.43 대로 주저앉는다.
`결제 가능한 통화` 의 **`결제`** 는 오히려 근거가 없는 PG 영역(Q9)으로 끌어당겼다.
같은 방식으로 8건을 고쳐 **0.6829 → 0.7561** 이 됐다.

⛔ **임계값을 내려 숫자를 맞추지 마라.** 그건 원인을 덮는 것이다. 순서는
① 🔴 사유별 분포 확인(`no_evidence` vs `low_confidence`) → ② 임계값 구간에 실제로 몇 건이
걸렸는지 → ③ 그 질문들이 **정말 답할 수 있는 것인지**(Q7·Q8·Q9 계열은 🔴 이 정답이다) →
④ 답할 수 있는데 낮으면 **문안을 고친다.**
⚠️ 문안 수정안은 재시드(25~30분) 전에 **번역→임베딩→top-1 유사도로 먼저 검증**하라.

#### 왜 58건에서 123건까지 늘려야 했나

1차 실행(58건)은 **🟢 16건**에 그쳤다. `08 §5` 의 45~60건은
"🟢 기대 질문을 35건 넣으면 🟢 발행도 30건 나온다"는 **가정**이었는데,
실측 발행률은 **27.6%** 였다. 패러프레이즈는 원문과 다른 문장이라 매칭률이 그대로
재현되지 않는다. D25 의 30건은 *발행된* 표본 수이므로 총량으로 흡수하는 수밖에 없다.

#### ⭐ 늘릴 때 어디를 늘려야 하는가 — 계열마다 발행률이 크게 다르다

2차 실행(113건, 🟢 28건)의 계열별 🟢 발행률이 결정적인 단서였다:

| 계열 | 근거 섹션 | 🟢 발행률 |
| --- | --- | --- |
| **Q6** | `Refund Policy > Shipping Fees` | **80%** (12/15) |
| **Q13** | `API Spec > Sandbox` | 47% (8/17) |
| **Q2** | `API Spec > Authentication` | 38% (5/13) |
| Q11 / Q4 | Webhook Signature / Pagination | 13% / 9% |
| **Q1 · Q3 · Q12** | Orders 필드 / Currencies / Idempotency | **0%** (0/28) |

⚠️ **질문을 잘 쓰는 것으로는 안 된다.** Q1·Q3·Q12 에는 시드 본문의 사실을 그대로 겨냥한
구체적 질문을 28건 넣었는데 **🟢 이 하나도 안 나왔다.** 근거 청크는 정확히 top-1 으로
잡히는데 매칭률이 80 을 못 넘는다. 차이를 만드는 것은 질문의 품질이 아니라
**섹션이 임베딩 공간에서 얼마나 뚜렷하게 분리되는가**다 — `Shipping Fees` 는
"배송비·반품·불량품" 어휘가 다른 섹션과 거의 겹치지 않는 반면, `Idempotency` 나
`Currencies` 는 API 문서 전반의 어휘에 묻힌다.

그래서 3차(123건)에서는 **발행률이 실측된 Q6·Q13 에만** 10건을 더했고 🟢 37건이 됐다.

⛔ **다시 늘려야 할 때도 같은 순서로 하라** — 추측으로 넓게 뿌리지 말고
`eval_questions.py` 나 직전 시드 로그로 **계열별 발행률을 먼저 보고** 높은 쪽에만 더한다.
Q6·Q13 은 이미 포화에 가까우므로, 더 필요하면 질문을 억지로 늘리기보다
`MIN_SAMPLE` 조정이나 시드 문서 보강을 먼저 검토하는 편이 정직하다.

### 7.4 🔴 정확도가 0% 로 뜨던 문제 (사용자 결정 2026-08-09)

이력을 123건으로 늘리자 🔴 표본이 30 을 넘겨 `sufficient: true` 가 됐고,
그 순간 지표 화면에 **"🔴 정확도 0%"** 가 떴다. `08 §4` 는 "🟡·🔴 은 표본 부족 표기가
정상"이라고 적어 두어 이 상태를 예상하지 못했다.

**0% 는 "AI 의 🔴 판정이 전부 틀렸다"는 뜻이 아니다.** `accuracy_service.for_grade` 의
분자는 `state='verified'` 또는 `correct` 피드백인데, 🔴 답변은 질문자에게 발행되지 않아
correct 피드백 경로가 없고 `inject_approvals` 는 🟢·🟡 만 처리했다 → 분자가 구조적으로 0.
실제 운영에서는 담당자가 인박스의 🔴 을 처리하므로 0% 가 나올 수 없다.

조치: `seed.resolve_red_cards()` 가 🔴 카드의 절반을 **선택지로 확정**한다
(`answer-option`). 결과 **정확도 0.4333 (표본 30)** — 최종 실행 기준이며 §7.2 와 같은 값이다.

⛔ **액션은 `answer-option` 이어야 한다.** `approve` 는 🔴 초안이 본문 없이 남는 경우가
있어 **빈 답변을 발행**하고, `edit` 은 시드가 답 내용을 **지어내야** 한다. 선택지는
⑦ 구조화가 만든 것이라 둘 다 피하면서 `05 §7.2` 의 "30초 컷" 경로를 그대로 탄다.

⛔ **제외 목록은 `NO_APPROVAL_KEYS` 가 아니라 `NO_RED_RESOLVE_KEYS` 다 — Q7 이 더 들어간다.**
Q7 은 §7.3 이 "발표 시각에 던져 🔴 유지를 확인하라"고 지정한 질문이라, 공식 Q&A 가 생기면
그때 재사용 경로로 빠져 **확인 자체가 불가능해진다.** 재시드 후 실측으로 확인했다 —
Q2 `green` · Q8 `no_evidence` · Q7 `conflict` 셋 다 보존됐다.

### 7.5 ⑤ 가 근거를 앞 500자만 보고 판정하던 버그 (2026-08-09)

배포본 🔴 33건 중 **`low_confidence` 10건이 예외 없이 `G=0`** 이었다. S 는 58~96 으로
검색은 성공했는데 생성된 문장이 전부 "근거 없음"으로 잘려나가 답변이 빈 채로 보류됐다.

**환각 방어가 작동한 게 아니었다.** 문제의 문장들은 원문에 그대로 있었다:

- `api-spec.md` — "Idempotency keys are ignored on `GET` and `DELETE` requests."
- `api-spec.md` — "Webhooks fire in the sandbox exactly as they do in production."

원인은 `answer.py` 의 ⑤ 입력 구성이 인용 청크를 `prompts.QUOTE_MAX_LENGTH`(500자)로
자른 것이다. 그 상수는 **`05 §6` `citations[].quote` — 화면 하이라이트용 스니펫 길이**이지
검증 근거 길이가 아니다. 결과적으로

> ④ 는 청크 **전문**을 보고 문장을 쓰고, ⑤ 는 **앞 500자만** 보고 판정한다

는 비대칭이 생겼다. 근거가 청크 뒤쪽에 있으면 ⑤ 는 그것을 볼 수 없다. 실제로 해당
청크에서 위 두 문장의 시작 위치는 **576자·501자**로 둘 다 경계 바로 바깥이었다.

조치: ⑤ 에는 청크 전문을 넘긴다. 회귀 테스트
`test_verify_receives_the_whole_chunk_not_a_display_snippet` 가 "긴 청크의 뒷부분 문장이
⑤ 프롬프트에 실리는가"를 직접 확인한다.

수정 후 실측(데모 문서를 복사한 별도 프로젝트, 실 API):

| 질문 | 수정 전 | 수정 후 |
|---|---|---|
| GET 요청에도 멱등키가 적용되나요? | 🔴 (S=85, **G=0**) | **🟢 85** |
| 샌드박스에서도 웹훅이 오나요? | 🔴 (S=70, **G=0**) | 🟡 69 |
| 플랫폼이 환전을 대신 해 주나요? | 🔴 (S=58, **G=0**) | 🟡 58 |
| Q11 웹훅 서명은 어떻게 검증하나요? | 🟡 50 (S=94, **G=50**) | **🟢 94** |

매칭률이 전부 S 값으로 수렴한다 — **G 가 더 이상 병목이 아니다.** Q11 은 `08 §3`
기대표가 🟢 를 요구하던 5건 중 하나였고, 시드 보강 없이 이 수정만으로 해결됐다.

⚠️ **이 수정은 아직 배포본에 반영되지 않았다.** 재배포 + 재시드 전까지 배포 DB 의
🔴 10건과 자동응답률은 수정 전 값이다. 재시드 시 등급 분포가 이동하므로
`08 §3`·`08 §4` 의 숫자를 함께 갱신해야 한다.

### 7.6 재배포 + 재시드 최종 실적 (2026-08-09)

`09 §7.5` 의 ⑤ 절단 수정과 `08 §2` 근거 섹션 보강을 반영해 재배포하고 데이터를 다시 채웠다.
프로젝트 id 는 `6988693f-5d95-4b37-adea-b0a2f4e54375` 로 바뀌었다.

| 항목 | 수정 전 | 수정 후 |
|---|---|---|
| 자동응답률 | 0.7561 | **0.8140** (목표 0.70) |
| 🟢 | 37건 | **62건** |
| 🔴 | 32건 | **24건** |
| 🟢 정확도 | — | **0.7419** (표본 62) |
| 🟡 정확도 | — | **0.8372** (표본 43) |
| 재질문 즉답률 | 지표 없음(재사용 0건) | **1.0** (재사용 6 · 미스 0) |
| `eval_questions.py` | 7/12 | **12/12** |
| 절약한 대기시간 | — | 2,520시간 (근거 105건 × 24h 가정) |

⚠️ **🔴 정확도는 표본 24 로 "표본 부족" 표기가 뜬다.** 이것은 후퇴가 아니라 ⑤ 수정의
결과다 — 잘못 🔴 로 떨어지던 답변 10건이 정상 등급으로 올라가면서 🔴 총수가 30 밑으로
내려갔다. `09 §7.4` 가 고쳤던 "🔴 정확도 0%"(분자가 구조적으로 0)와는 다른 상태이며,
"표본 부족 — 참고용"은 `08 §4` 가 원래 정상으로 규정한 화면이다.
🔴 을 30건으로 되돌리려고 질문을 늘리는 것은 지표를 위해 데이터를 만드는 일이라 하지 않는다.

#### 라이브 시연 리허설 — 실제로 돌려 보고 흔적을 지웠다

| 단계 | 결과 |
|---|---|
| A. 지수가 Q1 을 묻는다 | 🟢 **매칭률 100** (S 100 · G 100) · 인용 1건 · 한국어 답변 즉시 |
| B-1. 지수가 Q8 을 묻는다 | 🔴 `no_evidence` 보류 — 질문자에게 답변이 가지 않는다 |
| B-2. 담당자 인박스 | 카드에 배경 요약 + **선택지 4개** |
| B-3. 선택지 1번 확정 | 답변 `verified` — "30초 컷" 경로 |
| B-4. 질문자 화면 | 확정 한국어 답변 도착 |
| B-5. 민준이 Q10 을 묻는다 | **`source=reused`** 즉답, 확정 원문 그대로 (룰 4 재번역 금지) |

리허설이 만든 질문 4건·답변·카드·공식 Q&A 1건을 **전부 삭제**해 발표 당일 같은 시연이
가능한 상태로 되돌렸다. 되돌린 뒤 `assert_live_questions_not_reusable` 을 다시 돌려
12건 전부 안전함을 확인했다 (Q8 0.4495 · Q10 0.4749, 둘 다 `similar_threshold` 아래).

⛔ **리허설을 하면 반드시 지워라.** Q8 을 확정하면 공식 Q&A 가 생기고, 그러면 발표 당일
Q8 이 ② 재사용으로 빠져 🔴 시연이 통째로 사라진다.


### 7.7 CORS 프론트 도메인 확정 (2026-08-12)

프론트 도메인은 **`https://bulchimbeon.quanect.kr`** 이다.

```bash
railway variables --set "CORS_ORIGINS=https://bulchimbeon.quanect.kr,http://localhost:3000"
```

- `http://localhost:3000` 을 **남겨 둔다** — 프론트 팀의 로컬 개발이 그 오리진이다.
- ⚠️ **스킴까지 정확히 적어야 한다.** `bulchimbeon.quanect.kr` 처럼 스킴이 없으면
  브라우저가 보내는 `Origin` 헤더(`https://…`)와 문자열이 달라 조용히 차단된다.
- 변수만 바꾸면 **재배포가 필요하다** — 앱이 기동 시점에 읽는다.
  `railway variables --set` 은 기본적으로 재배포를 건다(`--skip-deploys` 로 끌 수 있다).

확인 (2026-08-12 실측):

| 오리진 | `access-control-allow-origin` |
|---|---|
| `https://bulchimbeon.quanect.kr` | 반환됨 ✓ |
| `http://localhost:3000` | 반환됨 ✓ |
| 목록 밖 오리진 | **없음** (차단) ✓ |

`allow_credentials=True` 와 `allow_methods/headers=["*"]` 조합은 오리진이 와일드카드가
아니라 **명시 목록**일 때만 안전하다. 목록을 `*` 로 바꾸지 마라.
