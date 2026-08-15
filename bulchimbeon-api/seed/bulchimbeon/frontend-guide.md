# Bulchimbeon Web Client Guide

## Routes and Screens
The web client is a React single-page application. Public routes are `/login` and `/signup`;
everything else requires a session. The authenticated shell mounts `/chat` for asking a
question, `/questions` for the question list and its detail history, `/documents` for the
document shelf, `/inbox` for the answerer's review-card queue, `/briefing` for the morning
digest, `/official-qa` for confirmed knowledge, `/metrics` for the accuracy dashboard,
`/members` for member and role management, `/settings` for project settings, and
`/notifications` for the notification list. Unknown paths redirect to `/`.

## Token Lifetimes
The access token is valid for 30 minutes. The refresh token is valid for 14 days. Once the
access token passes that lifetime the client must refresh it before the next request.

## Authentication and Token Handling
Login posts an email and password to `POST /api/v1/auth/login` and receives an access token
and a refresh token. Every authenticated request carries `Authorization: Bearer <access
token>`. When a request comes back `401`, the client calls `POST /api/v1/auth/refresh` once
with the refresh token and replays the original request. If that refresh also fails, the
client clears the stored session and routes the user back to `/login`. The client never
stores the password.

## Live Updates over SSE
Notifications, answer completion, and review-card arrival reach the client over a single
Server-Sent Events stream rather than polling. The client subscribes once per session and
fans events out to the screens that are mounted. When the stream drops, the client waits
3 seconds and then resubscribes. Because SSE is a long-lived streaming response, any proxy
in front of the API must not buffer it; a single buffering layer silently stalls the stream
without producing an error on either side.

## Build-time Configuration
The API base URL comes from `VITE_API_BASE_URL` and is baked into the bundle at build time
by Vite. It is not read at runtime, so changing the environment variable and restarting the
container has no effect — the frontend image must be rebuilt. In the same-origin deployment
this value is the relative path `/api/v1`, which means the browser sends API requests to the
same host that served the page and no CORS preflight is involved.

## Static Serving and Caching
The production image serves the built bundle with nginx on port 8080 and does nothing else;
it holds no `/api` proxy block, because the tunnel in front routes `/api/*` straight to the
API. Files under `/assets/` carry a content hash in their filename and are served with a
one-year immutable cache. A missing bundle under `/assets/` returns 404 instead of falling
back to the SPA entry, so a stale page fails loudly rather than receiving HTML where it
expected JavaScript. `index.html` is served with `no-cache` so a deploy is picked up on the
next navigation, and every other path falls back to `index.html` for client-side routing.

## Grade Badges and Cross-check Controls
An answer arrives with a matching rate and one of three grades. A green answer renders the
rate with its citations expanded. A yellow answer renders the same layout plus an
"awaiting answerer confirmation" badge, so the reader knows the answer is a reference and
not a confirmed one. A red answer is not published to the asker at all; the question detail
shows that it was handed to the answerer instead. Published answers carry two cross-check
buttons, "correct" and "different", which the asker presses after actually using the answer.
Pressing "different" routes the answer back to the answerer for review.
