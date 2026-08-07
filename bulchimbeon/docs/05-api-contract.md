# 05. API 계약서 (프론트엔드 전달용)

> 이 문서가 프론트 팀과의 **단일 계약**이다. 구현과 어긋나면 구현을 고치거나 이 문서를 먼저 갱신한다.
> 라이브 스키마: Swagger UI `GET /docs` (배포 URL 공유). 여기서는 의미·규칙 중심으로 정의한다.

## 1. 공통

### 1.1 베이스
- Base URL: `{HOST}/api/v1`
- 인증: `Authorization: Bearer {access_token}` (SSE만 예외 — **1회용 ticket**, §12)
- Content-Type: `application/json` (파일 업로드만 `multipart/form-data`)
- 시각: ISO 8601 UTC (`2026-08-06T02:00:00Z`)

### 1.2 페이지네이션
`?limit=20&offset=0` → 응답 `{ "items": [...], "total": 132, "limit": 20, "offset": 0 }`

### 1.3 등급·상태 enum (전 화면 공통)
- `grade`: `green`(🟢 즉답) | `yellow`(🟡 확인 대기) | `red`(🔴 보류)
- `answer.state`: `draft`(참고) | `verified`(확정) | `under_review`(재검토) | `expired`(만료) | `rejected`(반려)
- `question.status`: `processing` | `answered` | `held` | `failed`
- `role`: `answerer`(담당자) | `asker`(질문자)

### 1.4 에러 포맷
```json
{ "error": { "code": "FORBIDDEN_ROLE", "message": "담당자만 수행할 수 있습니다." } }
```
| HTTP | code 예시 | 의미 |
| --- | --- | --- |
| 400 | `VALIDATION_ERROR` `UNSUPPORTED_FILE_TYPE` | 요청 형식 오류 |
| 401 | `UNAUTHORIZED` `TOKEN_EXPIRED` | 인증 실패 |
| 403 | `FORBIDDEN_ROLE` `NOT_MEMBER` | 권한 없음 |
| 404 | `NOT_FOUND` | |
| 409 | `ALREADY_RESOLVED` `DUPLICATE_FEEDBACK` `INVITE_ALREADY_JOINED` | 중복/충돌 (기능 4.3 중복 차단) |
| 409 | `FEEDBACK_NOT_ALLOWED` | 피드백 불가 상태의 답변 (§6 — `draft`·`verified`만 허용) |
| 409 | `PIPELINE_IN_PROGRESS` | 파이프라인 진행 상태 때문에 거부된 요청 (§6 `PATCH /questions/{id}`) |
| 409 | `INVALID_CARD_ACTION` | 카드 `reason`에 유효하지 않은 액션 (§7 카드 액션 매트릭스) |
| 422 | `PIPELINE_FAILED` | 파이프라인 처리 불가 |
| 500 | `INTERNAL_ERROR` | |

**409 `ALREADY_RESOLVED`는 기존 처리 결과를 함께 반환한다.** 모바일에서 네트워크 재시도가 잦으므로,
프론트는 이 body로 *"내 요청이 성공한 것"* 과 *"다른 기기/다른 사람이 이미 처리한 것"* 을 판별한다.

```json
{
  "error": {
    "code": "ALREADY_RESOLVED",
    "message": "이미 처리된 카드입니다.",
    "resolution": "approved",
    "resolved_at": "2026-08-06T01:20:11Z",
    "resolved_by": { "id": "u-2", "name": "Alex" }
  }
}
```

### 1.5 i18n 소유 주체 (문자열은 누가 만드는가)

| 문자열 | 생성 주체 | 언어 기준 |
| --- | --- | --- |
| `answer.disclaimer`, `held_info.message`, `failure_info.message` | 서버 | **수신자 `users.language`** |
| 알림 `title` / `body` (§11), 정정·반려·유지 사유 안내 문구 | 서버 | **수신자 `users.language`** |
| `error.message` (§1.4) | 서버 | **개발자용 한국어 고정** |

- 서버가 만드는 **사용자 표시 문자열**은 항상 수신자의 `users.language`로 생성된다. 프론트는 그대로 렌더한다.
- 반면 `error.message`는 로깅·디버깅용이다. **프론트는 `error.code`로 분기해 자체 문안을 표시한다.**
  `error.message`를 그대로 사용자에게 노출하지 말 것.
- 답변 본문(`content_ko`/`content_en`)은 언어 필드가 분리되어 있으므로 프론트가 `users.language`로 선택한다.

---

## 2. 인증 `/auth`

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| POST | `/auth/signup` | 가입 `{email, password, name, language(ko\|en), timezone}` → 201 유저 |
| POST | `/auth/login` | `{email, password}` → `{access_token, refresh_token, user}` |
| POST | `/auth/refresh` | `{refresh_token}` → 새 토큰 쌍 |
| GET | `/auth/me` | 내 정보 + 소속 프로젝트 요약 |

```json
// POST /auth/login 200
{
  "access_token": "eyJ...", "refresh_token": "eyJ...", "token_type": "bearer",
  "user": { "id": "u-1", "email": "jisoo@ex.com", "name": "지수", "language": "ko", "timezone": "Asia/Seoul" }
}
```

```json
// GET /auth/me 200 — 앱 부팅 시 1회 호출. 프로젝트 스위처·역할 분기의 원천
{
  "user": { "id": "u-1", "email": "jisoo@ex.com", "name": "지수", "language": "ko", "timezone": "Asia/Seoul" },
  "projects": [
    {
      "id": "p-1", "name": "Acme 파트너십", "role": "asker", "member_status": "active",
      "away_mode": false, "unread_notifications": 3, "pending_cards": 0
    },
    {
      "id": "p-2", "name": "Globex 연동", "role": "answerer", "member_status": "active",
      "away_mode": true, "unread_notifications": 1, "pending_cards": 5
    }
  ],
  "unread_notifications_total": 4
}
```
- `role`이 `answerer`인 프로젝트에서만 담당자 화면(§7 큐 · §8 브리핑)을 노출한다.
- `pending_cards`는 담당자 프로젝트에서만 의미 있는 값이며, `asker`에게는 항상 `0`이다.
- `member_status: "left"`(§3 탈퇴)인 프로젝트는 이 목록에 **포함되지 않는다**.

---

## 3. 프로젝트 `/projects`

