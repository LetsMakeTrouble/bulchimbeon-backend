# 08. 데모 시나리오 · 시드 데이터

> `scripts/seed.py`가 이 문서의 내용을 그대로 DB에 주입한다. 시연·평가 질문셋 포함.

## 1. 가상 프로젝트 설정

- **프로젝트**: `GlobalMart JP Launch` — 한국 커머스팀이 미국 개발 파트너(DevCorp)의 API로 일본 리전 런칭을 준비하는 상황
- **담당자**: Mike Chen (`mike@devcorp.example` / 데모 비밀번호 `demo1234!`, language=en, timezone=America/New_York)
- **질문자**: 지수 (`jisoo@globalmart.example` / `demo1234!`, ko, Asia/Seoul), 민준 (`minjun@globalmart.example` / `demo1234!`, ko)
- 응답 지침(guidelines): `Answers must reference the exact API version. If a policy differs by region, always say which regions were checked. Prefer concise answers with field names in backticks.`

**설정값** — 기본값 그대로 두되 아래 세 가지는 시드에서 명시적으로 확인한다.

| 키 | 값 | 비고 |
| --- | --- | --- |
| `green_threshold` / `yellow_threshold` / `grounding_min` | 80 / 50 / 60 | 리스케일이 스케일 차이를 흡수하므로 모델이 바뀌어도 유지 |
| `retrieval_top_k` | **6** | 8에서 하향. 확장된 시드가 21청크이므로 top-k가 코퍼스보다 커지는 no-op이 발생하지 않는다 |
| `s_floor` / `s_ceil` | **0.25 / 0.65** | ⚠️ **캘리브레이션 전 잠정값.** M-1 게이트(`07`) 실측값으로 대체한 뒤 시연한다 |
| `similarity_floor` | 0.25 | top-1 `sim_raw`가 이 값 미만이면 강제 🔴 `no_evidence` |
| `briefing_hour` / `dnd_start` / `dnd_end` | 9 / 22:00 / 07:00 | ⏰ **담당자 Mike의 `users.timezone`(America/New_York) 기준**으로 판정된다. `settings`에 별도 타임존 키는 없다 |

> ### ⏰ 시연 시각과 DND — 이제 무관하다
> 발표가 한국 시각 오전에 잡히면 미국 동부는 심야, 즉 **Mike의 DND 시간대**다. 예전 규칙이라면 이때 Q7·Q8이 🟡로 강등되어 "충돌 감지 🔴"과 "근거 부족 🔴" 시연이 통째로 무너졌다.
> 확정 규칙(룰 6 · D2)에서 **DND 강등 대상은 `low_confidence`뿐**이고, **강제 🔴 4종(`conflict` / `no_evidence` / `schema_failed` / `quota_exceeded`)은 DND에서도 🔴을 유지**한다.
> 따라서 **Q7(conflict)과 Q8(no_evidence)은 발표가 몇 시에 열리든 🔴로 재현된다.** 시연 시각을 맞출 필요도, 시연용으로 DND를 끌 필요도 없다.
> 리허설 체크리스트(`prompts/09`)에서는 "발표 시각 기준 DND 판정 결과"를 한 번 눈으로 확인만 한다.

## 2. 시드 문서 (영어, `seed/` 폴더에 파일로 생성)

> 청킹은 마크다운 헤딩 경로 우선 분할이므로 `##` 섹션 하나가 청크 하나가 된다.
> **총 21청크** — `retrieval_top_k`(6)보다 충분히 크므로 검색이 "전부 반환"으로 퇴화하지 않는다.

