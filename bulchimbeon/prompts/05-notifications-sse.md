# M5 — 알림함 · SSE · 만료 스위퍼

@docs/02-business-rules.md §6, @docs/05-api-contract.md §1.5·§11·§12, @docs/04-data-model.md §4(알림 타입)·§6, @docs/03-tech-spec.md §2·§7 를 기준으로 알림 도메인을 구현해줘.

> ⚠️ **SSE `answer.completed`는 절대 컷 불가**다(결정 1.18). 데모 4단계(질문자 화면 갱신)가 이것 하나에 걸려 있다.
> 나머지 항목은 아래 각 절의 ✂️ 표시를 따른다.

## 작업

1. **모델·마이그레이션**: notifications
   - **`deliver_after timestamptz NULL`** — **NULL = 즉시 발송**, 값 = 그 시각(다음 브리핑) 이후 발송.
     비긴급 카드 알림 보류는 이 컬럼 하나로 표현한다. **`pending_briefing` 같은 별도 플래그·상태를 만들지 않는다**
   - 인덱스 `(user_id, read_at)` + 브리핑 조회용 `(user_id, deliver_after)`

2. **notification_service**: 타입별 생성 헬퍼 — **수신자 `users.language` 기준**으로 title/body 생성
   (정정 알림은 양쪽 언어 각자 생성, 룰 8 · `05 §1.5`). 기존 서비스들에 알림 생성 연결:
   - `answer.completed`(질문자), **`answer.failed`(질문자 — 파이프라인 실패, D23)**,
     `answer.verified` / `answer.corrected` / `answer.kept` / `answer.rejected`(질문자),
     `card.created`(담당자), `doc.review_needed`, `feedback.different`
   - 에러 응답의 `message`는 개발자용이며 한국어 고정이다(프론트는 `error.code`로 자체 문안을 쓴다).
     **사용자 표시 문자열만 수신자 언어를 따른다** — 두 계열을 섞지 말 것
   - ✂️ **컷라인**: 시간이 부족하면 알림 문안을 **한국어 고정**으로 두고 **`answer.corrected`만 양쪽 언어**로
     만든다(결정 1.18 컷라인 3). 데모에 등장하는 다국어 알림은 정정 알림 하나뿐이다

3. **긴급/브리핑 분기 (룰 6)**
   - urgent 카드만 즉시 알림(`deliver_after=NULL`) + SSE.
   - 비긴급은 **`deliver_after` = 다음 브리핑 시각**으로 넣어 둔다. 브리핑 flush는 M6가 이 컬럼을 읽어 처리한다
   - **DND 시간대(담당자 `users.timezone`)에는 즉시 알림도 보류**한다 — `deliver_after`를 DND 종료 이후로 잡는다
   - **`reason='green'` 카드는 알림 대상이 아니다** — 큐에만 적재된다 (룰 1)
   - 카드 자체는 항상 즉시 적재(안전망) — M4에서 보장되지만 테스트로 재확인
   - ⚠️ **브리핑 스케줄러를 컷할 경우 이 `deliver_after` 보류 로직도 함께 컷한다.**
     보류만 하고 flush가 없으면 알림이 영원히 안 나가는 반쪽 상태가 된다 (결정 1.18 컷라인 4)

4. **알림함 API** (계약서 §11): 목록(`unread_only`), 읽음 처리, unread-count.
   목록·카운트는 `deliver_after IS NULL OR deliver_after <= now()` 인 것만 노출한다