| 메서드 | 경로 | 권한 | 설명 |
| --- | --- | --- | --- |
| POST | `/projects` | 로그인 | 생성. **생성자 = 담당자** `{name, description}` |
| GET | `/projects` | 로그인 | 내 프로젝트 목록 (역할 포함) |
| GET | `/projects/{id}` | 멤버 | 상세 + settings + away_mode + 내 역할 |
| PATCH | `/projects/{id}/settings` | 담당자 | settings 부분 갱신 (기능 1.4 — 아래 키만 허용) |
| PATCH | `/projects/{id}/away-mode` | 담당자 | `{away_mode: true\|false}` 퇴근 모드. **꺼도 카드는 유지** (룰 9) |
| POST | `/projects/{id}/invite-code` | 담당자 | 초대 코드 재발급 → `{invite_code}` |
| POST | `/projects/join` | 로그인 | `{invite_code}` → asker로 참여. 중복 참여 409 |
| GET | `/projects/{id}/members` | 멤버 | 멤버 목록 (역할·상태) |
| POST | `/projects/{id}/transfer-answerer` | 담당자 | `{new_answerer_id}` 담당자 교체 + 미처리 카드·브리핑 이관 (기능 6.3) |
| DELETE | `/projects/{id}/members/{user_id}` | 담당자 | 멤버 내보내기 (asker만 가능) |
| POST | `/projects/{id}/leave` | asker | **자진 탈퇴** (D18). 담당자는 **403 `FORBIDDEN_ROLE`** |
| GET/PUT | `/projects/{id}/guidelines` | GET 멤버 / PUT 담당자 | 응답 지침 `{content}` (기능 1.3) |

```json
// PATCH /projects/{id}/settings 요청 예 (부분 갱신)
{ "green_threshold": 85, "briefing_hour": 8, "dnd_start": "23:00", "dnd_end": "06:30" }
```

**허용 키 전체 목록** (`04-data-model.md §3`과 1:1. 목록 밖의 키는 **400 `VALIDATION_ERROR`**)

| 키 | 타입 | 기본값 | 비고 |
| --- | --- | --- | --- |
| `green_threshold` | int 0~100 | 80 | 🟢 하한 |
| `yellow_threshold` | int 0~100 | 50 | 🟡 하한 |
| `grounding_min` | int 0~100 | 60 | 문장 3개 이상일 때만 적용 |
| `s_floor` | float 0~1 | 0.25 | 유사도 리스케일 하한 (⚠️ 캘리브레이션 전 잠정값) |
| `s_ceil` | float 0~1 | 0.65 | 유사도 리스케일 상한 (⚠️ 잠정값) |
| `similarity_floor` | float 0~1 | 0.25 | top-1 원시 유사도 미달 시 강제 🔴 `no_evidence` 기본값은 `s_floor`와 동일 |
| `reuse_threshold` | float 0~1 | 0.92 | **원시 코사인** (리스케일 안 함, ⚠️ 잠정값) |
| `similar_threshold` | float 0~1 | 0.85 | `similar_official_qa` 첨부 하한 (원시 코사인) (⚠️ 잠정값) |
| `draft_expire_hours` | int | 72 | draft 만료 |
| `max_lessons` | int | 30 | 초과 시 §10 `cleanup_suggestions[]` |
| `retrieval_top_k` | int | 6 | 검색 top-k |
| `daily_llm_call_limit` | int | 500 | 초과 시 강제 🔴 `quota_exceeded` |
| `saved_wait_assumption_hours` | int | 24 | §13 `saved_wait_hours.assumption_hours`의 원천 |
| `briefing_hour` | int 0~23 | 9 | 브리핑 발송 시각 |
| `dnd_start` / `dnd_end` | `"HH:MM"` | `"22:00"` / `"07:00"` | 방해 금지 구간 |

> ⚠️ **`briefing_timezone`은 삭제되었다.** 브리핑·DND 시각 판정은 항상 **현재 담당자(`projects.answerer_id`)의
> `users.timezone`**을 따른다. 담당자 교체 시 자동으로 따라가며, 프로젝트 설정으로 덮어쓸 수 없다.
> 이 키를 PATCH하면 400 `VALIDATION_ERROR`다.

---

## 4. 문서 `/documents` (담당자 전용 쓰기, 멤버 읽기)

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| POST | `/projects/{id}/documents` | multipart `file` + `title?` + `auto_activate?`(기본 true). MD/TXT/PDF/DOCX. → 201 문서+버전1, 인제스트 비동기 시작 |
| POST | `/documents/{id}/versions` | 재업로드 = 새 버전 (룰 5). multipart 동일 |
| GET | `/projects/{id}/documents` | 목록 (활성 버전 요약·ingest_status 포함) |
| GET | `/documents/{id}` | 상세 + 버전 목록 |
| PATCH | `/documents/{id}/versions/{vid}/activate` | 활성 버전 전환 (기능 1.2). **이 시점에 재검토 연쇄 실행** → 응답에 `review_cascade_count` |
| GET | `/documents/{id}/versions/{vid}/content` | 파싱된 원문 텍스트 (근거 열람용, 기능 2.2) |
| DELETE | `/documents/{id}` | soft delete |

```json
// PATCH .../activate 200
{ "document_id": "d-1", "active_version": 2, "review_cascade_count": 4,
  "message": "이 문서를 근거로 한 확정 답변 4건이 재검토 대상입니다." }
```
- **인제스트 완료 신호**: 업로드/새 버전 응답은 `ingest_status: "pending"` 상태로 즉시 201을 준다.
  완료는 SSE `document.ingested` (§12)로 통지된다. SSE 미사용 시 `GET /projects/{id}/documents`를 3초 간격 폴링.
- **근거 원문 열람 URL 구성** (기능 2.2): §6 `citations[]`의 `document_id`·`document_version_id`를 그대로 넣어
  `GET /documents/{document_id}/versions/{document_version_id}/content`를 호출한다.
  응답 `{document_id, version_id, version_no, title, mime, content}` — 프론트는 `heading_path`로 스크롤 앵커를 잡고
  `quote` 문자열을 하이라이트한다.

---

## 5. 외부 연동 `/integrations` (담당자 전용, 기능 1.5)

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| POST | `/projects/{id}/integrations` | `{provider: "notion"\|"github", config}` 연결 등록 |
| GET | `/projects/{id}/integrations` | 목록 + last_synced_at·상태 (토큰은 마스킹) |
| POST | `/integrations/{id}/sync` | **수동 동기화** 트리거 → 202. 완료 시 SSE `sync.completed` |
| DELETE | `/integrations/{id}` | 해제 |

```json
// notion config
{ "token": "ntn_...", "page_ids": ["..."] }
// github config — public repo면 token 생략 가능
{ "token": "ghp_...", "repo": "acme/partner-docs", "branch": "main", "path_glob": "docs/**/*.md" }
```
동기화 결과: 신규 파일 → 새 문서, 변경 파일 → 새 버전(활성화 + 재검토 연쇄 = 룰 5 동일 적용).

---

## 6. 질문 `/questions`

