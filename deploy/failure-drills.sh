#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${ENV_FILE:-${project_root}/deploy/.env}"
domain="$(python3 "${project_root}/scripts/env_value.py" "${env_file}" PUBLIC_DOMAIN)"
ca_file="${project_root}/artifacts/p1/caddy-local-root.crt"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
artifact_dir="${project_root}/artifacts/p11/failure-${timestamp}"
mkdir -p "${artifact_dir}"
compose=(
  docker compose
  --env-file "${env_file}"
  -f "${project_root}/deploy/compose.yml"
)

recover_services() {
  local exit_status=$?
  set +e
  "${compose[@]}" start postgres qdrant redis caddy >/dev/null 2>&1
  "${compose[@]}" up -d backend worker >/dev/null 2>&1
  return "${exit_status}"
}
trap recover_services EXIT

consistency_args=(python -m app.operations.backup verify-live)
if [[ "${ALLOW_EMPTY_LIVE:-0}" == 1 ]]; then
  consistency_args+=(--allow-empty)
fi

ready_status() {
  "${compose[@]}" exec -T backend python -c \
    'import sys, urllib.error, urllib.request
try:
    response = urllib.request.urlopen("http://127.0.0.1:8000/api/readyz", timeout=5)
    sys.stdout.write(str(response.status))
except urllib.error.HTTPError as exc:
    sys.stdout.write(str(exc.code))
except urllib.error.URLError:
    sys.stdout.write("000")'
}

wait_ready() {
  local attempts=0
  until [[ "$(ready_status)" == 200 ]]; do
    attempts=$((attempts + 1))
    if (( attempts >= 60 )); then
      echo "Readiness did not recover within 120 seconds." >&2
      return 1
    fi
    sleep 2
  done
}

wait_https() {
  local attempts=0
  until curl --cacert "${ca_file}" --fail --silent --show-error \
    --connect-timeout 2 "https://${domain}/api/healthz" >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    if (( attempts >= 60 )); then
      echo "HTTPS did not recover within 120 seconds." >&2
      return 1
    fi
    sleep 2
  done
}

echo "redis_flush_start" | tee -a "${artifact_dir}/events.log"
"${compose[@]}" exec -T redis redis-cli FLUSHDB
"${compose[@]}" restart worker backend
wait_ready
"${compose[@]}" exec -T backend "${consistency_args[@]}" \
  >"${artifact_dir}/after-redis-loss.json"

for dependency in qdrant postgres; do
  echo "${dependency}_outage_start" | tee -a "${artifact_dir}/events.log"
  "${compose[@]}" stop --timeout 20 "${dependency}"
  status="$(ready_status)"
  if [[ "${status}" != 503 ]]; then
    echo "Expected readiness 503 while ${dependency} was stopped, found ${status}." >&2
    exit 2
  fi
  "${compose[@]}" start "${dependency}"
  wait_ready
  echo "${dependency}_recovered" | tee -a "${artifact_dir}/events.log"
done

"${compose[@]}" kill -s SIGKILL worker
"${compose[@]}" up -d worker
wait_ready
echo "worker_kill_reconciled" | tee -a "${artifact_dir}/events.log"

"${compose[@]}" restart caddy
wait_https
curl --cacert "${ca_file}" --fail --silent --show-error \
  "https://${domain}/api/healthz" >"${artifact_dir}/https-health.json"
"${compose[@]}" exec -T backend "${consistency_args[@]}" \
  >"${artifact_dir}/final-consistency.json"
"${compose[@]}" ps --format json >"${artifact_dir}/compose-final.json"
echo "Failure drills passed; evidence: ${artifact_dir#${project_root}/}"
