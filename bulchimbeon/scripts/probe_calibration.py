"""M-1 캘리브레이션 게이트 — 불침번 임계값 실측 프로브 (M0 착수 전 1회 실행).

백엔드 코드 없이 OpenAI API 키 하나로 돈다. 표준 라이브러리 + `openai` 패키지만 쓴다.
소요 약 3분, 비용 < $0.01.

이 스크립트가 확정하는 값
--------------------------
`s_floor` · `s_ceil` · `similarity_floor` · `reuse_threshold` · `similar_threshold`
(= `04 §3` 임계값 5종. 단 **⚠️ 잠정값 표기는 4종** — `similarity_floor`는 `s_floor` 파생이라
별도 표기가 없다) 그리고 `reasoning_effort` 지원 여부·지연.

실행
----
    # 레포에 pyproject.toml이 이미 있을 때 (M0 이후)
    uv run python scripts/probe_calibration.py

    # M0 착수 전 — 아직 pyproject.toml이 없을 때
    uv run --with openai python scripts/probe_calibration.py

    # 키 설정 (PowerShell). `&&`는 PowerShell 5.1에서 파서 에러다.
    $env:OPENAI_API_KEY = "sk-..."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
사전 등록 판정 기준표 (결과를 보기 전에 읽는다 — 사후 합리화 방지)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

| 관측                                  | 결론                        | 액션                                                    |
| ------------------------------------- | --------------------------- | ------------------------------------------------------- |
| 🟢 기대 질문 원시 S≥80이 0개    | 리스케일 도입 확정 (Blocker)| 제안 `s_floor`/`s_ceil`을 `DEFAULT_SETTINGS`에 반영      |
| 🟢 기대 질문 원시 S 대부분 <50  | 최악 — 데모 전부 🔴         | 리스케일 + `retrieval_top_k` 축소 + 시드 확장 전부 필수  |
| 🟢 기대 질문 원시 S≥80이 과반수 | 리스케일 불필요 (기각)      | `s_floor=0.0` / `s_ceil=1.0`으로 두고 80/50 유지         |
| Q8↔Q10 < 0.92                         | 0.92 재사용 실패 (Blocker)  | `reuse_threshold` 재설정 + LLM 동일성 2차 게이트 필수    |
| Q8↔Q10 ≥ 제안 reuse_threshold 이지만  |                             |                                                          |
|   무관 질문쌍 최대값이 그보다 높음    | 오재사용 위험               | `reuse_threshold`를 무관 최대값 위로 올린다              |
| `reasoning_effort` 400                | 파라미터 미지원 확정        | 20초 예산 포기 또는 비추론 모델로 교체                   |
| minimal 지연 × 3 > 25초               | 파이프라인 데드라인 붕괴    | `06 §0` 성능 목표 재기술 + ①/⑧ 병렬화 또는 ④⑤ 병합      |
| `temperature=0`이 400이 **아님**      | 금지 파라미터 가정 오류     | `03 §4` 금지 목록 재확인 (그래도 전달하지 않는다)        |
| Q9 top-1 ≥ 🟢 기대 대역 최소값     | 유사도로 🔴 분리 불가       | `similarity_floor`에 기대지 말고 ④ `not_answerable` 강화 |
| Q9 top-1 < 🟢 기대 대역 최소값     | 분리 가능                   | 제안 `similarity_floor`를 채택                           |

판정 대상 6항목 요약: 판정 1 = S 스케일 / 판정 2 = 재사용 임계값 / 판정 3 = LLM 호출 규약·지연 /
판정 4 = 강제 🔴 분리 가능성. 마지막에 `projects.settings`에 그대로 붙여넣을 JSON 블록을 출력한다.

주의
----
- `SEED_CHUNKS`는 `docs/08-demo-scenario.md §2`를 **헤딩 단위로 손분할한 스냅샷**이다.
  §2가 바뀌면 이 상수도 함께 갱신해야 측정이 유효하다.
- `QUESTIONS`의 영어 번역문은 파이프라인 ①단계가 만들 문장의 대역이다.
  실제 ① 출력이 크게 다르면 측정값도 달라진다 — 실측은 어디까지나 발판이고 최종 확인은 M9 리허설이다.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import sys
import time
import unicodedata

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
LLM_MODEL = "gpt-5-mini"

# 원본 0.92 / 0.85의 간격을 보존해 similar_threshold를 제안한다.
REUSE_SIMILAR_GAP = 0.07
# 관측 최소값에서 이만큼 더 내려 floor를 잡는다 (측정 표본이 10건뿐이라 여유를 둔다).
FLOOR_MARGIN = 0.02
# 관측된 Q8↔Q10에서 이만큼 내려 reuse_threshold를 제안한다 (결정서 1.1).
REUSE_MARGIN = 0.03


# ─────────────────────────────────────────────────────────────────────────────
# 시드 코퍼스 — docs/08-demo-scenario.md §2 를 헤딩 단위로 손분할 (21 청크)
#   api-spec 9 · refund-policy 5 · integration-guide 5 · meeting-notes 2
# ─────────────────────────────────────────────────────────────────────────────
SEED_CHUNKS: list[dict] = [
    # ── seed/api-spec.md — Orders API Specification v2.1 (9청크) ──────────
    {
        "doc": "api-spec.md",
        "heading": "Orders API Specification v2.1 > GET /v2/orders/{order_id}",
        "text": (
            "Returns a single order. Response fields:\n"
            "- `order_id` (string): unique order identifier\n"
            "- `user_id` (string): the purchaser's account id. Included in all responses since v2.0.\n"
            "- `status` (string): one of `pending`, `paid`, `shipped`, `delivered`, `cancelled`\n"
            "- `currency` (string): ISO 4217. KRW, USD, and JPY are supported.\n"
            "- `total_amount` (integer): amount in the smallest currency unit"
        ),
    },
    {
        "doc": "api-spec.md",
        "heading": "Orders API Specification v2.1 > Authentication",
        "text": (
            "All endpoints require a Bearer token issued by the Auth service.\n"
            "Access tokens expire after 24 hours. Refresh tokens are not provided;\n"
            "clients must re-authenticate after expiry."
        ),
    },
    {
        "doc": "api-spec.md",
        "heading": "Orders API Specification v2.1 > Rate Limiting",
        "text": (
            "API calls are limited to 60 requests per minute per API key.\n"
            "Exceeding the limit returns HTTP 429 with a `Retry-After` header."
        ),
    },
    {
        "doc": "api-spec.md",
        "heading": "Orders API Specification v2.1 > Pagination",
        "text": (
            "List endpoints use cursor-based pagination with `cursor` and `limit` (max 100)."
        ),
    },
    {
        "doc": "api-spec.md",
        "heading": "Orders API Specification v2.1 > Webhooks",
        "text": (
            "Webhook endpoints are registered per project in the developer console. The platform\n"
            "sends a `POST` request whose JSON body contains `event_id`, `event_type`, `created_at`,\n"
            "and a `data` object. Supported event types are `order.created`, `order.paid`,\n"
            "`order.shipped`, `order.cancelled`, and `refund.completed`. Every delivery carries an\n"
            "`X-GlobalMart-Signature` header. Endpoints must return a `2xx` status within 5 seconds;\n"
            "any other response, or a timeout, is recorded as a failed delivery. Delivery history for\n"
            "the last seven days is available through `GET /v2/webhook-deliveries`. Registering more\n"
            "than five endpoints per project is not supported in v2.1."
        ),
    },
    {
        "doc": "api-spec.md",
        "heading": "Orders API Specification v2.1 > Errors",
        "text": (
            'All error responses use a single envelope: `{"error": {"code": "...", "message": "..."}}`.\n'
            "The `code` is a stable machine-readable string; the `message` is English prose intended\n"
            "for logs, not for end users. Common codes are `invalid_request` (400), `unauthorized`\n"
            "(401), `forbidden` (403), `not_found` (404), `conflict` (409), `rate_limited` (429), and\n"
            "`internal_error` (500). Every response also carries an `X-Request-Id` header; include it\n"
            "when contacting support. Clients should retry only on `429` and `5xx`, and must never\n"
            "retry a `4xx` other than `429`. Error codes are additive — new codes may appear without a\n"
            "version bump, so treat an unknown code as `internal_error`."
        ),
    },
    {
        "doc": "api-spec.md",
        "heading": "Orders API Specification v2.1 > Idempotency",
        "text": (
            "Write endpoints accept an optional `Idempotency-Key` header containing a client-generated\n"
            "UUID. When a key is supplied, the platform stores the first response for that key and\n"
            "replays it for any repeated request carrying the same key and the same request body,\n"
            "returning the original status code and body. Keys are scoped to the API key that created\n"
            "them and are retained for 24 hours; after that window a repeated request is treated as\n"
            "new. If the same key arrives with a different request body, the call is rejected with\n"
            "`409 conflict`. Idempotency keys are ignored on `GET` and `DELETE` requests."
        ),
    },
    {
        "doc": "api-spec.md",
        "heading": "Orders API Specification v2.1 > Sandbox",
        "text": (
            "A sandbox environment is available at `https://sandbox.api.globalmart.example` and mirrors\n"
            "the production surface of v2.1. Sandbox API keys are prefixed `sk_test_` and cannot be used\n"
            "against production. No real money moves in the sandbox: only card numbers from the published\n"
            "test-card list are accepted, and settlement is simulated immediately. Sandbox data is reset\n"
            "every Sunday at 00:00 UTC and is never migrated to production. Rate limits in the sandbox\n"
            "are the same as in production. Webhooks fire in the sandbox exactly as they do in\n"
            "production, so signature verification can be tested end to end before launch."
        ),
    },
    {
        "doc": "api-spec.md",
        "heading": "Orders API Specification v2.1 > Regions",
        "text": (
            "v2.1 is served from three regions: `us-east` (default), `eu-west`, and `ap-northeast`.\n"
            "The Japanese launch uses `ap-northeast`, whose host is\n"
            "`https://ap-northeast.api.globalmart.example`. An API key is bound to exactly one region at\n"
            "creation time and cannot be moved; a multi-region project must issue one key per region.\n"
            "Order data is stored in the region where the order was created and is not replicated across\n"
            "regions, so a `GET /v2/orders/{order_id}` issued against the wrong region returns\n"
            "`404 not_found`. Regional hosts run the same API version; there is no per-region feature\n"
            "flagging in v2.1."
        ),
    },
    # ── seed/refund-policy.md — Refund Policy v1 (5청크, 일본 조항 없음) ───
    {
        "doc": "refund-policy.md",
        "heading": "Refund Policy v1 > Standard Refund Window",
        "text": (
            "Refunds are accepted within 30 days of purchase for all product categories. The window is\n"
            "measured from the date the order reaches `delivered` status, or from the payment date for\n"
            "orders that are never shipped. Refunds are processed to the original payment method within\n"
            "5 business days of approval. A refund cannot be issued to a different card, account, or\n"
            "person than the one used for the original purchase. Orders older than 30 days fall outside\n"
            "this policy and must be escalated to the support team as a goodwill request."
        ),
    },
    {
        "doc": "refund-policy.md",
        "heading": "Refund Policy v1 > Shipping Fees",
        "text": (
            "Shipping fees are non-refundable except for defective items. When an item is returned as\n"
            "defective, the outbound shipping fee is refunded together with the item price, and a prepaid\n"
            "return label is issued at no cost to the buyer. For a change-of-mind return the buyer pays\n"
            "return shipping and the outbound fee is retained. Expedited shipping upgrades are never\n"
            "refunded, even when the underlying item is refunded in full."
        ),
    },
    {
        "doc": "refund-policy.md",
        "heading": "Refund Policy v1 > Digital Goods",
        "text": (
            "Digital goods are refundable only if not yet downloaded. Once a download has started the\n"
            "purchase is final. For subscription products the current billing period is not refundable,\n"
            "but the subscription can be cancelled to prevent the next charge; cancellation takes effect\n"
            "at the end of the paid period. A license key that has been revealed counts as downloaded.\n"
            "Bundles containing both physical and digital items are refunded per line item."
        ),
    },
    {
        "doc": "refund-policy.md",
        "heading": "Refund Policy v1 > Partial Refunds",
        "text": (
            "A partial refund may be issued for a single line item within a multi-item order without\n"
            "cancelling the whole order. The order stays in `delivered` status and the refunded amount is\n"
            "recorded against that line item. Partial refunds follow the same 30-day window and the same\n"
            "5-business-day processing time as full refunds. The total of all partial refunds against one\n"
            "order can never exceed `total_amount`. Tax is refunded in proportion to the refunded amount."
        ),
    },
    {
        "doc": "refund-policy.md",
        "heading": "Refund Policy v1 > Disputes",
        "text": (
            "If a buyer opens a chargeback with their card issuer, the order is frozen and no refund can\n"
            "be issued through the platform until the dispute closes. Evidence must be submitted within\n"
            "7 calendar days of the dispute notification; missing that deadline forfeits the dispute\n"
            "automatically. A dispute resolved in the buyer's favour is settled by the issuer, and the\n"
            "platform does not issue a second refund for the same amount. Dispute state is visible on the\n"
            "order record as `dispute_status`."
        ),
    },
    # ── seed/integration-guide.md — Integration Guide (5청크) ──────────────
    {
        "doc": "integration-guide.md",
        "heading": "Integration Guide > Getting Started",
        "text": (
            "Integration takes three steps: create a project in the developer console, issue a sandbox API\n"
            "key, and call `GET /v2/orders` with the key in an `Authorization: Bearer` header. A successful\n"
            "call returns an empty list rather than an error when the project has no orders yet. Client\n"
            "libraries are published for Python, Node, and Go; each wraps authentication, pagination, and\n"
            "error decoding. There is no SDK for mobile platforms — call the HTTP API directly. Partners\n"
            "should finish the sandbox integration before requesting production credentials, because\n"
            "production keys are issued only after a successful sandbox smoke test."
        ),
    },
    {
        "doc": "integration-guide.md",
        "heading": "Integration Guide > Environments and API Keys",
        "text": (
            "Each project has two independent key sets. Sandbox keys are prefixed `sk_test_` and work only\n"
            "against the sandbox host; production keys are prefixed `sk_live_` and work only against a\n"
            "regional production host. Keys are shown once at creation and cannot be retrieved again — store\n"
            "them in a secret manager, never in source control. A project may hold up to ten active keys at\n"
            "a time so that rotation can overlap. Revoking a key takes effect within one minute across all\n"
            "regions. Never send a key from browser code; every call must originate from your server."
        ),
    },
    {
        "doc": "integration-guide.md",
        "heading": "Integration Guide > Webhook Signature Verification",
        "text": (
            "Every webhook delivery includes an `X-GlobalMart-Signature` header of the form\n"
            "`t=<unix_seconds>,v1=<hex_digest>`. Compute the expected digest as an HMAC-SHA256 over the\n"
            'string `"<t>.<raw_request_body>"` using your project\'s webhook secret, then compare it with\n'
            "`v1` using a constant-time comparison. Reject the delivery if the digest does not match, or if\n"
            "`t` is more than five minutes away from your server clock — that window is what stops replay\n"
            "attacks. Always verify against the raw request body before any JSON parsing; re-serialising the\n"
            "body changes the bytes and the signature will no longer match."
        ),
    },
    {
        "doc": "integration-guide.md",
        "heading": "Integration Guide > Retrying Failed API Calls",
        "text": (
            "This section covers calls your server makes to the platform. Treat `429` and `5xx` as retryable\n"
            "and everything else as terminal. On a `429`, honour the `Retry-After` header rather than\n"
            "guessing a delay. On a `5xx`, retry with exponential backoff and full jitter, capped at five\n"
            "attempts, and always send the same `Idempotency-Key` so that a retried write cannot create a\n"
            "duplicate order. Never retry a `400` or `422` — the request will fail identically every time.\n"
            "Log the `X-Request-Id` from the failing response; support cannot trace a report without it."
        ),
    },
    {
        "doc": "integration-guide.md",
        "heading": "Integration Guide > Going Live Checklist",
        "text": (
            "Before switching to production, confirm all of the following: the sandbox smoke test passed for\n"
            "order creation, retrieval, and cancellation; webhook signature verification is implemented and\n"
            "tested against a deliberately tampered payload; idempotency keys are sent on every write;\n"
            "`Retry-After` is honoured on `429`; production keys are stored in a secret manager and not in\n"
            "the repository; and the correct regional host is configured for the target market. Partners must\n"
            "also nominate an on-call contact address, because delivery failures on production webhooks are\n"
            "reported by email within one hour."
        ),
    },
    # ── seed/meeting-notes-2026-07.md — Partner Sync Notes (2청크, 충돌) ───
    {
        "doc": "meeting-notes-2026-07.md",
        "heading": "Partner Sync Notes - July 2026 > API and Rate Limits",
        "text": (
            "- Confirmed with Mike: the API rate limit is 100 requests per minute\n"
            "  per API key. This supersedes the older figure in the API spec.\n"
            "- Webhook retries: 3 attempts with exponential backoff (30s, 2m, 10m)."
        ),
    },
    {
        "doc": "meeting-notes-2026-07.md",
        "heading": "Partner Sync Notes - July 2026 > JP Launch",
        "text": (
            "- JP launch target: end of Q3. Payment gateway integration is on track.\n"
            "- Open item: JP consumer-law review for the refund window (legal team, due Aug)."
        ),
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# 검증 질문셋 — docs/08-demo-scenario.md §3 (영어 번역문은 파이프라인 ① 대역)
#   band: green = 🟢 기대 대역 / red = 강제 🔴 기대 / reuse = 재사용 경로(검색 무관)
# ─────────────────────────────────────────────────────────────────────────────
QUESTIONS: list[dict] = [
    {"id": "Q1", "ko": "주문 조회 API 응답에 user_id 포함되나요?",
     "en": "Does the order lookup API response include the user_id field?",
     "expect": "green", "band": "green"},
    {"id": "Q2", "ko": "액세스 토큰 만료 시간이 어떻게 되나요?",
     "en": "How long is an access token valid before it expires?",
     "expect": "green", "band": "green"},
    {"id": "Q3", "ko": "지원하는 통화가 뭐예요?",
     "en": "Which currencies are supported?",
     "expect": "green", "band": "green"},
    {"id": "Q4", "ko": "목록 조회 페이지네이션 방식 알려주세요",
     "en": "What pagination method do the list endpoints use?",
     "expect": "green", "band": "green"},
    {"id": "Q5", "ko": "웹훅 재시도 정책이 있나요?",
     "en": "Is there a retry policy for webhook deliveries?",
     "expect": "green~yellow", "band": "green"},
    {"id": "Q6", "ko": "배송비도 환불되나요?",
     "en": "Are shipping fees refundable?",
     "expect": "green", "band": "green"},
    {"id": "Q7", "ko": "API 요청 제한이 분당 몇 회인가요?",
     "en": "How many API requests per minute are allowed?",
     "expect": "red(conflict)", "band": "red"},
    {"id": "Q8", "ko": "환불 정책이 일본 리전에도 동일하게 적용되나요?",
     "en": "Does the refund policy apply identically to the Japan region?",
     "expect": "red(no_evidence)", "band": "red"},
    {"id": "Q9", "ko": "결제 게이트웨이는 어떤 PG사를 쓰나요?",
     "en": "Which payment gateway provider is used?",
     "expect": "red(no_ev|low_conf)", "band": "red"},
    {"id": "Q10", "ko": "일본 리전 환불 정책도 동일하게 적용되나요?",
     "en": "Does the Japan region refund policy apply identically as well?",
     "expect": "green(reused)", "band": "reuse"},
    {"id": "Q11", "ko": "웹훅 서명은 어떻게 검증하나요?",
     "en": "How do I verify the webhook signature?",
     "expect": "green", "band": "green"},
    {"id": "Q12", "ko": "멱등키는 얼마나 유지되나요?",
     "en": "How long is an idempotency key retained?",
     "expect": "green", "band": "green"},
    {"id": "Q13", "ko": "샌드박스에서 실제 결제가 발생하나요?",
     "en": "Does a real payment occur in the sandbox environment?",
     "expect": "green", "band": "green"},
]

# ─────────────────────────────────────────────────────────────────────────────
# ④ 생성 스키마 — strict 요건 그대로 (전 필드 required · additionalProperties=false
#    · 기본값 필드 없음 · text_ko 포함으로 ⑧ 호출 제거). 결정서 1.8·1.9.
# ─────────────────────────────────────────────────────────────────────────────
SENTENCES_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sentences", "not_answerable", "conflict", "conflict_chunk_ids"],
    "properties": {
        "sentences": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text_en", "text_ko", "chunk_ids"],
                "properties": {
                    "text_en": {"type": "string"},
                    "text_ko": {"type": "string"},
                    "chunk_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "not_answerable": {"type": "boolean"},
        "conflict": {"type": "boolean"},
        "conflict_chunk_ids": {"type": "array", "items": {"type": "string"}},
    },
}

# `docs/06-ai-pipeline.md:147-155` 시스템 프롬프트 골격 전문 — 한 줄도 줄이지 않는다.
GEN_SYSTEM = (
    "You answer questions strictly from the provided evidence chunks.\n"
    "Rules:\n"
    "- Every sentence MUST cite at least one chunk id that supports it.\n"
    "- If the evidence does not contain the answer, set not_answerable=true. "
    "Never use general knowledge.\n"
    "- A chunk that merely mentions the topic without stating the requested fact does NOT count as\n"
    "  evidence. In that case set not_answerable=true.\n"
    "- If two chunks contradict each other on the asked point, set conflict=true and list both chunk ids.\n"
    "- For each sentence, provide both text_en and its Korean translation text_ko.\n"
    "- Follow the project guidelines below. Apply the approved lessons below."
)

# `[GUIDELINES]` — `docs/08-demo-scenario.md §1` 응답 지침 문안 그대로.
GEN_GUIDELINES = (
    "Answers must reference the exact API version. If a policy differs by region, always say "
    "which regions were checked. Prefer concise answers with field names in backticks."
)

# `[APPROVED LESSONS]` — 실 ④ 는 승인 교훈을 `max_lessons=30` 개까지 싣는다.
# 지연 측정이 실 규모를 반영하도록 같은 개수의 더미 교훈을 채운다 (`04 §3` `max_lessons`).
_LESSON_TEMPLATES = (
    "환불 기한을 답할 때는 기준 시점(결제일 / 배송완료일)을 함께 적는다.",
    "리전별로 규정이 다를 수 있으면 확인한 리전을 문장에 명시한다.",
    "레이트 리밋 수치는 출처 문서와 버전을 함께 인용한다 — 회의록과 스펙이 다르다.",
    "필드명은 백틱으로 감싸고 타입을 함께 적는다.",
    "재시도 정책은 대상 상태코드(429 / 5xx)를 명시한다.",
    "샌드박스와 프로덕션의 차이가 있으면 어느 환경 기준인지 밝힌다.",
)
GEN_LESSONS = [
    f"- (lesson {i + 1}) {_LESSON_TEMPLATES[i % len(_LESSON_TEMPLATES)]}"
    for i in range(30)
]


def _gen_user_prompt() -> str:
    """④ 생성 단계의 프롬프트 구성을 그대로 재현한다.

    `06 §2` ④ 는 `[GUIDELINES]` + 승인 교훈 `max_lessons=30` + `retrieval_top_k=6` 청크 +
    `[QUESTION]` 을 한 번에 싣는다. 판정 3 의 지연이 실 ④ 를 대표하려면 이 규모를 맞춰야 한다
    (청크 3개 409자짜리 축소판으로는 25초 데드라인 판정이 성립하지 않는다).
    """
    # rate limit 충돌 쌍(2 api-spec ↔ 19 meeting-notes)과 무관 청크(3)를 반드시 유지한다 —
    # `conflict` / `conflict_chunk_ids` 를 실제로 채우는 장치다. 4·5·6 은 평균 길이대 청크.
    picks = [SEED_CHUNKS[2], SEED_CHUNKS[19], SEED_CHUNKS[4],
             SEED_CHUNKS[5], SEED_CHUNKS[6], SEED_CHUNKS[3]]
    evidence = "\n".join(
        f"[ch-{i + 1}] doc_title={c['doc']} · version=1 · heading_path={c['heading']}\n{c['text']}"
        for i, c in enumerate(picks)
    )
    return (
        f"[GUIDELINES]\n{GEN_GUIDELINES}\n\n"
        f"[APPROVED LESSONS]\n" + "\n".join(GEN_LESSONS) + "\n\n"
        f"[EVIDENCE]\n{evidence}\n\n"
        "[QUESTION]\nHow many API requests per minute are allowed?"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 표 출력 (한글 폭 보정)
# ─────────────────────────────────────────────────────────────────────────────
def _w(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _pad(s: str, n: int, right: bool = False) -> str:
    fill = " " * max(0, n - _w(s))
    return fill + s if right else s + fill


def table(headers: list[str], rows: list[list[str]], right: set[int] | None = None) -> None:
    right = right or set()
    widths = [max(_w(h), *(_w(r[i]) for r in rows)) if rows else _w(h)
              for i, h in enumerate(headers)]
    line = "-+-".join("-" * w for w in widths)
    print("  " + " | ".join(_pad(h, widths[i]) for i, h in enumerate(headers)))
    print("  " + line)
    for r in rows:
        print("  " + " | ".join(_pad(c, widths[i], i in right) for i, c in enumerate(r)))


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def setup_console() -> None:
    """Windows 콘솔(cp949)에서 한글·기호 출력이 UnicodeEncodeError로 죽지 않게 한다.

    PowerShell 5.1 기본 코드페이지는 949라 그대로 두면 `—`·`🟢`에서 예외가 난다.
    출력이 깨져 보이면 실행 전에 `chcp 65001` 을 한 번 실행한다.
    """
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    if sys.platform == "win32":
        # 콘솔 코드페이지 변경 실패는 치명적이지 않다 — 출력이 깨질 뿐이다.
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)


# ─────────────────────────────────────────────────────────────────────────────
# 순수 파이썬 코사인 (numpy 불필요)
# ─────────────────────────────────────────────────────────────────────────────
def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0.0 or nb == 0.0 else dot / (na * nb)


def percentile(values: list[float], p: float) -> float:
    """선형 보간 백분위 (numpy 없이)."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


