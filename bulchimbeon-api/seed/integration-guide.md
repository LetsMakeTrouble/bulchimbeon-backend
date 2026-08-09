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
