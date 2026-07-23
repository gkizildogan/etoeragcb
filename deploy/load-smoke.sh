#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${ENV_FILE:-${project_root}/deploy/.env}"
domain="$(python3 "${project_root}/scripts/env_value.py" "${env_file}" PUBLIC_DOMAIN)"
mode="${1:-health}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
artifact_dir="${project_root}/artifacts/p11/load-${timestamp}"
mkdir -p "${artifact_dir}"

docker compose \
  --env-file "${env_file}" \
  -f "${project_root}/deploy/compose.yml" \
  ps --format json >"${artifact_dir}/compose-before.json"
docker stats --no-stream --format '{{json .}}' >"${artifact_dir}/stats-before.jsonl"
nvidia-smi \
  --query-gpu=timestamp,name,memory.used,memory.total,utilization.gpu \
  --format=csv,noheader >"${artifact_dir}/gpu-before.csv"

load_args=(
  --base-url "https://${domain}"
  --ca-file "${project_root}/artifacts/p1/caddy-local-root.crt"
  --mode "${mode}"
  --output "${artifact_dir}/load.json"
)
if [[ "${mode}" == health ]]; then
  load_args+=(--requests 100 --concurrency 10)
elif [[ "${mode}" == chat ]]; then
  if [[ -z "${LOAD_TEST_EMAIL:-}" ]]; then
    echo "Set LOAD_TEST_EMAIL for the authenticated chat smoke." >&2
    exit 2
  fi
  load_args+=(--requests 1 --concurrency 1 --email "${LOAD_TEST_EMAIL}")
else
  echo "Usage: deploy/load-smoke.sh [health|chat]" >&2
  exit 2
fi
python3 "${project_root}/scripts/load_test.py" "${load_args[@]}"

docker stats --no-stream --format '{{json .}}' >"${artifact_dir}/stats-after.jsonl"
nvidia-smi \
  --query-gpu=timestamp,name,memory.used,memory.total,utilization.gpu \
  --format=csv,noheader >"${artifact_dir}/gpu-after.csv"
echo "Load/resource smoke passed; evidence: ${artifact_dir#${project_root}/}"