| 메서드 | 경로 | 권한 | 설명 |
| --- | --- | --- | --- |
| POST | `/projects/{id}/questions` | **asker 전용** | 질문 접수 → 202. **담당자는 자기 프로젝트에 질문할 수 없다 → 403 `FORBIDDEN_ROLE`** (D17) |
| GET | `/projects/{id}/questions` | 멤버 | 목록 (질문자는 기본 자기 것, 담당자는 전체) |
| GET | `/questions/{id}` | 멤버 | 질문 상세 (핵심 화면) |
| PATCH | `/questions/{id}` | 질문 작성자 | 긴급도 변경 `{urgency}`. 파이프라인 완료 후 → 409 |
| POST | `/answers/{id}/feedback` | asker | 크로스체크 (맞았다/달랐다) |

### POST `/projects/{id}/questions` — 질문 접수 (asker)
```json
// 요청
{ "content_ko": "환불 정책이 일본 리전에도 동일하게 적용되나요?", "urgency": "normal" }
// 202 응답 — 파이프라인 비동기 시작 (등급 확정 목표 ≤25초 = LLM_PIPELINE_DEADLINE_SECONDS)
{ "question_id": "q-9", "status": "processing", "suggest_urgent": false,
  "created_at": "2026-08-06T01:10:22Z" }
```
- **`created_at`은 진행 표시 전용이다.** 프론트는 `now - created_at`으로 경과 초를 그리되, 예산은 `LLM_PIPELINE_DEADLINE_SECONDS`(기본 25초) 기준으로 한다
  (서버 시각 기준이므로 클라이언트 시계를 쓰지 말 것).
- `suggest_urgent: true`면 프론트는 "급한 질문인가요?" 를 노출하고, 질문자가 동의하면 `PATCH /questions/{id}`를 호출한다.
- 완료 시 SSE `answer.completed` 수신 → `GET /questions/{id}` 재조회. (SSE 미사용 시 2~3초 폴링)
- **담당자 권한 주의**(D17): 담당자는 자기 프로젝트에 질문할 수 없다. 담당자 계정으로 이 화면에 진입할 수 없도록
  프론트가 입력창을 감추되, 서버도 403으로 막는다.

### PATCH `/questions/{id}` — 긴급도 변경 (질문 작성자)
```json
// 요청
{ "urgency": "urgent" }
// 200
{ "id": "q-9", "urgency": "urgent", "status": "processing" }
```
- **허용 창은 `status="processing"` 동안뿐이다.** 파이프라인이 끝난 뒤(`answered`/`held`/`failed`) 호출하면
  **409 `PIPELINE_IN_PROGRESS`** (body에 현재 `status` 포함). 이미 만들어진 카드의 `is_urgent`는 바뀌지 않는다.
- 변경 가능한 필드는 `urgency`(`normal` \| `urgent`)뿐이다. 질문 본문은 수정할 수 없다.

### GET `/projects/{id}/questions?mine=true&status=&grade=&limit=&offset=`
목록 (질문자는 기본 자기 것, 담당자는 전체). **목록 아이템만으로 질문자 채팅 목록 화면을 그릴 수 있다 — 상세 호출 불필요.**

```json
// 200 — §1.2 페이지네이션 봉투
{
  "items": [
    { "id":"q-9", "content_ko":"환불 정책이 일본 리전에도 동일하게 적용되나요?",
      "status":"held", "grade":"red", "matching_rate":null, "state":null,
      "created_at":"2026-08-06T01:10:22Z",
      "feedback_summary": null },
    { "id":"q-8", "content_ko":"일본 리전 환불 정책도 동일하게 적용되나요?",
      "status":"answered", "grade":"green", "matching_rate":null, "state":"verified",
      "created_at":"2026-08-06T02:40:00Z",
      "feedback_summary": { "correct":0, "different":0, "my_feedback":null } },
    { "id":"q-5", "content_ko":"부분 환불도 30일 안에 신청해야 하나요?",
      "status":"answered", "grade":"yellow", "matching_rate":65, "state":"draft",
      "created_at":"2026-08-06T00:55:10Z",
      "feedback_summary": { "correct":0, "different":0, "my_feedback":null } },
    { "id":"q-7", "content_ko":"레이트 리밋은 분당 몇 건인가요?",
      "status":"processing", "grade":null, "matching_rate":null, "state":null,
      "created_at":"2026-08-06T01:01:55Z", "feedback_summary":null }
  ],
  "total": 12, "limit": 20, "offset": 0
}
```

| 필드 | 설명 |
| --- | --- |
| `status` | `processing` \| `answered` \| `held` \| `failed` (§1.3) |
| `grade` | `processing`이면 `null`. `held`면 `"red"` |
| `matching_rate` | `held`·`processing`·재사용 답변이면 `null` |
| `state` | **발행된 답변이 없으면 `null`** (🔴 보류·처리 중 포함 — 🔴 초안은 카드에서만 노출된다). 있으면 `answer.state` |
| `feedback_summary` | 발행된 답변이 없으면 `null`. 있으면 상세와 동일 shape |

### GET `/questions/{id}` — 질문 상세 (핵심 화면)
```json
{
  "id": "q-5", "content_ko": "부분 환불도 30일 안에 신청해야 하나요?",
  "content_en": "Does a partial refund also have to be requested within 30 days?",
  "urgency": "normal", "status": "answered", "asked_by": {"id":"u-1","name":"지수"},
  "answer": {
    "id": "a-5", "grade": "yellow", "state": "draft",
    "matching_rate": 65, "search_score": 65, "grounding_score": 100,
    "content_ko": "환불 정책 문서 기준 기본 환불 기한은 구매 후 30일입니다. 부분 환불에도 동일한 30일 기한이 적용됩니다.",
    "source": "generated", "degraded_from_red": false,
    "expires_at": "2026-08-09T02:00:00Z",
    "disclaimer": "참고용 답변입니다. 담당자 확인 전입니다.",
    "citations": [
      { "id":"c-1", "chunk_id":"ch-12",
        "document_id":"d-1", "document_version_id":"dv-3",
        "doc_title":"Refund Policy", "version_no":1,
        "heading_path":["Refunds","Standard Refund Window"], "page_no":null,
        "quote":"Refunds are accepted within 30 days of purchase.", "similarity":0.51 },
      { "id":"c-2", "chunk_id":"ch-14",
        "document_id":"d-1", "document_version_id":"dv-3",
        "doc_title":"Refund Policy", "version_no":1,
        "heading_path":["Refunds","Partial Refunds"], "page_no":null,
        "quote":"Partial refunds follow the same 30-day window.", "similarity":0.47 }
    ],
    "feedback_summary": { "correct": 0, "different": 0, "my_feedback": null }
  },
  "similar_official_qa": { "id":"oq-3", "question_ko":"...", "answer_ko":"...", "similarity":0.87 },
  "held_info": null, "failure_info": null
}
```
- `grounding_score` = round(지지 문장 수 / **생성 시점 원본 문장 수** × 100)이므로 문장 수에 따라 취할 수 있는 값이 이산적이다. 1~2문장 답변은 `grounding_min` 미적용 — 하나라도 무근거면 🔴이므로 이 필드는 `0`/`100`만 나타난다 (`02` 룰 1, `06 §2` ⑤).
- `search_score`는 `citations[].similarity`(원시 코사인)를 `s_floor`~`s_ceil`로 리스케일한 값이다. **두 값을 같은 수로 적지 말 것.**

