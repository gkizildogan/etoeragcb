# P0 feasibility results

## Status

**PASS — completed on 2026-07-20.** The full qualification, container-restart,
and clean-host-reboot suites passed with immutable image and model pins. The
post-reboot boot ID differs from the recorded pre-reboot ID. The generated
evidence is retained under the ignored `artifacts/p0/` directory.

## Pinned Qwen baseline

| Item | Value |
|---|---|
| GPU | NVIDIA GeForce RTX 3090, 24,576 MiB, compute capability 8.6 |
| NVIDIA driver | 595.84 |
| Host RAM | 62 GiB |
| vLLM image | `vllm/vllm-openai:v0.25.1@sha256:e4f88a835143cd22aee2397a26ec6bb80b3a4a6fe0c882bcbc63822904766089` |
| vLLM version | 0.25.1 |
| Model | `cyankiwi/Qwen3.5-9B-AWQ-4bit` |
| Model revision | `73536aa464f9a93c550aa5a916f0113a08b2f384` |
| Cache mode | Offline, local snapshot mounted read-only |
| Quantization detected | `compressed-tensors`; Marlin W4A16 kernel |

Initial conservative serving profile:

- `--language-model-only`
- `--reasoning-parser qwen3`
- `--default-chat-template-kwargs '{"enable_thinking":false}'`
- `--max-model-len 8192`
- `--max-num-batched-tokens 8192`
- `--max-num-seqs 1`
- `--gpu-memory-utilization 0.80`
- `--enforce-eager`

The API was kept on an isolated Docker network with no host-published port.

## Measurements

| Measurement | Result |
|---|---:|
| Checkpoint size reported by vLLM | 8.45 GiB |
| Weight load time | 5.77 s |
| Model-load GPU memory | 7.55 GiB |
| Engine profile/KV-cache/warm-up | 67.17 s |
| Qualification vLLM readiness wait | 144.103 s |
| Available KV-cache memory | 10.33 GiB |
| Reported GPU KV-cache capacity | 275,941 tokens |
| GPU memory after qualification | 22,489 / 24,576 MiB |
| Steady-state container RAM | 10.57 GiB |
| Streaming time to first content | 0.0969 s |
| Decode rate after first content | 34.14 token/s |
| Largest stable prompt | 8,000 tokens |
| Largest measured concurrency | 2 |
| Embedding dimension | 1,024 |
| Embedding batch throughput | 47.569 items/s |
| Rerank median / p95 latency | 0.3976 / 0.4186 s |

The reported 33.68x theoretical 8K KV-cache concurrency is not the configured
or validated concurrency. This baseline deliberately caps `max_num_seqs` at 1.

## Functional smoke tests

Every request used `chat_template_kwargs.enable_thinking=false`; no `/nothink`
suffix was used.

| Test | Result | Completion | Total request time |
|---|---|---:|---:|
| English exact response | HTTP 200, exact text, `reasoning: null` | 8 tokens | 1.407 s (first request) |
| Turkish exact response | HTTP 200, exact text, `reasoning: null` | 10 tokens | 0.382 s |
| Schema-constrained Turkish planner | HTTP 200, valid six-field JSON, `intent=knowledge`, `reasoning: null` | 155 tokens | 6.314 s |
| Citation marker | HTTP 200, `The capital is Ankara [S1].`, `reasoning: null` | 9 tokens | 0.375 s |
| Final health probe | HTTP 200 | n/a | n/a |

The planner request's end-to-end effective completion rate was approximately
24.5 tokens/s, including prompt processing and non-streamed response overhead.
It is not a standardized decode-throughput result.

## Accepted application limits

- `MAX_MODEL_LEN=8000`; no larger checkpoint claim is used.
- vLLM may admit at most two sequences because concurrency 2 was measured.
  Application generation concurrency starts at 1 to preserve headroom.
- `EMBED_DIM=1024`.
- Startup probes allow the model services up to 180 seconds on this host.
- Generation requests start with `MAX_NEW_TOKENS=1000`; combined history,
  context, and output budgets must remain within the measured 8,000 tokens.

## Reproducible qualification harness

The checks are automated by `p0/compose.yml` and `scripts/p0/run.sh`. The
harness loads all snapshots offline, publishes no host ports, uses the
immutable image/model pins, explicitly disables thinking at the server and
request layers, and writes evidence to `artifacts/p0/`.

To repeat the gate on the deployment host:

```bash
./scripts/p0/run.sh all
./scripts/p0/run.sh pre-reboot
sudo reboot
./scripts/p0/run.sh post-reboot
./scripts/p0/run.sh report
```

The generated report says `PASS`; the limits above are the values consumed by
the P1 configuration and Compose stack.
