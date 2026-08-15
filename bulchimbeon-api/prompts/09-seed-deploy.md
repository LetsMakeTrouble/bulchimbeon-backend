# M9 — 시드 데이터 · 클라우드 배포 · 최종 점검

@docs/08-demo-scenario.md 전체, @docs/03-tech-spec.md §6·§7, @docs/07-build-plan.md 를 기준으로 데모 준비를 완성해줘.

> ⏱ **배포는 6일차로 앞당긴다** (결정 1.18). 아래 4번(클라우드 배포 1차)은 **M5가 끝난 6일차에 먼저 실행**하고,
> 7일차에 시드·리허설(1·2·3·5번)을 마무리한다. 외부 마찰(Railway pgvector 권한, 마이그레이션, SSE 프록시)을
> 하루 먼저 노출시켜야 복구 시간이 생긴다 — 발표 전날에 처음 배포하면 복구 시간이 0이다.

## 작업

1. **시드 파일 생성 — 확장된 4문서 세트 / 총 23청크** (`docs/08-demo-scenario.md §2`의 본문을 **그대로**)
   - `seed/api-spec.md` (10청크) — 기존 `GET /v2/orders/{order_id}` · `Currencies` · `Authentication` ·
     `Rate Limiting` · `Pagination` + 신규 `Webhooks` · `Errors` · `Idempotency` · `Sandbox` · `Regions`
     (⚠️ `Currencies`는 **M-1 캘리브레이션에서 추가**됐다. Q3("지원하는 통화가 뭐예요?")가
     `GET /v2/orders/{order_id}`의 다목적 필드 목록 대신 `Sandbox`를 top-1로 잡아 MISMATCH가 났고,
     결정 1.7에 따라 임계값이 아니라 시드를 보강했다)
   - `seed/refund-policy.md` (5청크) — `Standard Refund Window` · `Shipping Fees` · `Digital Goods` ·
     `Partial Refunds` · `Disputes`
     (⚠️ **일본 조항은 여전히 없다** — 시나리오 B의 근거 부족 🔴 장치.
     api-spec의 `Regions`는 `ap-northeast`가 있다는 인프라 사실만 말하고 환불 정책을 다루지 않으므로
     ④의 "주제만 언급하는 청크는 근거가 아니다" 규칙에 걸려 `not_answerable=true`가 된다)
   - `seed/integration-guide.md` (5청크, 신규) — `Getting Started` · `Environments and API Keys` ·
     `Webhook Signature Verification` · `Retrying Failed API Calls` · `Going Live Checklist`
     (⚠️ `Retrying Failed API Calls`는 **클라이언트가 API를 호출할 때**의 재시도다.
     meeting-notes의 **웹훅 재시도**와 주제가 다르므로 Q5의 단일 출처성이 보존된다 — 섞어 쓰지 말 것)
   - `seed/meeting-notes-2026-07.md` (2청크) — `API and Rate Limits` · `JP Launch`.
     **Q7 충돌 문안 교정본**을 그대로 쓴다:
     `"Confirmed with Mike: the API rate limit is 100 requests per minute per API key.
     This supersedes the older figure in the API spec."`
     (시제·대상(per API key)·확정성을 api-spec의 60/min과 일치시켜야 **어떤 모델도 충돌로 판정**한다.
     `"will be raised … for enterprise keys, rollout date TBD"` 같은 예정형으로 되돌리면
     60/min과 양립 가능해져 충돌 감지가 재현되지 않는다)
     `"Payment gateway integration is on track."`은 **Q9의 미끼 청크로 유지**한다 — 어휘만 겹치고
     PG사 이름이 없는 청크가 일반 상식 폴백을 유도하지 않는지 보는 좋은 테스트다
   - **인제스트 후 `SELECT count(*) FROM chunks`가 21 근처**여야 한다.
     `retrieval_top_k=6`보다 충분히 커야 검색이 "전부 반환"으로 퇴화하지 않는다
   - `.gitattributes`의 `*.md text eol=lf`가 적용된 상태에서 커밋한다(CRLF 혼입 시 `content_hash` 불안정)
   - ⚠️ `scripts/probe_calibration.py`의 `SEED_CHUNKS`는 이 §2의 **스냅샷**이다.
     §2 본문을 손대면 그 상수도 함께 갱신해야 M-1 재측정이 유효하다