#### `citations[]` 필드 (기능 2.2 근거 원문 열람의 전제)

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `id` | string | citation 행 id |
| `chunk_id` | string | 근거 청크 id |
| `document_id` | string | **원문 열람 URL 구성용** |
| `document_version_id` | string | **원문 열람 URL 구성용** — 답변 생성 당시 활성 버전 |
| `doc_title` | string | 표시용 문서명 |
| `version_no` | int | 표시용 버전 순번 (URL에 쓰지 말 것 — id를 쓴다) |
| `heading_path` | string[] | 청크의 헤딩 경로. 원문 화면 스크롤 앵커·빵부스러기 표시 |
| `page_no` | int \| null | PDF일 때 1부터. MD/TXT는 `null` |
| `quote` | string | 인용 스니펫 — 원문 화면에서 하이라이트할 문자열 |
| `similarity` | float | **원시 코사인 유사도**(리스케일 전). `matching_rate`와 스케일이 다르므로 **% 로 표시하지 말 것** |

- 열람 URL: `GET /documents/{document_id}/versions/{document_version_id}/content` (§4).
  `document_id`·`document_version_id` 없이는 이 URL을 만들 수 없으므로 **두 필드는 필수 제공**이다.

#### 🔴 보류 (`status: "held"`)
`answer: null`, `held_info`가 채워진다.
```json
{ "status": "held", "answer": null,
  "held_info": { "reason": "conflict",
                 "message": "근거 문서가 충돌하여 담당자에게 전달했습니다.",
                 "card_status": "pending" } }
```
- `held_info.reason`: `conflict` \| `no_evidence` \| `low_confidence` \| `schema_failed` \| **`quota_exceeded`**
- `held_info.card_status`: `pending` \| `deferred` \| `resolved`
- `held_info.message`는 **수신자 `users.language` 기준 서버 생성 문자열**이다 (§1.5). 그대로 렌더한다.
- **DND 강등은 `low_confidence`에만 적용된다** (룰 6 · 결정 1.6). `conflict`·`no_evidence`·`schema_failed`·`quota_exceeded`는
  퇴근 모드/DND 시간대에도 🔴을 유지하므로 `held_info`가 그대로 내려온다. 즉 **이 4종은 시연 시각과 무관하게 🔴이다.**

#### held → answered 전이 (프론트 필수 처리)
> **held 질문이 담당자 확정으로 해소되면 status는 `answered`로 전환되고 `answer` 객체가 채워진다.
> `held_info`는 이력용으로 유지하되 `card_status`를 `resolved`로 갱신한다.**

```json
// 담당자가 카드를 approve/edit/answer-option 한 뒤의 GET /questions/{id}
{ "status": "answered",
  "answer": { "id":"a-9", "grade":"red", "state":"verified", "matching_rate":null,
              "content_ko":"일본 리전은 현지 법령에 따라 20일입니다.", "source":"generated",
              "expires_at":null, "disclaimer":null, "citations":[],
              "feedback_summary":{ "correct":0, "different":0, "my_feedback":null } },
  "held_info": { "reason":"no_evidence", "message":"...", "card_status":"resolved" } }
```
- 프론트는 `held_info != null && status == "answered"` 를 **"보류였다가 담당자가 답한 질문"** 으로 렌더한다
  (기존 🔴 말풍선 아래에 확정 답변을 잇는다). `held_info`가 남아 있다고 해서 보류로 표시하지 말 것.
- `reject`로 해소되면 `status`는 `held`를 유지하고 `held_info.card_status`만 `resolved`가 되며,
  반려 사유는 알림(§11)으로 전달된다.

#### 파이프라인 실패 (`status: "failed"`)
`answer: null`, `failure_info`가 채워진다 — `held_info`와 대칭 구조다 (D23).
```json
{ "status": "failed", "answer": null, "held_info": null,
  "failure_info": { "reason": "pipeline_error",
                    "message": "답변 생성에 실패했습니다. 담당자에게 전달했습니다.",
                    "card_status": "pending" } }
```
- `failure_info.reason`은 **로깅용 코드**다. 화면에는 `message`(수신자 언어)를 표시한다.
- 실패도 담당자 카드(`reason: "failed"`)가 생성되므로 질문이 유실되지 않는다. `card_status`는 그 카드의 상태다.

#### 재사용 답변 (D11)
| 필드 | 값 |
| --- | --- |
| `answer.source` | `"reused"` |
| `answer.grade` | `"green"` |
| `answer.state` | **`"verified"`** (초안 아님) |
| `answer.matching_rate` / `search_score` / `grounding_score` | **`null`** (원시 코사인 기준 판정이라 % 를 표시하지 않는다) |
| `answer.expires_at` | **`null`** (만료 스위퍼 대상 아님) |
| `answer.disclaimer` | disclaimer 대신 **`"공식 확정 답변입니다."`** |
| `answer.official_qa` | `{ "id":"oq-3", "question_ko":"...", "reuse_count": 4 }` |
| 카드 | **생성되지 않는다** |

#### 정확도 실적 표기 (선택 노출)
`answer.accuracy_context` — **§13 `metrics.grade_accuracy`의 아이템과 동일한 shape**이다.
```json
// 표본 충분
{ "grade":"yellow", "verified_rate":0.94, "sample":52, "window_days":30, "sufficient":true, "message":null }
// 표본 부족 (30건 미만, D25)
{ "grade":"yellow", "verified_rate":null, "sample":12, "window_days":30, "sufficient":false,
  "message":"검증 이력 부족 — 참고용" }
```
- `sufficient:false`면 `verified_rate`는 반드시 `null`이다. 프론트는 `%` 대신 `message`를 표시한다.

### POST `/answers/{id}/feedback` — 크로스체크 (asker)
```json
// 요청 — 달랐다 (note 필수)
{ "verdict": "different", "note": "부분 환불은 14일이라고 파트너가 말했어요" }
// 200
{ "answer_id": "a-5", "answer_state": "under_review", "message": "오류 신고됨, 재검토 중",
  "feedback_summary": { "correct": 0, "different": 1, "my_feedback": "different" },
  "recommend_approve": false }
```
```json
// 요청 — 맞았다 (note 생략 가능)
{ "verdict": "correct" }
// 200 — 이 건으로 correct가 2건이 되어 카드가 승인 추천으로 승격된 경우
{ "answer_id": "a-5", "answer_state": "draft", "message": "확인 감사합니다.",
  "feedback_summary": { "correct": 2, "different": 0, "my_feedback": "correct" },
  "recommend_approve": true }
```
- **응답에 갱신된 `feedback_summary`가 항상 포함된다.** 프론트는 이 값으로 낙관적 업데이트를 확정하며,
  `GET /questions/{id}` 재조회가 필요 없다.