def rescale(sim: float, floor: float, ceil: float) -> int:
    if ceil <= floor:
        return 0
    return round(max(0.0, min(1.0, (sim - floor) / (ceil - floor))) * 100)


def grade_of(score: int, green: int = 80, yellow: int = 50) -> str:
    return "GREEN" if score >= green else ("YELLOW" if score >= yellow else "RED")


# ─────────────────────────────────────────────────────────────────────────────
def require_key() -> None:
    if os.environ.get("OPENAI_API_KEY"):
        return
    print(
        "\n  OPENAI_API_KEY 가 없습니다.\n"
        "\n  PowerShell 에서 아래처럼 설정한 뒤 다시 실행하세요.\n"
        '      $env:OPENAI_API_KEY = "sk-..."\n'
        "      uv run --with openai python scripts/probe_calibration.py\n"
        "\n  (M0 이후 pyproject.toml 이 있으면 --with openai 는 생략할 수 있습니다.)\n"
        "  키는 문서에 커밋하지 말고 .env 또는 세션 환경변수로만 다루세요.\n"
    )
    sys.exit(1)


def get_client():
    try:
        from openai import OpenAI
    except ImportError:
        print(
            "\n  openai 패키지를 찾을 수 없습니다.\n"
            "      uv run --with openai python scripts/probe_calibration.py\n"
            "  또는 프로젝트에 openai 의존성을 추가한 뒤 실행하세요.\n"
        )
        sys.exit(1)
    return OpenAI(timeout=90.0)


