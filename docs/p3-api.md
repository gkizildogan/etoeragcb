# P3 API contract

All routes below are served through the LAN HTTPS endpoint and require an
`Authorization: Bearer <access-token>` header. A missing, disabled, expired,
cross-tenant, or wrong-owner identity receives a generic denial. There is no
public registration endpoint.

## Private sessions and messages

- `GET /api/sessions?limit=&cursor=` lists only the current user's active
  sessions in the active tenant.
- `POST /api/sessions` creates a private session from `{"title":"..."}`.
- `DELETE /api/sessions/{id}` soft-deletes only an owned session.
- `GET /api/sessions/{id}/messages?limit=&cursor=` lists messages in stable
  chronological order after rechecking tenant and user ownership.
- `POST /api/messages/{id}/feedback` creates or updates the current user's
  `-1`/`1` rating on an owned assistant message.

Pagination cursors are opaque, HMAC-authenticated, endpoint-specific, and
rejected if modified. Tenant peers receive the same 404 as nonexistent session
or message IDs.

## Collections

- `GET /api/collections` is available to tenant members.
- `POST /api/collections` and `PATCH|DELETE /api/collections/{id}` require a
  tenant administrator or superuser.
- `PUT|DELETE /api/collections/{id}/documents/{document_id}` changes membership
  only when both resources are active in the principal's tenant.

Active collection names are unique case-insensitively within a tenant. Every
effective metadata or membership mutation increments the tenant's durable
`retrieval_revision`; duplicate adds and missing removals are explicit no-ops
and do not increment it. Future retrieval cache keys must include this revision.

## Durable idempotency

`app.core.idempotency` provides canonical request hashing and atomic claims
scoped by tenant, user, operation, and `Idempotency-Key`. Claim outcomes are:

- `claimed` or `recovered`: the caller owns the work;
- `in_progress`: an identical live request is already running;
- `replay`: return the stored completed response/resource;
- `conflict`: the key was reused with a different canonical request.

Expired or explicitly failed work can be recovered without changing its request
hash. Completion stores the authoritative JSON response and resource ID.
`app.chat.service.persist_user_message` demonstrates atomic exactly-once message
persistence. P4's upload/reindex endpoints now consume the same primitive; chat
will consume it in its later phase.