- **허용 상태**(D12): `answer.state ∈ {draft, verified}` 만 허용.
  `expired`·`rejected`·`under_review` 답변에 피드백 시 **409 `FEEDBACK_NOT_ALLOWED`**.
  프론트는 이 상태에서 피드백 버튼을 비활성화한다.
- **유저당 1건**: 같은 유저가 다시 제출하면 verdict가 달라도 **409 `DUPLICATE_FEEDBACK`**. verdict 변경은 지원하지 않는다.
- `correct` 2건 누적 시 서버가 자동으로 카드 `recommend_approve` 처리 → 응답 `recommend_approve: true`.
- 확정(verified) 답변에도 `different` 가능 → 재검토 전환 (룰 3), `answer_state: "under_review"`.

---

## 7. 확인 카드 큐 `/review-cards` (담당자 전용)

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| GET | `/projects/{id}/review-cards?status=pending\|deferred\|resolved` | 큐 목록. 정렬: **승인 추천 → 긴급 → 오래된 순** |
| GET | `/review-cards/{id}` | 카드 상세. **최초 조회 시 first_viewed_at 기록** (지표) |
| POST | `/review-cards/{id}/approve` | 승인 → 답변 확정 + 공식 Q&A 편입 |
| POST | `/review-cards/{id}/edit` | `{content_en}` 수정 저장 → 번역·확정 + 정정 알림 + 교훈 후보 |
| POST | `/review-cards/{id}/answer-option` | `{index}` **선택지 탭 응답** — `question_struct.options[index]`로 확정 |
| POST | `/review-cards/{id}/keep` | 원안 유지 `{reason_en}` → 확정 복귀 + 사유 알림 (재검토 카드 전용) |
| POST | `/review-cards/{id}/reject` | 반려 `{reason_en}` → 질문자에게 사유 전달 |
| POST | `/review-cards/{id}/defer` | 출근 후 처리 `{until?}` → `deferred` (질문자엔 "확인 대기 중" 유지) |
| POST | `/projects/{id}/review-cards/bulk-keep` | `{document_version_id}` 문서 갱신 재검토 묶음 **전체 유지** (룰 5) |

### GET `/projects/{id}/review-cards` — 큐 목록 아이템

**큐 화면은 이 목록만으로 완성된다.** 아래 필드가 곧 카드 행의 렌더 계약이다.

```json
// 200 — §1.2 페이지네이션 봉투
{
  "items": [
    { "id":"rc-2", "reason":"green", "status":"pending", "is_urgent":false,
      "recommend_approve":true, "grade":"green",
      "question_preview_en":"What is the standard refund window?",
      "question_preview_ko":"기본 환불 기한은 며칠인가요?",
      "created_at":"2026-08-05T22:41:00Z", "first_viewed_at":null },
    { "id":"rc-4", "reason":"red", "status":"pending", "is_urgent":false,
      "recommend_approve":false, "grade":"red",
      "question_preview_en":"Does the refund policy apply identically to the Japan region?",
      "question_preview_ko":"환불 정책이 일본 리전에도 동일하게 적용되나요?",
      "created_at":"2026-08-06T01:12:00Z", "first_viewed_at":"2026-08-06T01:20:02Z" }
  ],
  "total": 7, "limit": 20, "offset": 0
}
```

| 필드 | 설명 |
| --- | --- |
| `reason` | `green` \| `yellow` \| `red` \| `feedback` \| `doc_update` \| `failed` — 액션 매트릭스의 키 |
| `status` | `pending` \| `deferred` \| `resolved` |
| `is_urgent` | 즉시 알림 대상이었는지. 정렬 2순위 |
| `recommend_approve` | 맞았다 2건↑ 승인 추천 (룰 3). 정렬 1순위 |
| `grade` | 원본 답변 등급. `reason="failed"`면 `null` |
| `question_preview_en` / `question_preview_ko` | 질문 앞부분(120자) 미리보기. 목록에서 상세를 부르지 않기 위한 필드 |
| `first_viewed_at` | 아직 안 본 카드면 `null` → "NEW" 뱃지 |

> ⚠️ **프리페치 금지.** `GET /review-cards/{id}`는 최초 조회 시 `first_viewed_at`을 기록한다
> (카드 처리 시간 지표 §13 `card_handle_30s_rate`의 시작점).
> **목록에서 호버 프리페치·백그라운드 선행 조회를 하지 말 것 — 지표가 파괴된다.**
> 목록 렌더에 필요한 값은 전부 위 아이템에 있으므로 상세를 미리 부를 이유가 없다.
> 상세 호출은 **담당자가 실제로 카드를 열었을 때 정확히 1회**여야 한다.

```json
// GET /review-cards/{id} 200 — 모바일 팝업 UI 대응 (상단 질문 / 중단 답변+근거 / 하단 액션)
{
  "id": "rc-4", "reason": "red", "status": "pending", "is_urgent": false,
  "recommend_approve": false, "grade": "red",
  "created_at": "2026-08-06T01:12:00Z", "first_viewed_at": "2026-08-06T01:20:02Z",
  "deferred_until": null, "answer_id": "a-9",
  "question": { "id":"q-9", "content_en":"Does the refund policy apply identically to the Japan region?", "content_ko":"..." },
  "question_struct": {
    "background": "The asker is preparing the JP launch checklist. Refund Policy v1 defines a 30-day window but has no region-specific clause.",
    "question": "Does the 30-day refund window apply to the Japan region as-is?",
    "options": ["Yes, identical for all regions", "No, Japan has a different window", "Depends on product category"]
  },
  "draft_answer": {
    "content_en": "The refund policy defines a 30-day window but states no Japan-specific rule.",
    "citations": [
      { "id":"c-1", "chunk_id":"ch-12",
        "document_id":"d-1", "document_version_id":"dv-3",
        "doc_title":"Refund Policy", "version_no":1,
        "heading_path":["Refunds","Standard Window"], "page_no":null,
        "quote":"Refunds are accepted within 30 days of purchase.", "similarity":0.78 }
    ]
  },
  "pending_feedbacks": []
}
```
- 상세는 목록 아이템의 **상위 집합**이다 (`question_preview_*`만 전체 `question`으로 대체).
- `draft_answer.citations[]`는 §6 `citations[]`와 **동일 스키마**다 (`document_id`·`document_version_id` 포함 → 팝업에서 근거 원문 열람).
- `question_struct`는 🔴 전달용이며 `reason="red"`에만 채워진다. 그 외에는 `null`.
- `reason="failed"`면 `answer_id: null`, `draft_answer: null`, `question_struct: null`이다 (D23).