def embed(client, texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), 100):
        resp = client.embeddings.create(
            model=EMBEDDING_MODEL, input=texts[i : i + 100], dimensions=EMBEDDING_DIM
        )
        out.extend(d.embedding for d in resp.data)
    return out


def err_of(exc: Exception) -> tuple[str, str]:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    msg = str(exc).replace("\n", " ")
    return (str(status) if status else type(exc).__name__), msg[:110]


# ─────────────────────────────────────────────────────────────────────────────
# 판정 1 · 4 — 검색 스케일과 강제 🔴 분리 가능성
# ─────────────────────────────────────────────────────────────────────────────
def verdict_1_and_4(chunk_vecs, q_vecs) -> dict:
    tops = []
    for q, qv in zip(QUESTIONS, q_vecs):
        sims = [cosine(qv, cv) for cv in chunk_vecs]
        best = max(range(len(sims)), key=lambda i: sims[i])
        tops.append({"q": q, "sim": sims[best], "chunk": SEED_CHUNKS[best], "all": sims})

    section("판정 1 — 검색 점수 스케일 (원시 코사인 → S)")
    print("  `S_raw = round(sim_raw x 100)` 은 **리스케일 전** 값이다. 등급도 리스케일 전 기준.\n")
    table(
        ["#", "질문(ko)", "top-1 근거", "sim_raw", "S_raw", "등급@80/50", "기대"],
        [
            [t["q"]["id"], t["q"]["ko"][:30], t["chunk"]["heading"].split(" > ")[-1][:28],
             f"{t['sim']:.4f}", str(round(t["sim"] * 100)),
             grade_of(round(t["sim"] * 100)), t["q"]["expect"]]
            for t in tops
        ],
        right={3, 4},
    )

    green_sims = [t["sim"] for t in tops if t["q"]["band"] == "green"]
    all_sims = [t["sim"] for t in tops]
    n_ge80 = sum(1 for s in green_sims if round(s * 100) >= 80)
    n_lt50 = sum(1 for s in green_sims if round(s * 100) < 50)

    print()
    print(f"  🟢 기대 대역(Q1~Q6·Q11~Q13): 최소 {min(green_sims):.4f} / 중앙 {percentile(green_sims, 0.5):.4f} "
          f"/ 최대 {max(green_sims):.4f}")
    n_green = len(green_sims)
    print(f"  원시 S≥80 인 건수: {n_ge80}/{n_green}      원시 S<50 인 건수: {n_lt50}/{n_green}")

    s_floor = round(max(0.0, min(all_sims) - FLOOR_MARGIN), 3)
    s_ceil = round(percentile(all_sims, 0.90), 3)
    if s_ceil - s_floor < 0.05:  # 표본이 뭉쳐 있으면 분모가 0에 수렴한다
        s_ceil = round(s_floor + 0.05, 3)

    print()
    if n_ge80 == 0:
        print("  판정: 리스케일 도입 **확정** — 원시 스케일로는 🟢에 도달하는 질문이 없다.")
    elif n_ge80 * 2 >= n_green:
        print("  판정: 리스케일 **불필요** — `s_floor=0.0` / `s_ceil=1.0` 으로 두고 80/50을 유지해도 된다.")
    else:
        print("  판정: 경계 구간 — 리스케일 도입 후 아래 표로 등급 분포를 직접 확인한다.")
    if n_lt50 * 2 >= n_green:
        print("  ⚠️ 최악 시나리오: 🟢 기대 질문 대부분이 원시 <50이다. 시드 확장·top_k 축소도 함께 검토한다.")

    print()
    print(f"  제안: s_floor = {s_floor}  (관측 최소 {min(all_sims):.4f} - {FLOOR_MARGIN})")
    print(f"        s_ceil  = {s_ceil}  (관측 상위 10% 지점)")
    print()
    print("  제안값을 적용했을 때의 등급 (여기가 실제 판단 근거다):")
    rows = []
    ok = 0
    for t in tops:
        s = rescale(t["sim"], s_floor, s_ceil)
        g = grade_of(s)
        band = t["q"]["band"]
        # S 기준 일치 판정은 🟢 대역에만 적용한다. Q7(conflict)·Q8(no_evidence)·Q9 는
        # 유사도가 아니라 ④ 의 conflict / not_answerable 플래그로 잡히고(06 §2 ④, 판정 4),
        # Q10 은 재사용 경로라 검색 점수로 판정하지 않는다.
        match = (g in ("GREEN", "YELLOW")) if band == "green" else True
        if band == "green":
            label = "OK" if match else "MISMATCH"
            ok += 1 if match else 0
        elif band == "reuse":
            label = "—(재사용)"
        else:
            label = "—(④/판정 4)"
        rows.append([t["q"]["id"], f"{t['sim']:.4f}", str(s), g, t["q"]["expect"], label])
    table(["#", "sim_raw", "S(리스케일)", "등급", "기대", "일치"], rows, right={1, 2})
    print()
    print(f"  기대 일치(🟢 대역 {ok}/{n_green}) — 🟢 대역에 MISMATCH 가 있을 때만 "
          "s_floor/s_ceil 을 조정해 재실행한다.")
    print("  Q7·Q8·Q9 의 S 가 높게 나오는 것은 정상이다"
          "(강제 🔴 은 ④ conflict/not_answerable 플래그가 잡는다 — 06 §2④, 판정 4).")

    # ── 판정 4 ────────────────────────────────────────────────────────────
    section("판정 4 — 강제 🔴 분리 가능성 (similarity_floor)")
    green_min = min(green_sims)
    q7 = next(t for t in tops if t["q"]["id"] == "Q7")
    q9 = next(t for t in tops if t["q"]["id"] == "Q9")
    table(
        ["항목", "값", "해석"],
        [
            ["🟢 기대대역 top-1 최소", f"{green_min:.4f}", "🟢 대역 하단"],
            ["Q9 top-1 (무근거)", f"{q9['sim']:.4f}",
             "분리 가능" if q9["sim"] < green_min else "🟢 대역과 겹침"],
            ["Q7 top-1 (충돌)", f"{q7['sim']:.4f}",
             "충돌은 유사도가 아니라 ④ conflict 플래그로 잡는다"],
        ],
        right={1},
    )
    if q9["sim"] < green_min:
        similarity_floor = round((q9["sim"] + green_min) / 2, 3)
        print(f"\n  판정: 유사도만으로 Q9 분리 **가능**. 제안 similarity_floor = {similarity_floor} "
              f"(Q9 {q9['sim']:.4f} 와 🟢 하단 {green_min:.4f} 의 중간)")
    else:
        similarity_floor = s_floor
        print("\n  판정: 유사도만으로 Q9 분리 **불가** (Q9 top-1 이 🟢 대역 최소값 이상).")
        print(f"        similarity_floor 는 s_floor 와 동일한 {similarity_floor} 로 두고,")
        print("        무근거 차단은 ④ 프롬프트의 `not_answerable` 규칙에 전적으로 의존한다.")
        print("        (\"A chunk that merely mentions the topic ... does NOT count as evidence.\")")
    print(f"\n  참고: Q9 의 top-1 근거는 `{q9['chunk']['heading']}` 였다 — "
          "미끼 청크가 실제로 걸리는지 확인한다.")

    return {"s_floor": s_floor, "s_ceil": s_ceil, "similarity_floor": similarity_floor}


