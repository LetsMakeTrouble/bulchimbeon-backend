# Web Client Release Notes

## v0.5 — Streaming and Reconnect
This release moved answer completion off polling and onto the shared SSE stream. The client
now holds one stream per session instead of one per screen, which removed the duplicate
notification rows that appeared when two tabs were open. Reconnect behaviour changed in this
release: after the stream drops, the client waits 3 seconds before resubscribing, down from
the previous delay, so a short tunnel restart is no longer visible to the user. Screens that
mount while the stream is down render the last cached list and refresh once it returns.

## v0.4 — Review Queue and Cross-check
The answerer's review queue gained keyboard handling: `j` and `k` move through cards,
`Enter` opens the focused card, and the approve control is reachable without the mouse. The
queue sorts red cards first and older cards before newer ones within the same grade, which
matches the order the API returns. Cross-check buttons were added under published answers in
this release, and pressing one is irreversible for the asker — a second press is rejected by
the API rather than replacing the first verdict.

## v0.3 — Document Shelf
Uploading a document now shows ingest progress per version rather than a single spinner for
the whole shelf. A version moves through `processing`, then either `ready` or `failed`, and
only a ready version can be activated. Activating a new version marks the previous one as
superseded but keeps it readable, so a citation that points at an older version still opens.
Failed versions show the reason returned by the API instead of a generic error.
