# Orders API Specification v2.1

## Order Lookup Response Fields — GET /v2/orders/{order_id}
Looking up a single order returns the fields below. The order lookup response always
includes `user_id`. Response fields:
- `order_id` (string): unique order identifier
- `user_id` (string): the purchaser's account id. Included in all responses since v2.0.
- `status` (string): one of `pending`, `paid`, `shipped`, `delivered`, `cancelled`
- `currency` (string): ISO 4217. KRW, USD, and JPY are supported.
- `total_amount` (integer): amount in the smallest currency unit

## Supported Currencies
The supported currencies are KRW, USD, and JPY. Those three settlement currencies are the
only currencies the API supports, and they are identified by their ISO 4217 codes.

## Currency Rules
A project may be configured to accept more than one of the supported currencies, but every
individual order carries exactly one. An order's currency is fixed when the order is created
and cannot be changed afterwards. Amounts are always expressed in the smallest unit of the
currency — KRW and JPY have no minor unit, so `total_amount` is a whole won or yen figure,
while USD amounts are in cents. Currency conversion is not performed by the platform; the
buyer is charged in the currency recorded on the order.

## Authentication
All endpoints require a Bearer token issued by the Auth service.
Access tokens expire after 24 hours. Refresh tokens are not provided;
clients must re-authenticate after expiry.

## Rate Limiting
API calls are limited to 60 requests per minute per API key.
Exceeding the limit returns HTTP 429 with a `Retry-After` header.

## Pagination for List Endpoints
The pagination method for list endpoints is cursor-based paging. Paging through a list
uses two query parameters, `cursor` and `limit`. The `limit` parameter sets the page size
and may not exceed 100; when it is omitted the page size defaults to 20. Each list
response carries a `next_cursor` value, and the client pages through the list by sending
that value back as `cursor` on the next request. A response whose `next_cursor` is null
is the last page. Offset-based paging is not supported in v2.1.

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

## Idempotency and Key Retention
An idempotency key is retained for 24 hours, and that retention window is how long the
key stays valid. Write endpoints accept an optional `Idempotency-Key` header containing a
client-generated UUID. When a key is supplied, the platform stores the first response for that key and
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