# ─────────────────────────────────────────────────────────────────────────────
# 판정 2 — 재사용 임계값
# ─────────────────────────────────────────────────────────────────────────────
def verdict_2(q_vecs) -> dict:
    section("판정 2 — 재사용 임계값 (reuse_threshold / similar_threshold)")
    idx = {q["id"]: i for i, q in enumerate(QUESTIONS)}
    q8_q10 = cosine(q_vecs[idx["Q8"]], q_vecs[idx["Q10"]])

    pairs = []
    for i in range(len(QUESTIONS)):
        for j in range(i + 1, len(QUESTIONS)):
            ids = {QUESTIONS[i]["id"], QUESTIONS[j]["id"]}
            if ids == {"Q8", "Q10"}:
                continue
            pairs.append((cosine(q_vecs[i], q_vecs[j]),
                          f"{QUESTIONS[i]['id']}↔{QUESTIONS[j]['id']}"))
    pairs.sort(reverse=True)
    unrelated_max, unrelated_label = pairs[0]

    table(
        ["항목", "값"],
        [
            ["Q8 ↔ Q10 (같은 질문 — 재사용 목표)", f"{q8_q10:.4f}"],
            ["기본값 0.92 도달 여부", "도달" if q8_q10 >= 0.92 else "**미달**"],
            [f"무관 질문쌍 최대 ({unrelated_label})", f"{unrelated_max:.4f}"],
            ["2·3위 무관쌍", f"{pairs[1][1]} {pairs[1][0]:.4f} / {pairs[2][1]} {pairs[2][0]:.4f}"],
        ],
    )

    reuse_threshold = round(max(0.0, min(1.0, q8_q10 - REUSE_MARGIN)), 3)
    similar_threshold = round(max(0.0, reuse_threshold - REUSE_SIMILAR_GAP), 3)

    print()
    if q8_q10 < 0.92:
        print("  판정: 기본값 0.92 로는 데모 5단계(Q10 재사용)가 **실패**한다 — 재설정 필수.")
    else:
        print("  판정: 기본값 0.92 로도 재사용이 성립한다. 그래도 실측값 기준으로 여유를 둔다.")
    print(f"\n  제안: reuse_threshold   = {reuse_threshold}  (관측 {q8_q10:.4f} - {REUSE_MARGIN})")
    print(f"        similar_threshold = {similar_threshold}  (원본 0.92/0.85 간격 {REUSE_SIMILAR_GAP} 보존)")

    if reuse_threshold <= unrelated_max:
        print()
        print(f"  ⚠️ **오재사용 위험**: 제안 임계값 {reuse_threshold} 이 무관 질문쌍 최대값 "
              f"{unrelated_max:.4f}({unrelated_label}) 이하다.")
        print("     이대로면 서로 다른 질문이 재사용으로 즉답된다. 대응은 둘 중 하나:")
        print(f"     (a) reuse_threshold 를 {round(unrelated_max + 0.01, 3)} 이상으로 올린다")
        print("     (b) LLM 동일성 2차 게이트(결정서 1.1)에 전적으로 의존한다 — 이 경우 게이트는 컷 불가")
    else:
        print(f"\n  안전 여유: 제안 임계값과 무관쌍 최대의 간격 = {reuse_threshold - unrelated_max:+.4f}")
    print("\n  어느 쪽이든 **LLM 동일성 2차 게이트는 구현한다** (단일 임계값 의존 제거, 결정서 1.1).")
    print("  게이트에서 걸러진 건은 `answer.reuse_missed` 이벤트로 기록해야 재질문 즉답률 분모가 생긴다(D26).")

    return {"reuse_threshold": reuse_threshold, "similar_threshold": similar_threshold}