### 7.1 카드 액션 매트릭스 (reason별 유효 액션)

`O`가 아닌 액션을 호출하면 **409 `INVALID_CARD_ACTION`** 이다. 프론트는 이 표대로 버튼을 렌더한다.

| `reason` | approve | edit | answer-option | keep | reject | defer | 비고 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `green` | O | O | X | X | O | O | 브리핑·알림 제외. `recommend_approve=true`가 되면 브리핑 최상단 (룰 3) |
| `yellow` | O | O | X | X | O | O | |
| `red` | O | O | **O** | X | O | O | `question_struct.options[]` 보유 → 선택지 탭 응답 가능 |
| `feedback` | X | O | X | **O** | O | O | 재검토 카드(`answer.state=under_review`). 확정 복귀는 `edit`/`keep`뿐 |
| `doc_update` | X | O | X | **O** | O | O | 재검토 카드. `bulk-keep` 대상 |
| `failed` | X | O | X | X | O | O | 승인할 원안이 없다 — 담당자가 `edit`으로 직접 작성 |

- **`keep`은 재검토 카드(`feedback`·`doc_update`)에만 유효하다.** `green`/`yellow`/`red`/`failed`에 호출하면 409 `INVALID_CARD_ACTION`.
  (근거: `04 §6` 상태 전이에서 `under_review → verified`의 경로가 "수정 저장 or 원안 유지"뿐)
- **`approve`는 아직 확정된 적 없는 카드(`green`·`yellow`·`red`)에만 유효하다.**
- **`answer-option`은 `question_struct.options[]`가 있는 카드(`red`)에만 유효하다.**
- `POST /projects/{id}/review-cards/bulk-keep`은 `reason="doc_update"` 묶음 전용이다.
- **`approve`·`keep`·`reject`의 200 응답은 아래 `edit` 응답과 동일한 shape이다**
  (`official_qa_id`·`lesson_candidate_id`는 해당 없으면 `null`. `reject`는 `answer.state: "rejected"`).
  모든 처리 응답에 미해소 피드백 해소 건수(`resolved_feedbacks`)가 포함된다 (룰 9 담당자 우선).
- 처리 완료 카드 재처리 시 **409 `ALREADY_RESOLVED`** (중복 차단, §1.4에 기존 `resolution`·`resolved_at` 포함).

```json
// POST /review-cards/{id}/edit 요청/응답
{ "content_en": "The 30-day window applies to all regions except Japan, where local law requires 20 days." }
// 200
{ "answer": { "id":"a-9", "state":"verified", "content_ko":"30일 환불 기한은 모든 리전에 적용되지만, 일본은 현지 법령에 따라 20일입니다.", "content_en":"..." },
  "official_qa_id": "oq-7", "lesson_candidate_id": "ls-2",
  "resolved_feedbacks": 0 }
```

### 7.2 POST `/review-cards/{id}/answer-option` — 선택지 탭 응답

**"30초 컷"의 실현 수단이다.** 담당자는 모바일 팝업에서 `question_struct.options[]` 버튼을 한 번 눌러 확정한다.
동작은 `edit`과 동일하되, 본문이 **선택한 선택지 텍스트**다.

```json
// 요청 — index는 0부터
{ "index": 1 }
// 200 — edit 와 동일한 응답 shape + 선택 내역 echo
{ "answer": { "id":"a-9", "state":"verified",
              "content_en":"No, Japan has a different window",
              "content_ko":"아니요, 일본은 기한이 다릅니다." },
  "official_qa_id": "oq-8", "lesson_candidate_id": "ls-3",
  "resolved_feedbacks": 0,
  "selected_option": { "index": 1, "text": "No, Japan has a different window" } }
```
- `index`가 `question_struct.options[]` 범위를 벗어나면 **400 `VALIDATION_ERROR`**.
- `question_struct`가 없는 카드(= `red`가 아닌 카드)면 **409 `INVALID_CARD_ACTION`**.
- 확정 후 처리는 `edit`과 완전히 동일하다 — 번역·공식 Q&A 편입·정정 알림·교훈 후보·피드백 해소.

### 7.3 POST `/review-cards/{id}/defer` — 나중에 처리

```json
// 요청 — body 전체 생략 가능
{ "until": "2026-08-07T13:00:00Z" }
// 200
{ "id": "rc-4", "status": "deferred", "deferred_until": "2026-08-07T13:00:00Z" }
```
- **`until` 생략 시 기본값 = 다음 `briefing_hour`** (담당자 `users.timezone` 기준, D15).
  프론트는 응답의 `deferred_until`을 그대로 표시하면 되며 기본값을 자체 계산하지 않는다.
- 질문자 화면에는 여전히 "확인 대기 중"으로 보인다. **카드는 사라지지 않는다** (룰 9).

---

## 8. 아침 브리핑 `/briefing` (담당자 전용)

### GET `/projects/{id}/briefing/today`

**모든 카드 배열은 §7의 큐 목록 아이템 스키마를 그대로 쓴다.** 배열마다 필드가 달라 N+1 상세 호출이 필요해지는 일이 없도록
`recommend_approve` / `pending_cards` / `deferred_cards` / `doc_review_bundles[].cards`가 **전부 동일 shape**이다.

```json
{
  "date": "2026-08-06", "timezone": "America/New_York",
  "recommend_approve": [
    { "id":"rc-2", "reason":"green", "status":"pending", "is_urgent":false,
      "recommend_approve":true, "grade":"green",
      "question_preview_en":"What is the standard refund window?",
      "question_preview_ko":"기본 환불 기한은 며칠인가요?",
      "created_at":"2026-08-05T22:41:00Z", "first_viewed_at":null,
      "correct_count":2 }
  ],
  "pending_cards": [
    { "id":"rc-4", "reason":"red", "status":"pending", "is_urgent":false,
      "recommend_approve":false, "grade":"red",
      "question_preview_en":"Does the refund policy apply identically to the Japan region?",
      "question_preview_ko":"환불 정책이 일본 리전에도 동일하게 적용되나요?",
      "created_at":"2026-08-06T01:12:00Z", "first_viewed_at":null }
  ],
  "deferred_cards": [
    { "id":"rc-3", "reason":"yellow", "status":"deferred", "is_urgent":false,
      "recommend_approve":false, "grade":"yellow",
      "question_preview_en":"...", "question_preview_ko":"...",
      "created_at":"2026-08-05T18:02:00Z", "first_viewed_at":"2026-08-05T18:30:00Z" }
  ],
  "doc_review_bundles": [
    { "document_id":"d-1", "document_version_id":"dv-3", "title":"Refund Policy",
      "new_version":2, "affected_count":4,
      "cards": [
        { "id":"rc-9", "reason":"doc_update", "status":"pending", "is_urgent":false,
          "recommend_approve":false, "grade":"green",
          "question_preview_en":"...", "question_preview_ko":"...",
          "created_at":"2026-08-06T00:05:00Z", "first_viewed_at":null }
      ] }
  ],
  "stats_snapshot": { "auto_answer_rate": 0.74, "questions_24h": 12 }
}
```
- `recommend_approve[]` 아이템에만 `correct_count`가 추가로 붙는다(추천 근거 표시용). 나머지 필드는 §7 목록 아이템과 동일.
- `doc_review_bundles[].document_version_id`는 `POST /projects/{id}/review-cards/bulk-keep`에 그대로 넘기는 값이다 (룰 5 전체 유지).
- **`timezone`은 파생값이다.** 현재 담당자(`projects.answerer_id`)의 `users.timezone`을 그대로 내려준다.
  `settings.briefing_timezone`은 **삭제되었으므로**(§3) 이 값을 프로젝트 설정으로 바꿀 수 없다. 담당자 교체 시 자동으로 따라간다.
