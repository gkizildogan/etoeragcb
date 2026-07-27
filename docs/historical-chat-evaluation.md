# Historical chat retrieval evaluation

This workflow creates a private, human-labeled diagnostic evaluation from a
tenant's historical member and administrator knowledge chats. It keeps two
views separate:

- **Recorded evidence** contains only sources packed into the historical
  answer. Legacy messages did not store the full retrieval pool, so this view
  is never described as a complete historical reranker evaluation.
- **Current-corpus replay** restores the original explicit document and
  collection scopes, then runs the current production planner, dense+sparse
  Qdrant retrieval, reranker, deduplication, candidate-confidence filter,
  confidence gate, and context packer. Redis caches, answer generation, and
  external web retrieval are disabled.

The first report is diagnostic. Do not use it to recalibrate the production
confidence gate and do not make it a CI gate.

## Prepare a run

`prepare-history` is not a standalone shell command. It is a subcommand of
`app.evaluation.cli`.

### Docker Compose deployment

First, find the active tenant UUID. While logged in, open
`https://<your-app-host>/ui-api/me` in another browser tab and copy the
`active_tenant_id` value from the JSON response.

The running backend image may have been built before this evaluator was added.
The following check deliberately mounts the current backend source read-only,
so it works without restarting the application:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml run \
  --rm --no-deps \
  --volume "$PWD/backend:/workspace:ro" \
  --workdir /workspace \
  backend python -m app.evaluation.cli prepare-history --help
```

To install the command into the backend image permanently, rebuild it. A
rebuild is not required for the source-mounted evaluation command below.

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml build backend
```

The evaluation must run in the Compose network because PostgreSQL, Qdrant,
vLLM, and TEI use private service names. The backend also needs its production
secrets and the document storage volume. Prepare a host artifact directory,
temporarily give it to the container's fixed application UID, and run:

```bash
mkdir -p artifacts/evaluation/historical-chat
sudo chown -R 10001:10001 artifacts/evaluation/historical-chat

docker compose --env-file deploy/.env -f deploy/compose.yml run \
  --rm --no-deps \
  --volume "$PWD/backend:/workspace:ro" \
  --workdir /workspace \
  --volume "$PWD/artifacts:/artifacts" \
  backend python -m app.evaluation.cli prepare-history \
  --tenant-id <active-tenant-uuid> \
  --output /artifacts/evaluation/historical-chat/test-1 \
  --include-private-content

sudo chown -R "$(id -u):$(id -g)" artifacts/evaluation/historical-chat
```

Use a new output name for every run. Existing run directories are deliberately
not overwritten. Keep the application stack running while preparation runs.

### Non-container development environment

Only use the host command below when the host shell already has every backend
setting and secret and can reach the configured PostgreSQL, Qdrant, vLLM, and
TEI addresses. Run it from `backend/`:

```bash
uv run python -m app.evaluation.cli prepare-history \
  --tenant-id <active-tenant-uuid> \
  --output ../artifacts/evaluation/historical-chat/2026-07-27 \
  --include-private-content
```

Optional `--start-at` and `--end-at` arguments accept RFC3339 timestamps. The
end is exclusive. Existing output directories are rejected.

Preparation selects valid user/assistant knowledge pairs in non-deleted chat
sessions. It includes current members, tenant admins, and superusers. Legacy
roles are classified from current membership at export; new messages store the
request-time role. Malformed pairs and conversation-only turns are excluded.
No emails or user identifiers are written.

Before producing labels, preparation audits all active document versions and
historical versions referenced by stored sources. It checks raw-file hashes,
version/job/generation lifecycle, generation manifests, relational counts and
chunk contents, deterministic reparsing/chunking, and every paginated Qdrant
point and vector type. Active inconsistencies fail the command. Collected
historical evidence is reported as `unavailable_by_retention`, not corruption.
An unavailable or changed tokenizer/model revision is a comparability warning.

## Private artifacts

The run directory is mode `0700`; each file is mode `0600`. It contains:

- `manifest.json` — tenant, time range, current generation/revision,
  configuration and evaluator hashes, counts, and run-file hashes.
- `examples.jsonl` — private query/answer text, scopes, role group, feedback,
  route/gate metadata, and corpus comparability.
- `candidates.jsonl` — review text and provenance plus recorded, hybrid branch,
  rerank, deduplication, candidate-confidence filtering, and packing outcomes.
- `labels.template.jsonl` and `labels.jsonl` — identical blank review records.
- `ingestion-audit.json` — PASS/FAIL/WARN evidence for each version.
- `report.json` and `report.md` — created after scoring.

`artifacts/` is ignored by Git. These files contain private chat and document
content and are outside ordinary application backups. If a labeled dataset is
valuable, place a copy in a separately access-controlled, encrypted backup.
Never email it or move it into a shared source tree.

## Human labels

Fill every record in `labels.jsonl`. Query records use:

```json
{"schema_version":1,"kind":"query","example_id":"<assistant-message-uuid>","answerable":true,"language":"tr","label_source":"human","reviewer":"reviewer-1","reviewed_at":"2026-07-27T10:00:00Z","notes":null}
```

Candidate records use:

```json
{"schema_version":1,"kind":"candidate","example_id":"<assistant-message-uuid>","candidate_key":"document:<version-uuid>:<chunk-uuid>","relevance":3,"label_source":"human","reviewer":"reviewer-1","reviewed_at":"2026-07-27T10:00:00Z","notes":null}
```

Grades mean:

- `0`: unrelated.
- `1`: partially useful context.
- `2`: substantially but incompletely supports the answer.
- `3`: directly and completely supports it.

Human review is authoritative. A future automated judge may write
`label_source: "llm_suggestion"` records in the same shape, but scoring rejects
them until a person confirms and changes the provenance to `human`. Scoring
also rejects blank, missing, duplicate, unknown, or malformed labels.

## Score a run

After preparation:

1. Open `examples.jsonl` to review each question and answer.
2. Open `candidates.jsonl` to review the pooled passages and ranks.
3. Edit every blank record in `labels.jsonl`. Do not edit
   `labels.template.jsonl`.
4. Supply a query label and a relevance label for every candidate. Scoring
   intentionally fails if any record remains blank.

Scoring does not contact production services, so it can run on the host from
`backend/`:

```bash
uv run python -m app.evaluation.cli score-history \
  --run-dir ../artifacts/evaluation/historical-chat/test-1
```

Read `report.md` for the summary and `report.json` for per-query metrics and
breakdowns.

The report includes precision@1/3/5/10, judged-pool recall@5/10, MRR, graded
nDCG@10, answerability/gate precision and recall, false-positive and
false-negative example IDs, and pre/post-reranker deltas. It also reports
relevant-document rank lift, harmful demotions, per-query win/tie/loss, and
breakdowns by member/admin/superuser, language, scope type, feedback, and corpus
comparability. Recorded-view metrics are explicitly restricted to stored
packed sources.

Both commands are read-only with respect to PostgreSQL, Qdrant, chat history,
human labels, thresholds, indexes, and production configuration.