# ─────────────────────────────────────────────────────────────────────────────
# 판정 3 — LLM 호출 규약 · 지연
# ─────────────────────────────────────────────────────────────────────────────
def probe_responses(client, effort: str):
    t0 = time.perf_counter()
    client.responses.create(
        model=LLM_MODEL,
        instructions=GEN_SYSTEM,
        input=_gen_user_prompt(),
        text={"format": {"type": "json_schema", "name": "SentencesOut",
                         "strict": True, "schema": SENTENCES_SCHEMA}},
        reasoning={"effort": effort},
        max_output_tokens=2000,
    )
    return time.perf_counter() - t0


def probe_chat(client, effort: str):
    t0 = time.perf_counter()
    client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "system", "content": GEN_SYSTEM},
                  {"role": "user", "content": _gen_user_prompt()}],
        response_format={"type": "json_schema",
                         "json_schema": {"name": "SentencesOut", "strict": True,
                                         "schema": SENTENCES_SCHEMA}},
        reasoning_effort=effort,
        max_completion_tokens=2000,
    )
    return time.perf_counter() - t0


def probe_temperature(client):
    t0 = time.perf_counter()
    client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": "Reply with the single word OK."}],
        temperature=0,
        max_completion_tokens=64,
    )
    return time.perf_counter() - t0