- **🟢 카드는 브리핑에 포함되지 않는다.** 단, `correct` 2건 누적으로 `recommend_approve=true`가 되면
  `recommend_approve[]` 최상단에 올라온다 (룰 3 · 결정 1.4). 큐(§7)에는 항상 적재되어 있다.
- 브리핑 시각(`settings.briefing_hour`, 담당자 타임존)에 스케줄러가 `briefing.ready` 알림 + SSE 발행. 이 GET은 언제든 호출 가능(수동 새로고침 = 데모 플랜B).

---

## 9. 공식 Q&A `/official-qas`

| 메서드 | 경로 | 권한 | 설명 |
| --- | --- | --- | --- |
| GET | `/projects/{id}/official-qas?query=&limit=` | 멤버 | 확정 지식 목록/검색 (query는 키워드 매칭) |
| GET | `/official-qas/{id}` | 멤버 | 상세 (ko/en 쌍, 출처 답변, reuse_count, status) |
| DELETE | `/official-qas/{id}` | **담당자** | **`status`를 `archived`로 전환** (물리 삭제 아님) |

```json
// DELETE /official-qas/{id} 200
{ "id": "oq-3", "status": "archived", "archived_at": "2026-08-06T04:10:00Z" }
```
- `archived`는 **재사용 대상에서도 검색 대상에서도 제외**된다. 목록/검색 응답에 기본으로 나타나지 않는다.
- 이미 이 Q&A를 근거로 발행된 답변은 그대로 남는다(이력 보존).
- `status`: `active` \| `under_review` \| `archived`. `under_review` 중에도 재사용은 금지된다 (D7).

---

## 10. 교훈 `/lessons` (담당자 전용)

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| GET | `/projects/{id}/lessons?status=` | 목록. `status`는 `candidate` 또는 `approved`. 30개 초과 시 응답에 `cleanup_suggestions[]` (오래되고 안 쓰인 순) |
| POST | `/lessons/{id}/approve` | 후보 → 승인 (이후 답변 생성에 주입) |
| DELETE | `/lessons/{id}` | 삭제 — 동일 내용 재생성 차단 |

교훈 객체: `{ id, content, status, needs_recheck, last_used_at, source_answer_id, created_at }`

---

## 11. 알림함 `/notifications`

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| GET | `/notifications?unread_only=true&limit=` | 내 알림 (전 프로젝트) |
| POST | `/notifications/read` | `{ids: [...]}` 읽음 처리 |
| GET | `/notifications/unread-count` | 뱃지용 `{count}` |

알림 객체: `{ id, type, title, body, payload: {project_id, question_id?, card_id?}, read_at, created_at }` — type 목록과 **수신자**는 `04-data-model.md` §4가 정본이다.

- `type` 어휘(04 §4와 1:1): `answer.completed` `answer.failed` `answer.verified` `answer.corrected` `answer.kept` `answer.rejected` `card.created` `briefing.ready` `doc.review_needed` `feedback.different` `sync.completed` / `sync.failed`
- **`title`·`body`는 서버가 수신자 `users.language` 기준으로 생성한다** (§1.5). 프론트는 그대로 렌더하며 자체 번역하지 않는다.
- 파이프라인 실패 시 `answer.failed` 타입이 **질문자에게** 발행된다 (D23). 질문자 화면에는 §6 `failure_info`가 함께 내려오고, 담당자에게는 `reason='failed'` 카드(§7)와 `card.created` 알림이 전달된다.

---

## 12. SSE `/sse/stream`

### 12.1 티켓 인증 (access token을 URL에 싣지 않는다)

| 메서드 | 경로 | 권한 | 설명 |
| --- | --- | --- | --- |
| POST | `/sse/ticket` | 로그인 | 1회용 스트림 티켓 발급 → `{ticket, expires_in}` |
| GET | `/sse/stream?ticket={ticket}` | 티켓 | 이벤트 스트림 |

```json
// POST /sse/ticket 200
{ "ticket": "st_9f2c...", "expires_in": 60 }
```
```
GET /api/v1/sse/stream?ticket=st_9f2c...
Accept: text/event-stream
```
- EventSource가 헤더를 못 실으므로 쿼리로 인증해야 하는데, **access token을 URL에 실으면 안 된다** —
  쿼리스트링은 프록시·리버스프록시·브라우저 히스토리·리퍼러·서버 액세스 로그에 평문으로 남고,
  access token은 수명이 길어 유출 시 전체 API 권한이 넘어간다.
  대신 **TTL 60초 · 1회용 · SSE 전용** 티켓을 쓴다. 유출되어도 만료·소진 후에는 무가치하다.
- 티켓은 `Authorization: Bearer` 헤더로 발급받는다. **소진(연결 성립) 즉시 폐기**되므로 재연결마다 새로 발급한다.
- 만료·재사용 티켓으로 접속하면 **401 `UNAUTHORIZED`**.

### 12.2 재연결 규약 (필수 구현)

> **SSE 연결은 반드시 끊긴다.** Railway는 SSE 스트림을 **15분에 강제 종료**하며, 하트비트가 없으면 **5분**에 끊는다.
> 재연결은 예외가 아니라 **정상 동작**이다. 프론트는 아래를 반드시 구현한다.

1. **끊기면 새 티켓을 받아 재연결한다.** `POST /sse/ticket` → `GET /sse/stream?ticket=`. 지수 백오프(1s → 2s → 4s, 상한 30s).
2. **재연결 시 구독 중인 리소스를 전량 재조회한다.** 끊긴 동안의 이벤트는 유실된다 —
   **SSE에는 재생(replay) 계약이 없다.** 화면에 떠 있는 질문 상세·카드 큐·알림함을 모두 GET으로 다시 읽는다.
