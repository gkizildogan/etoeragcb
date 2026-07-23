# P9 Streamlit client

P9 adds a thin Streamlit client over the existing FastAPI contracts. It does
not query PostgreSQL, Qdrant, Redis, model services, storage, SearXNG, or the
worker directly. Caddy remains the only browser-visible service.

## Authentication and tenant state

- Access and rotating refresh tokens are kept in Streamlit's server-side
  session object. They are not written to browser storage, cookies, query
  parameters, logs, or URLs.
- The API client retries an authenticated request once after a `401`, rotates
  both tokens from `/auth/refresh`, and clears the UI session if the refresh or
  retried request is unauthorized.
- Logout calls `/auth/logout`, clears tenant-scoped UI state, and returns to the
  login form. Disabled and revoked accounts follow the same clearing path.
- A user with more than one active membership can reauthenticate into another
  tenant. Sessions, source scope, signed links, feedback state, upload targets,
  and tenant-specific widgets are cleared during the switch.

## Views

### Chat

The sidebar creates, selects, and deletes private conversations. Chat can be
explicitly narrowed to collections and documents; the only retrieval-mode
control is `Search the web too`.

The client accepts every P8 SSE event:

- `start` resets partial output, including after idempotent replay;
- `status` displays the current bounded stage;
- `delta` appends provisional content;
- `replace` authoritatively replaces that content;
- `citations` passes through strict marker, source type, identifier, page, and
  URL validation;
- `done` completes the request; and
- `error` displays a retry-safe failure without inventing an answer.

An interrupted stream reconnects once with the exact same request body,
`client_request_id`, and `Idempotency-Key`. Stored replay therefore does not
create duplicate messages or duplicated text.

Persisted assistant metadata makes `failed`, `partial`, and empty web retrieval
visible. A web failure explicitly says that available document evidence was
retained.

Document citations use a two-step flow. `Prepare` first requests a short-lived,
tenant/user-scoped signed URL from the API. `Open` then navigates the browser
through Caddy's `/api/files/...` route and preserves the cited PDF page
fragment. Web citations are rendered only after validating an absolute
credential-free HTTP(S) URL. Answer and user Markdown use Streamlit's default
HTML-disabled renderer.

### Documents

Members can view document versions and ingestion state. Administrators can:

- upload PDF, text, Markdown, JSONL, and DOCX files;
- create a new version of an existing document;
- assign a new document to collections;
- reindex an active version; and
- delete a document after an explicit confirmation.

JSONL filenames are sent as `application/x-ndjson`, so the supplied
`docstoingest/test.jsonl` fixture follows the ingestion contract even when a
browser reports a generic MIME type. Optional five-second fragment polling
shows `staged`, `processing`, `ready`, `active`, `failed`, and `superseded`
versions without a page-level loop.

### Collections

All members can view collection descriptions and membership. Administrators
can create, rename, describe, delete, and atomically converge each
collection's document membership through the API. The retrieval revision is
shown after list/mutation refreshes.

## Errors and empty states

The UI distinguishes permissions, missing resources, conflicts, validation,
in-progress idempotent requests, rate limits (including `Retry-After`), and
temporary service failures. Empty sessions, conversations, documents, and
collections have actionable messages. Backend authorization remains
authoritative for every operation.

## Automated evidence

Run:

```bash
cd streamlit_app
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest
```

The mocked `streamlit.testing.AppTest` and protocol suite covers:

- login/logout and server-side auth clearing;
- authenticated Chat, Documents, and Collections views;
- authoritative replacement of unsafe draft text;
- fragmented SSE, unknown events, interrupted replay, and stable idempotency;
- one-time refresh rotation and hard expiry after a second `401`;
- citation rejection and signed-link route validation;
- feedback, visible web failure fallback, and chat request scope;
- document status polling and administrator controls.

## Manual HTTPS workflow

The owner selected LAN-only deployment, so run this workflow at
`https://goksu-ubuntu.local` from a phone or laptop that trusts
`artifacts/p1/caddy-local-root.crt`. When public deployment resumes, run the
same workflow at `https://$PUBLIC_DOMAIN` with a publicly trusted certificate
and save separate external-network evidence. The LAN run does not count as a
public-domain or external-port gate.

1. Sign in as an administrator. Create a collection named `P9 toy retrieval`.
2. Open Documents, choose `New document`, upload
   `docstoingest/test.jsonl`, and assign it to that collection.
3. Enable auto-refresh. Confirm the version progresses to `active`, with
   non-zero sections/chunks. A failed version must show its safe error code.
4. Open Chat, create a private conversation, select only `P9 toy retrieval`,
   and ask: `Which song was the signal for the massacre to begin?`
5. Confirm provisional streaming is replaced by the safe final answer, the
   answer cites only displayed source markers, `Prepare` becomes an `Open`
   control, and the file opens at the cited page when a paged source is used.
6. Submit Helpful or Not helpful with an optional comment, refresh the page,
   and confirm the conversation remains private and unchanged.
7. Ask the same document-answerable question with `Search the web too`
   enabled while SearXNG is unavailable. Confirm the document answer remains
   and the UI visibly reports web fallback.
8. Sign out and sign in as a member. Confirm the member can chat and inspect
   statuses but cannot see upload, reindex, delete, collection mutation, or
   membership controls.

P10 has calibrated and activated the retrieval gate. Steps 5 and 7 are now
available through the normal production route, subject to the uploaded
fixture meeting the calibrated confidence thresholds. A credentialed browser
run is still user-operated and is not claimed by the automated suite.

## Deployment evidence

On 2026-07-23 the P9 image was rebuilt and the Streamlit container became
healthy. A CA-verified request through local Caddy returned HTTPS `200` with
HSTS, CSP, `nosniff`, referrer, frame, and permissions-policy headers, and
`/api/healthz` returned `{"status":"ok"}`. No credentialed browser workflow was
claimed.