### seed/api-spec.md — "Orders API Specification v2.1" (9청크)
```markdown
# Orders API Specification v2.1

## GET /v2/orders/{order_id}
Returns a single order. Response fields:
- `order_id` (string): unique order identifier
- `user_id` (string): the purchaser's account id. Included in all responses since v2.0.
- `status` (string): one of `pending`, `paid`, `shipped`, `delivered`, `cancelled`
- `currency` (string): ISO 4217. KRW, USD, and JPY are supported.
- `total_amount` (integer): amount in the smallest currency unit

## Authentication
All endpoints require a Bearer token issued by the Auth service.
Access tokens expire after 24 hours. Refresh tokens are not provided;
clients must re-authenticate after expiry.

## Rate Limiting
API calls are limited to 60 requests per minute per API key.
Exceeding the limit returns HTTP 429 with a `Retry-After` header.

## Pagination
List endpoints use cursor-based pagination with `cursor` and `limit` (max 100).

## Webhooks
Webhook endpoints are registered per project in the developer console. The platform
sends a `POST` request whose JSON body contains `event_id`, `event_type`, `created_at`,
and a `data` object. Supported event types are `order.created`, `order.paid`,
`order.shipped`, `order.cancelled`, and `refund.completed`. Every delivery carries an
`X-GlobalMart-Signature` header. Endpoints must return a `2xx` status within 5 seconds;
any other response, or a timeout, is recorded as a failed delivery. Delivery history for
the last seven days is available through `GET /v2/webhook-deliveries`. Registering more
than five endpoints per project is not supported in v2.1.

## Errors
All error responses use a single envelope: `{"error": {"code": "...", "message": "..."}}`.
The `code` is a stable machine-readable string; the `message` is English prose intended
for logs, not for end users. Common codes are `invalid_request` (400), `unauthorized`
(401), `forbidden` (403), `not_found` (404), `conflict` (409), `rate_limited` (429), and
`internal_error` (500). Every response also carries an `X-Request-Id` header; include it
when contacting support. Clients should retry only on `429` and `5xx`, and must never
retry a `4xx` other than `429`. Error codes are additive — new codes may appear without a
version bump, so treat an unknown code as `internal_error`.

## Idempotency
Write endpoints accept an optional `Idempotency-Key` header containing a client-generated
UUID. When a key is supplied, the platform stores the first response for that key and
replays it for any repeated request carrying the same key and the same request body,
returning the original status code and body. Keys are scoped to the API key that created
them and are retained for 24 hours; after that window a repeated request is treated as
new. If the same key arrives with a different request body, the call is rejected with
`409 conflict`. Idempotency keys are ignored on `GET` and `DELETE` requests.

## Sandbox
A sandbox environment is available at `https://sandbox.api.globalmart.example` and mirrors
the production surface of v2.1. Sandbox API keys are prefixed `sk_test_` and cannot be used
against production. No real money moves in the sandbox: only card numbers from the published
test-card list are accepted, and settlement is simulated immediately. Sandbox data is reset
every Sunday at 00:00 UTC and is never migrated to production. Rate limits in the sandbox
are the same as in production. Webhooks fire in the sandbox exactly as they do in
production, so signature verification can be tested end to end before launch.

## Regions
v2.1 is served from three regions: `us-east` (default), `eu-west`, and `ap-northeast`.
The Japanese launch uses `ap-northeast`, whose host is
`https://ap-northeast.api.globalmart.example`. An API key is bound to exactly one region at
creation time and cannot be moved; a multi-region project must issue one key per region.
Order data is stored in the region where the order was created and is not replicated across
regions, so a `GET /v2/orders/{order_id}` issued against the wrong region returns
`404 not_found`. Regional hosts run the same API version; there is no per-region feature
flagging in v2.1.
```

### seed/refund-policy.md — "Refund Policy v1" (5청크, 일본 조항 없음 — 시나리오 B용)
```markdown
# Refund Policy v1

## Standard Refund Window
Refunds are accepted within 30 days of purchase for all product categories. The window is
measured from the date the order reaches `delivered` status, or from the payment date for
orders that are never shipped. Refunds are processed to the original payment method within
5 business days of approval. A refund cannot be issued to a different card, account, or
person than the one used for the original purchase. Orders older than 30 days fall outside
this policy and must be escalated to the support team as a goodwill request.

## Shipping Fees
Shipping fees are non-refundable except for defective items. When an item is returned as
defective, the outbound shipping fee is refunded together with the item price, and a prepaid
return label is issued at no cost to the buyer. For a change-of-mind return the buyer pays
return shipping and the outbound fee is retained. Expedited shipping upgrades are never
refunded, even when the underlying item is refunded in full.