def verdict_3(client) -> None:
    section("판정 3 — LLM 호출 규약 · 지연 (strict JSON 스키마)")
    print(f"  모델 {LLM_MODEL} / 스키마는 파이프라인 ④ 와 동일 "
          "(전 필드 required · additionalProperties=false · 기본값 필드 없음 · text_ko 포함)\n")

    probes = [
        ("responses.parse 계열", "minimal", lambda: probe_responses(client, "minimal")),
        ("responses.parse 계열", "medium", lambda: probe_responses(client, "medium")),
        ("chat.completions", "minimal", lambda: probe_chat(client, "minimal")),
    ]
    rows, latencies = [], {}
    for surface, effort, fn in probes:
        try:
            dt = fn()
            rows.append([surface, effort, f"{dt:6.2f}s", "OK", ""])
            latencies[(surface, effort)] = dt
        except Exception as exc:  # noqa: BLE001 — 400 여부 자체가 관측 대상
            code, msg = err_of(exc)
            rows.append([surface, effort, "  -   ", code, msg])

    try:
        dt = probe_temperature(client)
        rows.append(["chat.completions", "temperature=0", f"{dt:6.2f}s",
                     "**통과**", "금지 가정과 다름 — 03 §4 재확인"])
    except Exception as exc:  # noqa: BLE001
        code, msg = err_of(exc)
        rows.append(["chat.completions", "temperature=0", "  -   ", code, msg])

    table(["호출 방식", "파라미터", "지연", "결과", "비고"], rows, right={2})

    print()
    minimal = latencies.get(("responses.parse 계열", "minimal")) or \
        latencies.get(("chat.completions", "minimal"))
    if minimal is None:
        print("  판정: `reasoning_effort` 호출이 전부 실패했다. 위 에러 코드를 확인한다.")
        print("        400 이면 파라미터 미지원 확정 — 20초 예산을 포기하거나 비추론 모델로 교체한다.")
    else:
        print(f"  ④형 단건 지연 {minimal:.2f}s × 호출수 = **보수적 상한 근사** — ①번역·⑦구조화를 ④ 와")
        print("  같은 비용으로 치는 상향 가정과, 단건 프롬프트라는 하향 가정이 섞여 부호가 상쇄된다.")
        table(
            ["경로", "LLM 호출", "추정 총 지연", "데드라인 25s"],
            [
                ["🟢/🟡 (①번역 ④생성 ⑤검증)", "3회", f"{minimal * 3:.1f}s",
                 "OK" if minimal * 3 <= 25 else "**초과**"],
                ["🔴 (①번역 ④생성 ⑤검증 ⑦구조화)", "4회", f"{minimal * 4:.1f}s",
                 "OK" if minimal * 4 <= 25 else "**초과**"],
                ["최악 (④ 재시도 2회 포함)", "6회", f"{minimal * 6:.1f}s",
                 "OK" if minimal * 6 <= 25 else "**초과**"],
            ],
            right={2},
        )
        if minimal * 3 > 25:
            print("\n  ⚠️ 데드라인 붕괴: `06 §0` 성능 목표를 재기술하고 ①/⑧ 병렬화 또는 ④⑤ 병합을 검토한다.")
        else:
            print("\n  판정: `LLM_PIPELINE_DEADLINE_SECONDS=25` 안에 들어온다. "
                  "④ 의 `text_ko` 필드로 ⑧ 호출을 제거한 효과가 여기 반영돼 있다.")
        print("  이 표만으로 '안전'을 선언하지 않는다 — 최종 확정은 `06:247` M3 실LLM 스모크와 M9 리허설이다.")
    print()
    print("  ⚠️ 어느 방식이 통과하든 **두 호출 방식을 섞지 않는다** (결정서 1.9). 하나를 골라 프로바이더에 고정한다.")
    print("  ⚠️ `temperature`/`top_p`/`presence_penalty`/`frequency_penalty`/`seed` 는 전달하지 않는다.")
    print("     `max_tokens` 가 아니라 `max_completion_tokens` 다.")