2. **`scripts/seed.py`** (데모 시나리오 §5 요구사항)
   - 유저 3명 · 프로젝트 · 지침 · **문서 4개** 업로드 · 인제스트 완료(ready) 대기
   - `--reset`(멱등 — 프로젝트 삭제 API는 MVP 범위 밖이므로 **DB 레벨에서** 정리한다, D19)
   - **`--with-history`: 질문 45~60건**을 실제 파이프라인으로 실행해 이력·지표를 채운 상태로 시작한다.
     Q1~Q6만으로는 표본이 6건이라 **`grade_accuracy`가 전부 "표본 부족"으로 뜨고**(D25, 30건 기준)
     시계열 그래프도 점 하나가 되어 발표 마지막 화면이 비어 보인다.
     검증 질문셋(§3, **Q1~Q13**) 중 `08 §5` 구성대로 **Q1~Q6 + Q11~Q13(9종)**과 그 패러프레이즈 변형을 섞어 45~60건(🟢만 35건 이상 — D25의 30건은 등급별 표본이므로 총 30건으로는 전 등급이 "표본 부족"으로 남는다)을 만든다. 🟢 위주로 하되 등급 분포를 위해 Q7·Q9 계열 변형도 몇 건 섞는다.
     ⛔ **원문 그대로의 Q8·Q10은 시나리오 B 라이브 재현을 위해 이력에 미리 소진하지 않는다**(변형은 무방 — 🔴 no_evidence는 공식 Q&A를 만들지 않으므로 Q10 재사용 경로를 오염시키지 않는다).
     ⛔ **Q1은 이력 실행은 허용하되 승인(`verified`)·공식 Q&A 편입을 금지한다.** 승인하면 데모 당일 Q1이 재사용 경로로 빠져 §4 1단계의 "🟢 87% + 인용"이 사라진다.
   - **승인 주입 어서션**: 승인(`verified`) 주입 시 질문 텍스트가 Q1/Q8/Q10 원문 또는 그 변형 시드 목록에서
     유래했는지로 **명시적 제외 목록 필터링**을 하고, 추가 안전망으로 "승인 후 `official_qas`에 대해
     Q1·Q8·Q10 원문 임베딩을 조회했을 때 최대 `sim_raw`가 `settings.similar_threshold` 미만"을 어서션한다.
     임계값은 `settings`에서 로드하고 하드코딩하지 않는다(CLAUDE.md 아키텍처 규칙 3)
   - 서비스 레이어 직접 호출(HTTP 아님)로 구현
   - ⚠️ 시드가 카드 상세(`GET /review-cards/{id}`)를 호출하지 않게 한다 — `first_viewed_at`이 오염되면
     `card_handle_30s_rate`가 무의미해진다 (M7)
3. **검증 질문셋 회귀 테스트**: `tests/test_demo_scenarios.py` — FakeLLM 모드에서 **Q1~Q13** 기대 등급 재현(§3 표).
   Q11~Q13은 확장된 시드 섹션이 실제로 top-6 안에 들어오는지 보는 케이스이므로 빠뜨리지 말 것.
   Q9의 기대값은 **`no_evidence` 또는 `low_confidence` 둘 다 정답**이다(미끼 청크가 검색은 되므로).
   ⚠️ 단, **회귀 테스트에서는 Q9를 결정적으로 만든다** — FakeLLM 픽스처에서 Q9를 `not_answerable=true`(→ `no_evidence`, 강제 🔴)로 고정하거나, `low_confidence` 경로를 쓸 경우 **담당자 DND(22:00~07:00 ET) 밖의 고정 시각으로 시계를 주입**한다. 고정하지 않으면 DND 강등(D2)으로 Q9만 🟡이 되어 실행 시각에 따라 테스트가 흔들린다.
   실LLM 검증용 `scripts/eval_questions.py`(질문셋 실행 → 등급·`sim_raw`·`elapsed_ms` 출력 → 기대값 대비 리포트)도 작성.
   **`eval_questions.py`의 출력은 M-1 `probe_calibration.py`의 판정 1 표와 대조 가능한 형태**로 만든다
   (임계값이 실측 이후에도 유효한지 확인하는 마지막 관문)