3. **서버는 스트림 시작 시 미읽음 수를 1회 push한다** (`notification.unread_count`). 뱃지는 이 값으로 즉시 동기화된다.
4. **access token 만료 시 서버가 스트림을 종료한다.** 프론트는 `POST /auth/refresh`로 토큰을 갱신한 뒤
   **새 ticket을 발급받아** 재연결한다. 갱신 실패면 재연결을 중단하고 로그인 화면으로 보낸다.
5. 15초마다 `event: ping`이 온다. **30초 넘게 ping이 없으면 죽은 연결로 보고 능동적으로 재연결한다.**

- 이벤트 형식: `event: {type}` + `data: {json}`.
- 프론트 구현 계약: SSE는 **"갱신 신호"**로만 쓰고, 수신 시 해당 리소스를 GET으로 재조회한다 (payload는 id 위주).
  payload를 화면 상태로 직접 반영하지 말 것 — 재연결 유실 구간과 일관성이 깨진다.

### 12.3 이벤트 목록

| event | data | 수신자 |
| --- | --- | --- |
| `answer.completed` | `{question_id, grade, status}` | 질문자 |
| `answer.updated` | `{question_id, answer_id, state}` | 질문자 (확정/정정/반려/재검토) |
| `card.created` | `{card_id, project_id, is_urgent}` | 담당자 |
| `card.resolved` | `{card_id, resolution}` | 담당자 (다른 기기 동기화) |
| `briefing.ready` | `{project_id, date}` | 담당자 |
| `document.ingested` | `{document_id, version_id, status}` | 담당자 (업로드 인제스트 완료) |
| `notification.created` | `{notification_id, type}` | 본인 |
| `notification.unread_count` | `{count}` | 본인 (**스트림 시작 시 1회**) |
| `sync.completed` | `{integration_id, new_documents, new_versions}` | 담당자 |

- `document.ingested.status`: `ready` \| `failed`. 수신 시 `GET /projects/{id}/documents` 재조회로 목록을 갱신한다.
  이 이벤트가 오기 전까지 해당 버전은 검색 대상이 아니다 (§4).

---

## 13. 이력 `/events` · 지표 `/metrics`

### GET `/projects/{id}/events?entity_type=question&entity_id=q-9&limit=`
질문/답변 타임라인 (기능 5.3). `{ items: [ {type, actor: {id,name}|null, payload, created_at} ] }`
- `type` 값은 `04 §5`와 1:1이다: `question.created` `question.status_changed` `question.graded` `answer.published` `answer.reused` `answer.reuse_missed` `answer.expired` `feedback.created` `card.created` `card.viewed` `card.approved` `card.edited` `card.rejected` `card.deferred` `card.kept` `official_qa.created` `official_qa.suspended` `official_qa.archived` `lesson.candidate` `lesson.approved` `lesson.deleted` `document.version_activated` `answers.review_cascade` `member.joined` `answerer.transferred` `sync.run`

> 비율 지표의 `value: null`은 "표본 없음"을 뜻한다 — 프론트는 0%가 아니라 「측정 전」을 표시한다. (`grade_accuracy[]`는 `sufficient`/`message`로 별도 표현)

### GET `/projects/{id}/metrics?days=30`
```json
{
  "window_days": 30,
  "auto_answer_rate": { "value": 0.82, "target": 0.70, "green": 52, "yellow": 12, "red": 14 },
  "correction_rate": { "value": 0.07, "target": 0.10, "correct": 40, "different": 3 },
  "card_handle_30s_rate": { "value": 0.82, "target": 0.80, "within_30s": 23, "viewed_cards": 28 },
  "requestion_instant_rate": { "value": 1.0, "target": 0.95, "reused": 6, "reuse_missed": 0 },
  "grade_accuracy": [
    { "grade":"green", "verified_rate":0.94, "sample":52, "window_days":30, "sufficient":true, "message":null },
    { "grade":"yellow", "verified_rate":null, "sample":12, "window_days":30, "sufficient":false,
      "message":"검증 이력 부족 — 참고용" }
  ],
  "saved_wait_hours": { "value": 168, "assumption_hours": 24, "basis_count": 7 }
}
```

| 필드 | 계약 |
| --- | --- |
| `auto_answer_rate` | value = (green+yellow)/(green+yellow+red). 분모 0(질문 없음)이면 value: null |
| `card_handle_30s_rate` | `value = within_30s / viewed_cards`. **분모는 `first_viewed_at`이 기록된 카드 수**다. `viewed_cards=0`이면 `value: null` |
| `requestion_instant_rate` | `value = reused / (reused + reuse_missed)` (D26). 분모 0이면 `value: null` |
| `grade_accuracy[]` | **§6 `answer.accuracy_context`와 동일한 아이템 shape.** 표본 30건 미만이면 `sufficient:false` + `verified_rate:null` + `message` (D25) |
| `saved_wait_hours` | `value = basis_count × assumption_hours`. **가정치임을 화면에 반드시 표기한다** |

> ⚠️ **`saved_wait_hours`는 실측이 아니라 추정이다.** `assumption_hours`는 `settings.saved_wait_assumption_hours`(기본 24h)이고
> `basis_count`는 🟢+🟡로 즉답된 질문 수다. 프론트는 반드시 `"평균 대기 {assumption_hours}시간 가정, {basis_count}건 기준"`
> 같은 부연을 함께 노출한다. 숫자만 크게 띄우면 심사에서 근거를 되묻는다.

### GET `/projects/{id}/metrics/timeseries?days=30&bucket=day`
학습 곡선용 시계열 — "쓸수록 좋아진다"를 그래프 하나로 보여주는 데이터다.

```json
{
  "window_days": 30, "bucket": "day",
  "items": [
    { "date":"2026-07-08", "questions":4, "green":1, "yellow":1, "red":2,
      "reused":0, "lessons_approved":0, "official_qas":1 },
    { "date":"2026-08-06", "questions":12, "green":7, "yellow":3, "red":2,
      "reused":3, "lessons_approved":2, "official_qas":19 }
  ]
}
```
| 필드 | 의미 |
| --- | --- |
| `date` | 버킷 시작일 (`YYYY-MM-DD`, 담당자 타임존 기준) |
| `questions` | 해당 버킷에 접수된 질문 수 |
| `green` / `yellow` / `red` | 해당 버킷 등급 분포 (합계가 `questions`와 다를 수 있다 — 처리 중·실패 제외) |
| `reused` | 재사용으로 즉답된 건수 |
| `lessons_approved` | 해당 버킷에 승인된 교훈 수 |
| `official_qas` | **버킷 종료 시점 누적** 공식 Q&A 수 (증분 아님 — 지식이 쌓이는 곡선) |

- `bucket`: `day` \| `week`. 기본 `day`.
- 질문이 없는 날도 **빈 버킷을 0으로 채워서** 내려준다 (그래프에 구멍이 생기지 않게).

---

## 14. 헬스체크

`GET /health` → `{ "status":"ok", "db":"ok", "version":"0.1.0" }` (인증 불필요)