## Digital Goods
Digital goods are refundable only if not yet downloaded. Once a download has started the
purchase is final. For subscription products the current billing period is not refundable,
but the subscription can be cancelled to prevent the next charge; cancellation takes effect
at the end of the paid period. A license key that has been revealed counts as downloaded.
Bundles containing both physical and digital items are refunded per line item.

## Partial Refunds
A partial refund may be issued for a single line item within a multi-item order without
cancelling the whole order. The order stays in `delivered` status and the refunded amount is
recorded against that line item. Partial refunds follow the same 30-day window and the same
5-business-day processing time as full refunds. The total of all partial refunds against one
order can never exceed `total_amount`. Tax is refunded in proportion to the refunded amount.

## Disputes
If a buyer opens a chargeback with their card issuer, the order is frozen and no refund can
be issued through the platform until the dispute closes. Evidence must be submitted within
7 calendar days of the dispute notification; missing that deadline forfeits the dispute
automatically. A dispute resolved in the buyer's favour is settled by the issuer, and the
platform does not issue a second refund for the same amount. Dispute state is visible on the
order record as `dispute_status`.
```

### seed/integration-guide.md — "Integration Guide" (5청크, 신규)
```markdown
# Integration Guide

## Getting Started
Integration takes three steps: create a project in the developer console, issue a sandbox API
key, and call `GET /v2/orders` with the key in an `Authorization: Bearer` header. A successful
call returns an empty list rather than an error when the project has no orders yet. Client
libraries are published for Python, Node, and Go; each wraps authentication, pagination, and
error decoding. There is no SDK for mobile platforms — call the HTTP API directly. Partners
should finish the sandbox integration before requesting production credentials, because
production keys are issued only after a successful sandbox smoke test.

## Environments and API Keys
Each project has two independent key sets. Sandbox keys are prefixed `sk_test_` and work only
against the sandbox host; production keys are prefixed `sk_live_` and work only against a
regional production host. Keys are shown once at creation and cannot be retrieved again — store
them in a secret manager, never in source control. A project may hold up to ten active keys at
a time so that rotation can overlap. Revoking a key takes effect within one minute across all
regions. Never send a key from browser code; every call must originate from your server.

## Webhook Signature Verification
Every webhook delivery includes an `X-GlobalMart-Signature` header of the form
`t=<unix_seconds>,v1=<hex_digest>`. Compute the expected digest as an HMAC-SHA256 over the
string `"<t>.<raw_request_body>"` using your project's webhook secret, then compare it with
`v1` using a constant-time comparison. Reject the delivery if the digest does not match, or if
`t` is more than five minutes away from your server clock — that window is what stops replay
attacks. Always verify against the raw request body before any JSON parsing; re-serialising the
body changes the bytes and the signature will no longer match.

## Retrying Failed API Calls
This section covers calls your server makes to the platform. Treat `429` and `5xx` as retryable
and everything else as terminal. On a `429`, honour the `Retry-After` header rather than
guessing a delay. On a `5xx`, retry with exponential backoff and full jitter, capped at five
attempts, and always send the same `Idempotency-Key` so that a retried write cannot create a
duplicate order. Never retry a `400` or `422` — the request will fail identically every time.
Log the `X-Request-Id` from the failing response; support cannot trace a report without it.

## Going Live Checklist
Before switching to production, confirm all of the following: the sandbox smoke test passed for
order creation, retrieval, and cancellation; webhook signature verification is implemented and
tested against a deliberately tampered payload; idempotency keys are sent on every write;
`Retry-After` is honoured on `429`; production keys are stored in a secret manager and not in
the repository; and the correct regional host is configured for the target market. Partners must
also nominate an on-call contact address, because delivery failures on production webhooks are
reported by email within one hour.
```

### seed/meeting-notes-2026-07.md — "Partner Sync Notes, July 2026" (2청크, 충돌 케이스용)
```markdown
# Partner Sync Notes — July 2026

