#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${ENV_FILE:-${project_root}/deploy/.env}"
domain="$(python3 "${project_root}/scripts/env_value.py" "${env_file}" PUBLIC_DOMAIN)"
ca_file="${project_root}/artifacts/p1/caddy-local-root.crt"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
artifact_dir="${project_root}/artifacts/p11/release-${timestamp}"
mkdir -p "${artifact_dir}"
compose=(
  docker compose
  --env-file "${env_file}"
  -f "${project_root}/deploy/compose.yml"
)

python3 "${project_root}/scripts/verify_compose_boundary.py" \
  | tee "${artifact_dir}/network-boundary.txt"
docker compose --env-file "${env_file}" -f "${project_root}/deploy/compose.yml" \
  --profile operations --profile monitoring config --quiet

openssl s_client \
  -connect "${domain}:443" \
  -servername "${domain}" \
  -CAfile "${ca_file}" \
  -verify_return_error </dev/null >"${artifact_dir}/tls-handshake.txt" 2>&1
openssl s_client \
  -connect "${domain}:443" \
  -servername "${domain}" \
  -CAfile "${ca_file}" </dev/null 2>/dev/null |
  openssl x509 -noout -issuer -subject -dates \
    >"${artifact_dir}/tls-certificate.txt"
openssl s_client \
  -connect "${domain}:443" \
  -servername "${domain}" \
  -CAfile "${ca_file}" </dev/null 2>/dev/null |
  openssl x509 -checkend 604800 -noout

curl --cacert "${ca_file}" --fail --silent --show-error \
  "https://${domain}/api/healthz" >"${artifact_dir}/https-health.json"
"${compose[@]}" exec -T backend python -m app.operations.backup verify-live \
  >"${artifact_dir}/live-consistency.json"
"${compose[@]}" exec -T backend python -m app.evaluation.cli verify \
  | tee "${artifact_dir}/retrieval-evaluation.txt"
"${compose[@]}" ps --format json >"${artifact_dir}/compose.json"

(
  cd "${project_root}/backend"
  UV_CACHE_DIR=/tmp/etoeragcb-uv-cache uv run --frozen pytest \
    tests/test_auth.py tests/test_p3.py tests/test_p4.py tests/test_p8.py tests/test_p11.py
) | tee "${artifact_dir}/release-tests.txt"

python3 "${project_root}/scripts/load_test.py" \
  --base-url "https://${domain}" \
  --ca-file "${ca_file}" \
  --mode health \
  --requests 100 \
  --concurrency 10 \
  --output "${artifact_dir}/health-load.json"
echo "Automated release checks passed; evidence: ${artifact_dir#${project_root}/}"
