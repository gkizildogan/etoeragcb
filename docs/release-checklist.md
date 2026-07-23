# P11 release checklist

Status date: 2026-07-23. **v1 is not delivered.**

| Gate | Status | Evidence / remaining action |
|---|---|---|
| P0 pinned RTX 3090/model qualification | Pass | `artifacts/p0/report.md`, including post-reboot and measured concurrency |
| LAN HTTPS | Pass | Caddy internal CA at `goksu-ubuntu.local`; CA artifact under `artifacts/p1/` |
| Public ACME certificate | Deferred | Owner selected LAN-only operation; cannot count as public HTTPS |
| External port scan | Deferred | Requires public routing and a separate network; local Compose boundary exposes only Caddy 80/443 |
| Retrieval evaluation/calibration | Pass | `backend/evaluation/reports/p10-retrieval-v1.{json,md}` |
| Cache metrics | Pass | Unit/integration coverage and deployed private metrics |
| Retention/GC | Pass (guarded) | Daily schedule; payload GC blocked until recent verified off-machine backup |
| Encrypted off-machine backup | Pass | Quiesced API/worker; Restic snapshot `8380ab6f…`; full repository check, authenticated restricted-Drive upload, and zero-difference `rclone check` |
| Authenticated Google Drive copy | Pass | Restricted folder; OAuth-authorized `gdrive` remote; daily user timer enabled |
| Clean restore mechanics | Pass, not release eligible | `artifacts/p11/restore-20260723T152452Z.json`; clean PostgreSQL/Qdrant/files restore from local encrypted repository, source had accounts but no documents/chats |
| Full §9 restore | Blocked | Strict drill now downloads a fresh repository copy from Drive; upload/activate a test document, create a chat, complete off-machine backup, then run it |
| HTTPS renewal assumptions | Partial | Internal-CA persistence/config and live Caddy restart passed; public ACME renewal cannot be tested in LAN mode |
| Application host reboot | Deferred by owner; service roll pass | P0 model reboot passed earlier; all Python services were recreated on 3.13.14 and returned healthy. Owner will manually stop/start Compose during development and revisit host reboot later |
| Ingestion interruption/reconciliation | Pass | P4 injected interruption/stale heartbeat coverage plus live worker SIGKILL/reconcile drill in `artifacts/p11/failure-20260723T151444Z/` |
| PostgreSQL/Qdrant consistency | Mechanism pass; empty live corpus | Live outage/recovery/count checks passed; strict verifier correctly refuses release eligibility without active chunks |
| Cache invalidation/Redis loss | Pass | Live Redis flush plus backend/worker recovery in `artifacts/p11/failure-20260723T151444Z/` |
| Account disablement | Pass | Auth/P8 tests prove immediate API and signed-link denial |
| Cross-tenant/session access | Pass | Auth/P3/P8 exhaustive negative tests |
| Signed-link tamper/expiry | Pass | P8 tests and strict restore verifier |
| Dependency audit | Pass | Pinned local `pip-audit` found no known vulnerabilities in either production lock; SHA-pinned CI job remains enforced |
| Python runtime | Pass | Backend, worker, web fetcher, and Streamlit report Python 3.13.14; hash locks, static checks, strict typing, tests, live migration, HTTPS health, and P10 provenance verification pass |
| Image scan | Pass under high gate | Digest-pinned Grype: zero active critical/high findings. Exact 3.13.14 suppression for CVE-2026-15308 is backed by CPython's security changelog showing `gh-153030` fixed in 3.13.14 |
| Prometheus alerts | Pass | Six rules validate; private Alertmanager delivered `ETOERAGCB WARNING` through Gmail, recipient confirmed receipt, and counters showed one notification with zero email failures |
| Resource/load smoke | Partial | `artifacts/p11/load-20260723T163811Z/`: post-upgrade 100/100 HTTP 200 at concurrency 10; authenticated chat load awaits populated data and operator credentials |

## Automated release bundle

Run:

```bash
deploy/release-check.sh
```

It validates the Compose boundary, LAN TLS chain/expiry, HTTPS health, strict
live data consistency, committed retrieval evaluation, security-critical
tests, and bounded health load. It intentionally fails while active document
and retrieval evidence is absent.

Run reversible dependency/cache/worker drills separately:

```bash
deploy/failure-drills.sh
```

Only mark P11 and v1 complete after every deferred/blocked row is replaced by
dated passing evidence.
