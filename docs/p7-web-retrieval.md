# P7 safe concurrent web retrieval

P7 adds optional public web evidence without giving the API or worker direct internet
access. Document retrieval remains authoritative and continues when search, DNS, page
fetching, extraction, or the entire web branch fails.

## Network and request flow

```text
backend ──private search network──► SearXNG ──search_egress──► search engines
   │
   └──────private search network──► web-fetcher ──fetch_egress──► validated page IP

backend ──private model network──► TEI reranker + vLLM tokenizer
```

Only `web-fetcher` joins `fetch_egress`. It does not join the edge, data, or model
networks and receives no database, JWT, signing, or backup secrets. The backend remains on
internal Docker networks and its configured SearXNG/fetcher addresses are validated as the
pinned internal service names.

SearXNG's JSON format is enabled in `deploy/searxng/settings.yml`. The client calls the
official `/search` JSON API with general search, safe-search enabled, a 2,000-character
query bound, a 1 MB response bound, and `WEB_TOP_RESULTS=5`. See the official
[SearXNG Search API](https://docs.searxng.org/dev/search_api.html).

## Page-fetch security invariants

`app.web` applies these checks independently on the initial URL and every redirect:

- Accept only HTTP and HTTPS, reject credentials, malformed authorities, control
  characters, backslashes, and ports outside the configured 80/443 allowlist.
- Normalize IDNA hostnames and percent-encode Unicode paths before creating the raw request.
- Resolve A/AAAA records inside the isolated fetcher. Reject the whole answer set if any
  address is non-global, private, loopback, link-local, multicast, reserved, unspecified,
  or a known metadata/virtual-service address.
- Connect to the selected validated literal address, which pins the DNS decision. For
  HTTPS, pass the original normalized hostname as SNI so certificate hostname validation
  remains enabled. Python documents that `server_hostname` controls the hostname matched
  against the certificate in its [asyncio connection API](https://docs.python.org/3.11/library/asyncio-eventloop.html#opening-network-connections).
- Re-resolve and revalidate every redirect, detect loops, and allow at most three redirects.
  A same-host DNS rebinding from a public to a private answer is rejected before the second
  socket opens.
- Apply one eight-second deadline to the complete redirect chain.
- Send `Accept-Encoding: identity` and reject compressed responses, unsupported transfer
  encodings, conflicting framing, oversized headers, malformed chunks, and bodies above
  `WEB_MAX_BYTES=2000000`.
- Accept only `text/html`, `application/xhtml+xml`, and `text/plain`. Strip scripts, styles,
  templates, frames, objects, SVG, and Unicode control/format characters. Retain at most
  `WEB_TEXT_MAX_CHARS=3000` from one page, producing exactly one candidate per final URL.

Python's `ipaddress.is_global` classification is the base reachability check; explicit
metadata ranges are denied in addition to it. See the official
[ipaddress documentation](https://docs.python.org/3/library/ipaddress.html#ipaddress.IPv4Address.is_global).

The fetch API is private and receives URLs only from the internal SearXNG result path. User
input is never treated as a direct fetch URL.

## Combined retrieval and untrusted content

When `web_search=true`, the document and web tasks start concurrently. Results are merged
deterministically by alternating document and web ranks into the bounded
`RERANK_POOL_N=50` pool. Both branches then use the same P6 multilingual cross-encoder,
deduplication, diversity, generation-tokenizer budget, and confidence gate.

Context packing reserves a representative of each available source type and applies:

- the existing section and document/canonical-source caps;
- `DOMAIN_CHUNK_LIMIT=2`;
- `WEB_CONTEXT_LIMIT=4`;
- the total `RERANK_KEEP=12` and `CONTEXT_TOKEN_BUDGET=5000` limits.

Every web passage is enclosed in `BEGIN/END UNTRUSTED WEB SOURCE` boundaries. Active HTML
is removed, but textual prompt-injection instructions are preserved as quoted evidence and
remain explicitly untrusted; P8 generation must never promote them to instructions.

Web outcomes and bounded rejection reasons are available through
`rag_web_retrieval_total` and `rag_web_fetch_total`. A failed/empty web result never removes
document candidates. When `web_search=false`, neither SearXNG nor the fetcher is called.

## Verification

The focused P7 suite covers document+web concurrency and merging, multiple-domain
selection, disabled/failing web fallback, malicious redirects, DNS rebinding, mixed DNS
answers, IPv4/IPv6 private and metadata targets, Unicode/IDNA URLs, whole-chain timeouts,
oversized/compressed/non-text/malformed responses, bounded concurrency, and prompt-injection
text:

```bash
cd backend
UV_CACHE_DIR=/tmp/etoeragcb-uv-cache uv run pytest tests/test_p7.py -q
```

Validate network ownership and host exposure:

```bash
UV_CACHE_DIR=/tmp/etoeragcb-uv-cache uv run python scripts/verify_compose_boundary.py
```

Run the real SearXNG → fetcher → TEI → vLLM path from `deploy/`:

```bash
docker compose --env-file .env -f compose.yml run --rm --no-deps \
  backend python -m app.rag.p7_smoke
```

The smoke also submits `http://127.0.0.1/` to the private fetch service and requires the
`forbidden_address` result before testing public pages.