# ─────────────────────────────────────────────────────────────────────────────
def print_settings(measured: dict) -> None:
    section("최종 산출 — projects.settings (DEFAULT_SETTINGS 에 그대로 반영)")
    settings = {
        "green_threshold": 80,
        "yellow_threshold": 50,
        "grounding_min": 60,
        "s_floor": measured["s_floor"],
        "s_ceil": measured["s_ceil"],
        "similarity_floor": measured["similarity_floor"],
        "reuse_threshold": measured["reuse_threshold"],
        "similar_threshold": measured["similar_threshold"],
        "draft_expire_hours": 72,
        "max_lessons": 30,
        "retrieval_top_k": 6,
        "daily_llm_call_limit": 500,
        "saved_wait_assumption_hours": 24,
        "briefing_hour": 9,
        "dnd_start": "22:00",
        "dnd_end": "07:00",
    }
    print()
    print(json.dumps(settings, indent=2, ensure_ascii=False))
    print()
    print("  반영 위치")
    print("    1. `app/config.py` 의 `DEFAULT_SETTINGS` (기본값의 단일 원천 — 룰 1)")
    print("    2. `docs/04-data-model.md §3` 의 기본값 표에서 ⚠️ 잠정값 표기를 제거하고 실측일자를 남긴다")
    print("    3. `docs/05-api-contract.md §3` settings 허용 키 표의 기본값 열")
    print("    4. `docs/08-demo-scenario.md §3` 기대 등급표를 판정 1 의 '제안값 적용 후 등급'과 대조한다")
    print("       — 단 Q7·Q8·Q9 는 ④ 플래그 경로이므로 대조 대상이 아니다")
    print()
    print("  `briefing_timezone` 은 존재하지 않는다 — 브리핑·DND 는 담당자 `users.timezone` 단일 원천이다.")