## API and Rate Limits
- Confirmed with Mike: the API rate limit is 100 requests per minute
  per API key. This supersedes the older figure in the API spec.
- Webhook retries: 3 attempts with exponential backoff (30s, 2m, 10m).

## JP Launch
- JP launch target: end of Q3. Payment gateway integration is on track.
- Open item: JP consumer-law review for the refund window (legal team, due Aug).
```

> ### 의도된 장치
> - **충돌 🔴 (Q7)**: api-spec의 `Rate Limiting`은 *"limited to 60 requests per minute per API key"*, meeting-notes는 *"the API rate limit is 100 requests per minute per API key"* — **시제·대상(per API key)·확정성이 전부 일치**하고 수치만 다르다. 그래서 어떤 모델도 이것을 충돌로 판정한다.
>   ⚠️ 이전 문안(*"will be raised to 100 … for enterprise keys, rollout date TBD"*)은 **미래·한정 대상·미확정**이라 60/min과 양립 가능했다. 실제 모순이 아니었으므로 모델이 `conflict=true`를 내지 않았다. 이 교정 없이는 Q7 시연이 성립하지 않는다.
> - **근거 부족 🔴 (Q8)**: refund-policy 어디에도 **일본 조항이 없다**. api-spec의 `Regions`는 `ap-northeast`가 존재한다는 인프라 사실만 말할 뿐 환불 정책을 다루지 않으므로, "주제를 언급만 하고 요청된 사실을 진술하지 않는 청크는 근거가 아니다"라는 ④ 프롬프트 규칙(`06 §2`)에 걸려 `not_answerable=true`가 된다. → 담당자 수정 → 지식 편입 데모.
> - **미끼 (Q9)**: meeting-notes의 *"Payment gateway integration is on track."* 은 "결제 게이트웨이"라는 어휘만 겹치고 **PG사 이름이 없다**. 일반 상식 폴백 금지를 검증하는 좋은 미끼이므로 **그대로 유지**한다.
> - **웹훅 재시도(Q5)의 단일 출처성 유지**: 재시도 횟수(3회, 30s/2m/10m)는 **meeting-notes에만** 있다. api-spec의 `Webhooks`는 등록·페이로드·서명·타임아웃만 다루고, integration-guide의 `Retrying Failed API Calls`는 **클라이언트가 API를 호출할 때**의 재시도라 주제가 다르다. 단일 출처 케이스가 보존된다.

## 3. 검증 질문셋 (기대 등급 포함 — 파이프라인 회귀 테스트 겸용)

| # | 질문 (ko) | 기대 결과 | 근거 |
| --- | --- | --- | --- |
| Q1 | 주문 조회 API 응답에 user_id 포함되나요? | 🟢 | api-spec `GET /v2/orders/{order_id}` |
| Q2 | 액세스 토큰 만료 시간이 어떻게 되나요? | 🟢 | api-spec `Authentication` — 24시간 + 재인증 |
| Q3 | 지원하는 통화가 뭐예요? | 🟢 | api-spec — KRW/USD/JPY |
| Q4 | 목록 조회 페이지네이션 방식 알려주세요 | 🟢 | api-spec `Pagination` — cursor 기반, max 100 |
| Q5 | 웹훅 재시도 정책이 있나요? | 🟢~🟡 | meeting-notes `API and Rate Limits`만 근거 (단일 출처) |
| Q6 | 배송비도 환불되나요? | 🟢 | refund-policy `Shipping Fees` — 불량품 제외 환불 불가 |
| Q7 | API 요청 제한이 분당 몇 회인가요? | **🔴 (conflict)** | 60/min(api-spec) vs 100/min(meeting-notes) — 동일 대상·동일 확정성의 진짜 모순 |
| Q8 | 환불 정책이 일본 리전에도 동일하게 적용되나요? | **🔴 (no_evidence)** | 일본 조항 부재 + legal review 진행 중 |
| Q9 | 결제 게이트웨이는 어떤 PG사를 쓰나요? | **🔴 (`no_evidence` 또는 `low_confidence`)** | 문서에 PG사 이름 없음. 미끼 청크가 검색은 되므로 `no_evidence`가 아니라 매칭률 미달로 떨어질 수 있다 — **둘 다 정답** |
| Q10 | (Q8 담당자 수정·확정 후) **일본 리전 환불 정책도 동일하게 적용되나요?** | 🟢 reused | 공식 Q&A 재사용. Q8과 **어휘를 근접**시켜 원시 코사인이 `reuse_threshold`를 넘게 한다 |
| Q11 | 웹훅 서명은 어떻게 검증하나요? | 🟢 | integration-guide `Webhook Signature Verification` — HMAC-SHA256, 5분 윈도우 |
| Q12 | 멱등키는 얼마나 유지되나요? | 🟢 | api-spec `Idempotency` — 24시간, 같은 키·다른 body는 409 |
| Q13 | 샌드박스에서 실제 결제가 발생하나요? | 🟢 | api-spec `Sandbox` — 실제 결제 없음, 테스트 카드만, 매주 일요일 리셋 |

- Q11~Q13은 **확장된 시드 섹션을 근거로 하는 🟢 케이스**다. 검색이 6청크만 뽑는 상황에서 정답 청크가 실제로 top-k에 들어오는지 검증한다.
- ⚠️ **Q10의 주 방어선은 문안이 아니라 LLM 동일성 게이트다.** 어휘 근접은 원시 코사인을 `reuse_threshold` 위로 올리는 **보조 수단**일 뿐이고, "정말 같은 질문인가"의 최종 판정은 ②의 2차 게이트(yes/no)가 한다(`06 §2`). 게이트가 `no`를 내면 재사용 대신 새 답변이 생성되고 `answer.reuse_missed` 이벤트가 남는다.
- **Q7·Q8의 기대값은 시연 시각과 무관**하다(§1의 DND 규칙 — 강제 🔴 4종은 DND에서도 🔴 유지). ⚠️ **Q9는 예외다**: `no_evidence`로 잡히면 시각과 무관하지만, `low_confidence`로 떨어지고 발행 가능한 문장이 남으면 담당자 DND 시간대에 🟡로 강등된다(`02` 룰 6 · `06 §2` ⑥). 따라서 Q9만 시각 의존적일 수 있다.

## 4. 라이브 데모 스크립트 (5분)

1. **[지수 화면]** Q1 입력 → **20초 내** 🟢 87% + 인용(원문 스니펫) 표시 — *"영어 명세를 한국어로, 근거와 함께 즉답"*
   > 📌 **"87%"에 대하여**: `S`가 `s_floor`~`s_ceil` 리스케일 값이므로 임베딩 모델이 바뀌어도 80대 후반은 **재현 가능하다**. 다만 **정확한 숫자는 M-1 캘리브레이션 후 확정**된다. 리허설에서 실제 값을 확인하고 대사를 맞춘다.
   > ⏱ "20초"도 `LLM_REASONING_EFFORT=minimal` 기준 실측 후 확정한다. 예전 대사 "수 초 내"는 추론 모델 도입 전 수치라 지킬 수 없다.
2. Q8 입력 → 🔴 보류: "근거 부족 — 담당자에게 전달했습니다" — *"모르면 지어내지 않고 정직하게 넘긴다"*
3. **[마이크 화면(모바일 뷰)]** 확인 카드: 영어로 구조화된 배경→질문→선택지 + AI 초안 → `edit`으로 "Japan is 20 days (local law)" 입력, 30초 컷 — *"노트북 없이 침대에서 검토"*
4. **[지수 화면]** 정정 알림(한국어) 수신 → 답변이 확정(✔)으로 갱신
5. **[민준 화면]** Q10 입력 → 즉시 🟢 "공식 확정 답변" 재사용 — *"같은 질문에 두 번 다시 24시간이 걸리지 않는다"*
   > 📌 재사용은 임계값 하나로 결정되지 않는다. 원시 코사인이 `reuse_threshold` 이상이면 **후보**가 되고, LLM에 *"이 두 질문은 같은 질문인가?"* 를 한 번 물어 **`yes`일 때만** 확정 원문을 내보낸다(+약 2초). 심사 질문 *"유사도 하나로 잘못 재사용하면?"* 에 대한 답이 이 게이트다.
6. **[지표 화면]** `GET /metrics`: 자동응답률·절약된 대기시간 — 마무리 멘트
   > 📌 `saved_wait_hours`는 `settings.saved_wait_assumption_hours`(24h) 기반 **가정치**로 표기된다. 등급별 실측 정확도는 표본 30건 이상일 때만 숫자가 뜬다(D25) — `--with-history`가 **🟢 등급의** 표본을 만든다(§5). 🟡·🔴은 "표본 부족" 표기가 정상이다.

플랜B: SSE 미동작 시 새로고침(폴링)으로 동일 시연 가능. 클라우드 URL 불안정 시 로컬 docker-compose 백업.

## 5. seed.py 요구사항

1. 유저 3명 → 프로젝트 생성(마이크=담당자) → 지수·민준 asker 참여
2. guidelines 등록 → `seed/*.md` **4개 문서**(api-spec / refund-policy / integration-guide / meeting-notes) 업로드 → 인제스트 완료 대기(ready). **총 21청크 생성 확인**을 어서션으로 둔다 — 청크 수가 `retrieval_top_k`보다 적으면 검색이 no-op가 되므로 조용히 깨지는 실패 모드다.
3. `--with-history` 옵션: **질문 45~60건**(**🟢 기대 질문만 35건 이상**)을 실제 파이프라인으로 실행해 이력·지표를 채운 상태로 시작 (발표 직전용)
   - 구성: Q1~Q6 + Q11~Q13(총 9종)과 그 **패러프레이즈 변형**을 섞어 45~60건. 🟢 위주로 하되 Q7·Q8·Q9 계열도 몇 건 섞어 등급 분포를 만든다. 🟢이 단독으로 35건 이상이 되도록 배분하고, Q7·Q8·Q9 계열은 등급 분포 확인용 8~10건에 그친다.
   - 실행 후 일부 답변에 담당자 승인(`verified`)과 질문자 "맞았다"(`correct`) 피드백을 주입한다 — **`grade_accuracy`의 분자**가 여기서 나온다.
   - ⚠️ **D25의 30건은 총건수가 아니라 등급별 표본이다**(`02` 룰 10, `05 §13` 예시에서 green sample=52 `sufficient:true` / yellow sample=12 `sufficient:false`). 🟢 하나라도 숫자를 띄우려면 🟢만 35건 이상이어야 한다. 🟡·🔴은 데모 규모에서 반드시 "표본 부족"으로 뜨며 **이것이 D25가 의도한 정상 동작**이다(`02` 룰 10, `06 §7` 4겹) — 6단계 대사에 "🟢은 실측 94%, 🟡·🔴은 표본이 모자라 일부러 숫자를 감춘다"를 포함한다.
   - 비용 가늠: 45~60건 × 3~4회 = **135~240 LLM 호출**. `daily_llm_call_limit`(500) 안이지만 여유가 절반 남짓이므로 발표 당일 재실행은 피하고 전날 채워 둔다.
   - ⚠️ 라이브 스크립트(§4)에서 실제로 던지는 **Q1·Q8·Q10**과 그 패러프레이즈 변형은 **승인(`verified`) 주입 대상에서 제외**한다. 승인은 공식 Q&A를 만들고(`06 §3`), 그러면 데모 당일 같은 질문이 ②에서 재사용 경로로 빠져 `matching_rate`·`search_score`·인용이 전부 `null`이 된다(`05 §6` D11, `06 §2` ②). §4 1단계의 "🟢 87% + 인용"이 화면에서 사라진다.
4. 멱등성: 재실행 시 기존 데모 데이터 삭제 후 재주입 (`--reset`). 프로젝트 삭제 API는 없으므로(D19) **DB 레벨에서 수행**한다.