5. **SSE** (계약서 §12)
   - **`sse_manager`는 유저별 `asyncio.Queue` 브로드캐스트이며 인메모리다 → `--workers 1` 전제.**
     워커가 2개면 다른 워커에 붙은 클라이언트에게 이벤트가 가지 않는다.
     모듈 상단 주석에 이 전제와 **확장 시점의 정답(Redis Pub/Sub 또는 Postgres `LISTEN/NOTIFY`)** 을 남긴다
   - **인증은 단명 티켓 방식이다** (`05 §12.1`, `03 §7`)
     - `POST /sse/ticket` (Authorization 헤더 필요) → `{ticket, expires_in: 60}`. **TTL 60초·1회용**
     - `GET /sse/stream?ticket=…` — 검증 즉시 폐기. 만료·재사용 티켓은 401 `UNAUTHORIZED`
     - ⛔ **`?token=`으로 access token을 URL에 싣지 않는다.** 쿼리스트링은 프록시 로그·리퍼러·브라우저
       히스토리에 평문으로 남고 access token은 30분짜리 전체 API 자격증명이다
   - **FastAPI 네이티브 `EventSourceResponse`(`fastapi.sse`)를 쓴다.** `sse-starlette`를 추가하지 않는다 —
     네이티브가 `X-Accel-Buffering: no` 헤더와 15초 ping을 자동 처리한다
   - **스트림 시작 시 `notification.unread_count`를 1회 push**한다 (재연결 후 뱃지 동기화의 유일한 수단)
   - access token 만료 시 서버가 스트림을 종료한다(프론트가 refresh 후 새 티켓으로 재연결)
   - **이벤트 9종 발행 연결** (`05 §12.3`): `answer.completed` · `answer.updated` · `card.created` ·
     `card.resolved` · `briefing.ready`(M6) · **`document.ingested`**(M2에서 만든 훅을 여기 배선) ·
     `notification.created` · `notification.unread_count` · `sync.completed`(M8)
   - **재연결 규약** — Railway는 SSE를 **15분에 강제 종료**한다(하트비트 없으면 5분).
     재연결은 예외가 아니라 정상 동작이므로 **서버는 재연결을 전제로 설계한다**:
     SSE에는 재생(replay) 계약이 없고, 프론트는 재연결 시 구독 리소스를 전량 재조회한다.
     이 규약을 `docs/05-api-contract.md §12.2`와 일치하도록 구현·문서화한다

6. **스케줄러 잡** (`core/scheduler.py` — APScheduler, 앱 lifespan에서 초기화)
   - **만료 스위퍼 (10분 주기)** — 조건 3개를 **전부** 만족할 때만 `expired`로 내린다 (D14)
     ```
     state = 'draft'
       AND expires_at < now()
       AND 연결된 review_cards 중 status ∈ {pending, deferred} 인 것이 없을 것
     ```
     **살아 있는 카드 밑의 답변을 스위퍼가 죽이면 안 된다.** 담당자가 72h 이후에 지연(deferred) 카드를
     처리해도 그 답변은 여전히 `draft`여야 `draft → verified` 확정이 성립한다.
     세 번째 조건을 빠뜨리면 데모에서 "담당자가 카드를 눌렀는데 409"가 난다.
     전환 시 `answer.expired` 이벤트
   - **좀비 회수 잡 (신규)** — `questions.status='processing' AND created_at < now() - interval '5 minutes'`
     → `failed` + `review_cards(reason='failed', answer_id=NULL)` 생성 + `answer.failed` 알림 (D23).
     프로세스가 죽어 파이프라인의 `except`조차 못 탄 경우의 안전망이다 (M3의 `# TODO(M5)` 해소)
   - **APScheduler는 프로세스마다 중복 발화한다 → `--workers 1` 전제.** 잡 등록부에 주석으로 남긴다

7. 상세 조회 응답의 `accuracy_context` (계약서 §6) — 등급별 `verified_rate` 계산 헬퍼.
   **표본 30건 미만이면 `verified_rate=null` + `sufficient=false` + `message`**로 내린다 (D25).
   §13 `metrics.grade_accuracy[]`와 **동일한 아이템 shape**이어야 한다(M7에서 재사용)

## 완료 기준

- **SSE 티켓 테스트**: `POST /sse/ticket` → `GET /sse/stream?ticket=` 성공 /
  **만료·재사용 티켓 401** / **access token을 `?token=`으로 넘기는 경로가 존재하지 않음**
- SSE 이벤트 테스트: 질문 파이프라인 완료 → 질문자 스트림에 `answer.completed` 수신 (httpx 스트리밍 테스트)
- 스트림 시작 시 `notification.unread_count`가 1회 오는지
- urgent 🔴 → 담당자 즉시 알림(`deliver_after IS NULL`) / 비긴급 → **알림함 미노출 + `deliver_after` 설정됨** ·
  카드는 존재 / DND 중 urgent → 보류 / **🟢 카드 → 알림 없음**
- **만료 스위퍼 테스트 (시간 주입)**: 살아 있는 카드가 **없으면** `expired` 전환 /
  **pending·deferred 카드가 걸려 있으면 `draft` 유지** — 두 케이스를 각각 단언한다
- **좀비 회수 테스트**: `processing`으로 6분 방치된 질문 → `failed` + `reason='failed'` 카드 생성
- `uv run pytest` 통과 → 커밋 `feat(M5): notifications, sse, expiry sweeper`