def main() -> int:
    setup_console()
    print()
    print("불침번 M-1 캘리브레이션 게이트 — probe_calibration.py")
    print(f"임베딩 {EMBEDDING_MODEL}({EMBEDDING_DIM}d) / 생성 {LLM_MODEL}")
    print(f"시드 청크 {len(SEED_CHUNKS)}개 · 질문 {len(QUESTIONS)}건 "
          f"(retrieval_top_k=6 보다 충분히 큰지 확인: {'OK' if len(SEED_CHUNKS) >= 20 else '부족'})")
    print("판정 기준은 파일 상단 독스트링의 사전 등록 표를 따른다 — 결과를 보고 기준을 바꾸지 않는다.")

    require_key()
    client = get_client()

    print("\n임베딩 중...", flush=True)
    try:
        chunk_vecs = embed(client, [c["text"] for c in SEED_CHUNKS])
        q_vecs = embed(client, [q["en"] for q in QUESTIONS])
    except Exception as exc:  # noqa: BLE001
        code, msg = err_of(exc)
        print(f"\n  임베딩 호출 실패 [{code}] {msg}")
        print("  키·네트워크·모델 접근 권한을 확인한 뒤 다시 실행하세요.")
        return 1
    if len(chunk_vecs[0]) != EMBEDDING_DIM:
        print(f"\n  ⚠️ 응답 차원이 {len(chunk_vecs[0])} 다 — vector({EMBEDDING_DIM}) 리터럴과 불일치. "
              "기동 시 fail-fast 대상이다.")

    measured = verdict_1_and_4(chunk_vecs, q_vecs)
    measured.update(verdict_2(q_vecs))
    try:
        verdict_3(client)
    except Exception as exc:  # noqa: BLE001
        code, msg = err_of(exc)
        print(f"\n  판정 3 전체 실패 [{code}] {msg} — 판정 1·2·4 결과는 유효하다.")

    print_settings(measured)

    section("보조 프로브 (DB 필요 — psql 로 직접 실행)")
    print("""
  CREATE EXTENSION IF NOT EXISTS vector;
  SELECT extversion FROM pg_extension WHERE extname='vector';   -- 0.8.0 이상? (hnsw.iterative_scan)
  SELECT '[1,0,0]'::vector <=> '[1,0,0]'::vector AS same,       -- 기대 0 — <=> 는 "거리"다
         '[1,0,0]'::vector <=> '[0,1,0]'::vector AS orthogonal; -- 기대 1

  CREATE TABLE t(id int primary key, doc int, act bool);
  CREATE UNIQUE INDEX ON t(doc) WHERE act;
  INSERT INTO t VALUES (1,1,true),(2,1,false);
  UPDATE t SET act = (id = 2) WHERE doc = 1;   -- 23505 나면 단일 UPDATE 스왑 금지 확정 (04 §7)
  DROP TABLE t;
""")
    print("  로컬 이미지와 **배포 DB 양쪽**에서 확인한다. Railway/Render 매니지드 Postgres가")
    print("  `CREATE EXTENSION vector` 권한을 주는지 배포 당일이 아니라 지금 확인한다 (03 §6).")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
