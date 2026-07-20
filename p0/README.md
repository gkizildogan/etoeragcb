# P0 hardware and model qualification

This harness is intentionally separate from the application stack. It must
pass on the deployment host before P1 begins. It uses only the model named in
the plan, immutable model revisions and image digests, offline model loading,
and an internal Docker network with no published ports.

## Prerequisites

- Docker Engine with Compose and NVIDIA Container Toolkit
- an RTX 3090 visible to Docker
- 64 GB host RAM
- the three snapshots named by `model-revisions.lock` in `model-cache/`
- the immutable images named by `docker-images.lock`

The current conservative settings are in `.env.example`. To tune a serving
limit, copy that file to `p0/.env`, change one value, and preserve the produced
artifact. These settings are not RAG confidence thresholds.

## Qualification sequence

Run from the repository root as a user that can access the Docker daemon:

```bash
./scripts/p0/run.sh all
./scripts/p0/run.sh pre-reboot
sudo reboot
```

After the host returns, run:

```bash
./scripts/p0/run.sh post-reboot
./scripts/p0/run.sh report
```

`all` verifies pins and local snapshots, starts the three servers, waits for
readiness, runs the full qualification, captures resource state, restarts all
servers, and runs a restart smoke suite. `pre-reboot` records the current Linux
boot ID and healthy service state. `post-reboot` refuses to pass unless the
boot ID changed, then proves that the same pinned stack starts and answers
again.

Evidence is written under `artifacts/p0/` and is intentionally ignored by Git.
Keep it until `docs/feasibility.md` has been updated and reviewed. No endpoint
is published to the host; the one-shot `probe` container is the API client.

Useful individual commands:

```bash
./scripts/p0/run.sh verify
./scripts/p0/run.sh start
./scripts/p0/run.sh qualify
./scripts/p0/run.sh restart
./scripts/p0/run.sh status
./scripts/p0/run.sh logs
./scripts/p0/run.sh report
./scripts/p0/run.sh down
```

`logs` may include fixed synthetic smoke prompts but must not be used with real
documents or secrets. The vLLM request logger is disabled.
