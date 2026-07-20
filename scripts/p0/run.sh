#!/usr/bin/env bash
set -euo pipefail

P0_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
P0_PROJECT_ROOT="$(cd -- "$P0_SCRIPT_DIR/../.." && pwd)"
P0_ARTIFACTS_DIR="$P0_PROJECT_ROOT/artifacts/p0"
P0_COMPOSE_FILE="$P0_PROJECT_ROOT/p0/compose.yml"
P0_ENV_FILE="$P0_PROJECT_ROOT/p0/.env"

set -a
# shellcheck source=/dev/null
. "$P0_PROJECT_ROOT/docker-images.lock"
# shellcheck source=/dev/null
. "$P0_PROJECT_ROOT/model-revisions.lock"
if [[ -f "$P0_ENV_FILE" ]]; then
  # shellcheck source=/dev/null
  . "$P0_ENV_FILE"
fi
set +a

if [[ "$MODEL_CACHE_HOST_PATH" != /* ]]; then
  MODEL_CACHE_HOST_PATH="$P0_PROJECT_ROOT/${MODEL_CACHE_HOST_PATH#./}"
fi

mkdir -p "$P0_ARTIFACTS_DIR"
export MODEL_CACHE_HOST_PATH P0_PROJECT_ROOT P0_ARTIFACTS_DIR
export P0_RUN_UID="$(id -u)"
export P0_ARTIFACTS_GID="$(stat -c '%g' "$P0_ARTIFACTS_DIR")"

P0_COMPOSE=(
  docker compose
  --project-directory "$P0_PROJECT_ROOT"
  --env-file "$P0_PROJECT_ROOT/docker-images.lock"
  --env-file "$P0_PROJECT_ROOT/model-revisions.lock"
)
if [[ -f "$P0_ENV_FILE" ]]; then
  P0_COMPOSE+=(--env-file "$P0_ENV_FILE")
fi
P0_COMPOSE+=(-f "$P0_COMPOSE_FILE")

p0_timestamp() {
  date -u +%Y%m%dT%H%M%SZ
}

p0_verify_snapshot() {
  local model_name="$1"
  local revision="$2"
  local snapshot="$3"
  local host_snapshot
  host_snapshot="$MODEL_CACHE_HOST_PATH/${snapshot#${MODEL_CACHE_CONTAINER_PATH}/}"
  if [[ ! -d "$host_snapshot" ]]; then
    echo "Missing snapshot for $model_name: $host_snapshot" >&2
    return 1
  fi
  if [[ "$(basename -- "$host_snapshot")" != "$revision" ]]; then
    echo "Revision/path mismatch for $model_name" >&2
    return 1
  fi
  if find -L "$host_snapshot" -type l -print -quit | grep -q .; then
    echo "Broken symlink in snapshot for $model_name" >&2
    return 1
  fi
  if [[ ! -f "$host_snapshot/config.json" ]]; then
    echo "Missing config.json for $model_name" >&2
    return 1
  fi
}

p0_verify_image() {
  local image_ref="$1"
  if [[ "$image_ref" != *@sha256:* ]]; then
    echo "Image is not digest-pinned: $image_ref" >&2
    return 1
  fi
  docker image inspect "$image_ref" >/dev/null
}

p0_verify_tei_limits() {
  local concurrent_requests="${TEI_MAX_CONCURRENT_REQUESTS:-32}"
  local client_batch_size="${TEI_MAX_CLIENT_BATCH_SIZE:-32}"
  local qualification_batch_size="${P0_EMBED_BATCH_SIZE:-32}"
  local name value
  for name in concurrent_requests client_batch_size qualification_batch_size; do
    value="${!name}"
    if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
      echo "Invalid positive integer for $name: $value" >&2
      return 1
    fi
  done
  if ((qualification_batch_size > client_batch_size)); then
    echo "P0_EMBED_BATCH_SIZE ($qualification_batch_size) exceeds TEI_MAX_CLIENT_BATCH_SIZE ($client_batch_size)." >&2
    return 1
  fi
  if ((qualification_batch_size > concurrent_requests)); then
    echo "P0_EMBED_BATCH_SIZE ($qualification_batch_size) exceeds TEI_MAX_CONCURRENT_REQUESTS ($concurrent_requests); TEI requires one permit per batch input." >&2
    return 1
  fi
}

p0_verify() {
  p0_verify_tei_limits
  docker version >/dev/null
  "${P0_COMPOSE[@]}" version >/dev/null
  p0_verify_image "$VLLM_IMAGE"
  p0_verify_image "$TEI_IMAGE"
  p0_verify_image "$PYTHON_BASE_IMAGE"
  p0_verify_snapshot "$VLLM_MODEL" "$VLLM_MODEL_REVISION" "$VLLM_MODEL_SNAPSHOT"
  p0_verify_snapshot "$EMBED_MODEL" "$EMBED_REVISION" "$EMBED_MODEL_SNAPSHOT"
  p0_verify_snapshot "$RERANK_MODEL" "$RERANK_REVISION" "$RERANK_MODEL_SNAPSHOT"
  "${P0_COMPOSE[@]}" config --quiet
  {
    echo "verified_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "vllm_image=$VLLM_IMAGE"
    echo "tei_image=$TEI_IMAGE"
    echo "python_base_image=$PYTHON_BASE_IMAGE"
    echo "vllm_model=$VLLM_MODEL"
    echo "vllm_revision=$VLLM_MODEL_REVISION"
    echo "embed_model=$EMBED_MODEL"
    echo "embed_revision=$EMBED_REVISION"
    echo "rerank_model=$RERANK_MODEL"
    echo "rerank_revision=$RERANK_REVISION"
  } >"$P0_ARTIFACTS_DIR/verification.env"
  docker image inspect --format '{{json .RepoDigests}} {{.Id}}' \
    "$VLLM_IMAGE" "$TEI_IMAGE" "$PYTHON_BASE_IMAGE" \
    >"$P0_ARTIFACTS_DIR/verification-images.jsonl"
  echo "Pins, snapshots, Docker, and Compose configuration verified."
}

p0_capture_host() {
  local label="$1"
  local capture_dir="$P0_ARTIFACTS_DIR/host"
  mkdir -p "$capture_dir"
  {
    echo "captured_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "boot_id=$(tr -d '\n' </proc/sys/kernel/random/boot_id)"
    echo "kernel=$(uname -srmo)"
    echo "docker=$(docker version --format '{{.Server.Version}}')"
    echo "compose=$(docker compose version --short)"
  } >"$capture_dir/${label}.env"
  nvidia-smi --query-gpu=name,uuid,driver_version,memory.total,memory.used,memory.free,temperature.gpu,power.draw --format=csv,noheader,nounits \
    >"$capture_dir/${label}.gpu.csv"
  local container_ids=()
  mapfile -t container_ids < <("${P0_COMPOSE[@]}" ps -q vllm tei-embed tei-rerank)
  if ((${#container_ids[@]})); then
    docker stats --no-stream --format '{{json .}}' "${container_ids[@]}" \
      >"$capture_dir/${label}.docker-stats.jsonl"
  else
    : >"$capture_dir/${label}.docker-stats.jsonl"
  fi
  "${P0_COMPOSE[@]}" ps --format json >"$capture_dir/${label}.compose-ps.json"
}

p0_capture_logs() {
  local label="$1"
  local log_dir="$P0_ARTIFACTS_DIR/logs"
  mkdir -p "$log_dir"
  "${P0_COMPOSE[@]}" logs --no-color vllm >"$log_dir/${label}-vllm.log"
  "${P0_COMPOSE[@]}" logs --no-color tei-embed >"$log_dir/${label}-tei-embed.log"
  "${P0_COMPOSE[@]}" logs --no-color tei-rerank >"$log_dir/${label}-tei-rerank.log"
}

p0_verify_runtime_logs() {
  local label="$1"
  local vllm_log="$P0_ARTIFACTS_DIR/logs/${label}-vllm.log"
  if ! grep -Eqi 'compressed[-_ ]?tensors' "$vllm_log"; then
    echo "vLLM log does not prove compressed-tensors loading." >&2
    return 1
  fi
  if ! grep -Eqi 'marlin' "$vllm_log"; then
    echo "vLLM log does not prove use of the expected Marlin path." >&2
    return 1
  fi
  if grep -Eqi 'CUDA out of memory|OutOfMemoryError' "$P0_ARTIFACTS_DIR/logs/${label}-"*.log; then
    echo "An out-of-memory failure was found in qualification logs." >&2
    return 1
  fi
  {
    echo "verified_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "compressed_tensors_log_match=pass"
    echo "marlin_log_match=pass"
    echo "oom_scan=pass"
  } >"$P0_ARTIFACTS_DIR/runtime-log-verification.env"
}

p0_probe() {
  local suite="$1"
  local output_name="$2"
  "${P0_COMPOSE[@]}" --profile tools run --rm --no-deps probe \
    --suite "$suite" --output "/artifacts/$output_name"
}

p0_start() {
  p0_capture_host "$(p0_timestamp)-before-start"
  "${P0_COMPOSE[@]}" up -d --no-build --force-recreate vllm tei-embed tei-rerank
  echo "Pinned model services started on the internal rag-p0_model network."
}

p0_qualify() {
  local stamp
  stamp="$(p0_timestamp)"
  p0_probe full "qualification-$stamp.json"
  p0_capture_logs "qualification-$stamp"
  p0_verify_runtime_logs "qualification-$stamp"
  cp "$P0_ARTIFACTS_DIR/qualification-$stamp.json" "$P0_ARTIFACTS_DIR/qualification-latest.json"
  p0_capture_host "$stamp-after-qualification"
}

p0_restart() {
  local stamp
  stamp="$(p0_timestamp)"
  p0_capture_host "$stamp-before-restart"
  "${P0_COMPOSE[@]}" restart vllm tei-embed tei-rerank
  p0_probe smoke "restart-$stamp.json"
  p0_capture_logs "restart-$stamp"
  p0_verify_runtime_logs "restart-$stamp"
  cp "$P0_ARTIFACTS_DIR/restart-$stamp.json" "$P0_ARTIFACTS_DIR/restart-latest.json"
  p0_capture_host "$stamp-after-restart"
}

p0_pre_reboot() {
  local boot_id
  boot_id="$(tr -d '\n' </proc/sys/kernel/random/boot_id)"
  p0_probe smoke "pre-reboot-smoke.json"
  {
    echo "captured_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "boot_id=$boot_id"
    echo "vllm_image=$VLLM_IMAGE"
    echo "vllm_revision=$VLLM_MODEL_REVISION"
    echo "embed_revision=$EMBED_REVISION"
    echo "rerank_revision=$RERANK_REVISION"
  } >"$P0_ARTIFACTS_DIR/pre-reboot.env"
  p0_capture_host "pre-reboot"
  echo "Pre-reboot evidence recorded for boot $boot_id. Reboot the host now."
}

p0_post_reboot() {
  if [[ ! -f "$P0_ARTIFACTS_DIR/pre-reboot.env" ]]; then
    echo "Run pre-reboot before rebooting the host." >&2
    return 1
  fi
  local previous_boot_id current_boot_id
  previous_boot_id="$(sed -n 's/^boot_id=//p' "$P0_ARTIFACTS_DIR/pre-reboot.env")"
  current_boot_id="$(tr -d '\n' </proc/sys/kernel/random/boot_id)"
  if [[ -z "$previous_boot_id" || "$previous_boot_id" == "$current_boot_id" ]]; then
    echo "Linux boot ID did not change; a clean reboot is not proven." >&2
    return 1
  fi
  p0_verify
  "${P0_COMPOSE[@]}" up -d --no-build vllm tei-embed tei-rerank
  p0_probe smoke "post-reboot.json"
  p0_capture_logs "post-reboot"
  p0_verify_runtime_logs "post-reboot"
  p0_capture_host "post-reboot"
  {
    echo "captured_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "previous_boot_id=$previous_boot_id"
    echo "current_boot_id=$current_boot_id"
  } >"$P0_ARTIFACTS_DIR/post-reboot.env"
  echo "Clean-reboot smoke test passed on boot $current_boot_id."
}

p0_report() {
  python3 "$P0_PROJECT_ROOT/scripts/p0/render_report.py" \
    --artifacts "$P0_ARTIFACTS_DIR" \
    --output "$P0_ARTIFACTS_DIR/report.md"
  echo "Report written to $P0_ARTIFACTS_DIR/report.md"
}

p0_usage() {
  echo "Usage: $0 {verify|start|qualify|restart|pre-reboot|post-reboot|report|status|logs|down|all}" >&2
}

P0_ACTION="${1:-}"
case "$P0_ACTION" in
  verify) p0_verify ;;
  start) p0_verify; p0_start ;;
  qualify) p0_qualify ;;
  restart) p0_restart ;;
  pre-reboot) p0_pre_reboot ;;
  post-reboot) p0_post_reboot ;;
  report) p0_report ;;
  status) "${P0_COMPOSE[@]}" ps ;;
  logs) "${P0_COMPOSE[@]}" logs --tail 250 vllm tei-embed tei-rerank ;;
  down) "${P0_COMPOSE[@]}" down ;;
  all)
    p0_verify
    p0_start
    p0_qualify
    p0_restart
    p0_report
    ;;
  *) p0_usage; exit 2 ;;
esac