4. **배포 (6일차에 선행 실행)**: Railway(또는 Render)
   - 시작 커맨드: **`uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`**, `alembic upgrade head` 선행
   - **`CREATE EXTENSION vector` 권한을 먼저 확인**한다. 매니지드 Postgres가 슈퍼유저 권한을 주지 않으면
     이 한 줄에서 막히고, 그때는 pgvector 지원 이미지를 직접 띄우거나 DB를 갈아타야 한다
   - `SELECT extversion FROM pg_extension WHERE extname='vector'` → **0.8.0 이상** 확인
     (`hnsw.iterative_scan` 전제. M-1에서 로컬은 확인했고 **여기서 확인하는 건 배포 DB다**)
   - `.env` 체크리스트 출력. 배포 문서 `docs/09-deploy-notes.md`로 정리(URL·시드 실행 방법·롤백)
5. **최종 점검 체크리스트 실행 후 결과 보고**
   - [ ] 클라우드 URL `/api/health` ok, Swagger 접근 가능
   - [ ] **배포 DB의 pgvector extension 생성 권한 + `extversion >= 0.8.0` 확인**
   - [ ] **시작 커맨드에 `--workers 1`이 실제로 들어가 있는지 확인** (대시보드에서 눈으로 확인한다.
         워커가 2개면 SSE가 절반만 도착하고 브리핑이 두 번 나간다)
   - [ ] 시드 후 데모 스크립트 §4의 1~6단계를 curl(또는 httpx 스크립트)로 전 구간 재현
   - [ ] **발표 시각 기준 DND 판정 결과 확인** — 담당자(Mike, `America/New_York`)의 DND 22:00~07:00 ET가
         **발표 시각(KST)에 걸리는지** 계산한다. 걸리더라도 **강제 🔴 4종은 🔴을 유지**하므로
         Q7·Q8은 그대로 재현되어야 한다. **실제로 그 시각에 Q7·Q8을 던져 🔴이 나오는지 확인**한다.
         🟡로 강등되면 DND 예외 규칙(D2)이 구현되지 않은 것이다
   - [ ] 등급 확정 지연 확인 (`question.graded`의 `elapsed_ms`) — M-1 판정 3의 추정치와 대조
   - [ ] **SSE 15분 재연결 확인** — 스트림을 **16분 이상** 열어 두고 프록시가 끊은 뒤
         새 티켓으로 재연결이 되는지, 재연결 시 `notification.unread_count`가 1회 오는지 확인한다.
         짧게 열어보고 "SSE 잘 된다"로 넘기면 발표 중에 조용히 죽는다
   - [ ] SSE 버퍼링 확인 (네이티브 `EventSourceResponse`가 `X-Accel-Buffering: no`를 내보내는지)
   - [ ] CORS에 프론트 도메인 등록
   - [ ] 일일 LLM 한도(`settings.daily_llm_call_limit`) 동작 · 토큰 마스킹 · 비밀값 로그 미출력
   - [ ] 시드 후 `SELECT count(*) FROM chunks` **≥ 20** (`08 §2` 기준 23청크)

## 완료 기준

- `uv run python scripts/seed.py --reset --with-history` 성공 후, 클라우드 URL에서
  **시나리오 A**(Q1 🟢) · **시나리오 B**(Q8 🔴 → 카드 → 수정 확정 → **질문자 GET이 `answered`로 전환** → Q10 재사용)
  재현 증적(응답 JSON) 제시
- 위 체크리스트 전 항목을 결과와 함께 보고 (통과/실패/조치)
- `uv run pytest` 전체 통과 → 커밋 `feat(M9): seed, deploy, demo readiness`

## 플랜B (리허설에서 미리 확인해 둘 것)

- SSE 미동작 → 새로고침(폴링)으로 동일 시연. `GET /projects/{id}/briefing/today`는 언제든 수동 호출 가능
- 클라우드 URL 불안정 → 로컬 `docker compose up -d db` + 호스트 uvicorn 백업
- 브리핑 스케줄러를 컷한 경우 → `GET /briefing/today` 수동 호출로 동일 화면.
  단 이때 **M5의 `deliver_after` 보류 로직도 함께 컷되어 있어야 한다**(보류만 하고 flush가 없으면 알림이 영영 안 나간다)
