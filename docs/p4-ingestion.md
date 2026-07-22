# P4 staged ingestion and recovery

P4 adds a durable, tenant-scoped document pipeline. A successful HTTP upload
creates only a `staged` version and ingestion job. It does not replace the
currently searchable version. The worker parses, chunks, embeds, indexes, and
validates the staged version before one PostgreSQL transaction changes the
tenant generation and document version pointers.

## Upload contract

`POST /api/documents` requires a tenant administrator, bearer token, and
`Idempotency-Key`. It accepts multipart form fields:

- `file`: required upload;
- `title`: required for a new document;
- `document_id`: optional existing document UUID for a changed version;
- `collection_ids_json`: optional JSON array of collection UUID strings for a
  new document.

The endpoint returns HTTP 202 with the document, version, and job IDs. An
identical retry with the same idempotency key returns that exact response. A
different request with the same key returns 409; a concurrent identical claim
returns 425 with `Retry-After`. Upload and reindex requests are rate limited.

`POST /api/documents/{id}/reindex` stages a new version from the active raw
file and also requires an idempotency key. `GET /api/documents` and
`GET /api/documents/{id}` expose status and bounded error information to tenant
members. `DELETE /api/documents/{id}` requires an administrator and atomically
activates a manifest that excludes the document.

The accepted formats are UTF-8 JSONL, plain text, Markdown, PDF, and DOCX.
Validation covers size, tenant storage quota, safe filename, extension,
declared MIME, magic bytes, empty input, PDF/DOCX structure, and DOCX expanded
size/path safety. Files are mode-restricted under the non-public
`document_files` volume; only backend and worker mount it.

## JSONL fixture mapping

The local `docstoingest/test.jsonl` fixture has 314 non-empty records. Each
record must contain:

```json
{"text":"...","source_page":"...","category":"...","word_count":1}
```

`text` is preserved as source content. `category` becomes a level-1 section,
`source_page` becomes its level-2 child and metadata tag, and JSONL line order
is the stable page order. `category`, `source_page`, `word_count`, and line
number remain in section metadata. The fixture stays ignored as requested;
committed tests create a small equivalent and additionally validate the local
file when it is present.

No public retrieval benchmark was downloaded in P4. This fixture is sufficient
to test ingestion mechanics, hierarchy, repeated passages, and activation.
P10 is the appropriate phase to version a labeled retrieval benchmark and its
provenance instead of treating unlabeled text as retrieval ground truth.

## Chunk and index semantics

The original extracted text is never overwritten. A separate NFKC,
language-aware lexical representation preserves Turkish `İ/I/ı/i` behavior
for hashes and sparse features. Token offsets and counts come from the pinned
BGE-M3 TEI serving tokenizer. Chunks never cross a source page/block or leaf
section and are bounded by `CHUNK_TOKENS` with `CHUNK_OVERLAP`.

Repeated normalized chunks receive deterministic occurrence numbers in
document order. Their UUIDs include document version, page, section, character
offset, occurrence, and content hash, so retries reproduce IDs while identical
passages on distinct pages do not collide.

Qdrant uses one collection named by `QDRANT_COLLECTION`, with named `dense`
and `sparse` vectors and payload indexes for tenant, generation, document,
version, collection, section, pages, hashes, and lexical text. The worker
embeds/upserts bounded batches, checks the exact version point count, and reads
back a sample before activation. Retrieval in P5 must load the immutable active
manifest from PostgreSQL and apply its version IDs to both retrieval branches;
the `created_generation_id` payload is provenance, not the visibility switch.

## Failure and reboot behavior

The database, not Redis, owns job and generation state. Each worker stage
updates a heartbeat, and a lease heartbeat continues during parsing and
tokenization. A fresh `processing` lease prevents arq delivery and reconciliation
from claiming the same job concurrently. Worker startup and a once-per-minute
arq cron scan process `staged`/`queued` jobs and retry `processing` jobs whose
heartbeat is stale. Every retry deletes only that inactive version's partial
points and then uses the same stable chunk IDs.

Failures mark the staged version/generation/job failed and leave the previous
document version and tenant generation pointers unchanged. If another document
activates while indexing is in progress, activation rebases the preparing
manifest under the tenant row lock before committing. A newer version of the
same document prevents an older slow job from activating afterward.

Deletion creates and activates a tombstone manifest without deleting points
in place. Garbage collection derives its protected version set from the current
and configured retained active generations plus all staged/processing/ready/
active versions. It removes only inactive point/file/chunk data outside that
set. P11 will schedule retention/backup-aware GC operations.

## Verification

From `deploy/`:

```bash
docker compose run --rm migrate
docker compose run --rm --no-deps backend python -m app.ingest.smoke
docker compose logs --tail=100 worker
```

The smoke command creates a random tenant/version point, validates the pinned
TEI token offsets and 1,024-dimensional embedding, validates named dense+sparse
Qdrant storage, and deletes its point before exiting. It never uses document
content or leaves a searchable test record.

The P4 automated gate covers upload validation/authorization/idempotency,
original versus lexical text, JSONL hierarchy, stable repeated-content IDs,
staged activation, injected activation failure, stale-heartbeat reconciliation,
tombstoning, and active/retained-generation-safe garbage collection.
