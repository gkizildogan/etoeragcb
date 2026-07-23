#!/usr/bin/env bash
set -Eeuo pipefail
umask 007

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${ENV_FILE:-${project_root}/deploy/.env}"
compose_file="${project_root}/deploy/compose.yml"
state_root="${project_root}/deploy/state"
backup_id="$(date -u +%Y%m%dT%H%M%SZ)"
destination="$(python3 "${project_root}/scripts/env_value.py" "${env_file}" BACKUP_DESTINATION)"
retention="$(python3 "${project_root}/scripts/env_value.py" "${env_file}" BACKUP_RETENTION --default 30)"
secrets_gid="$(
  python3 "${project_root}/scripts/env_value.py" "${env_file}" SECRETS_GID --default 1000
)"

if [[ "${destination}" != gdrive://folder/* ]]; then
  echo "P11 currently automates the configured gdrive://folder/<id> destination." >&2
  exit 2
fi
if [[ ! "${retention}" =~ ^[0-9]+$ ]] || (( retention < 2 )); then
  echo "BACKUP_RETENTION must be an integer of at least 2." >&2
  exit 2
fi
if [[ "$(id -g)" != "${secrets_gid}" ]]; then
  echo "The operator primary GID must match SECRETS_GID for protected staging access." >&2
  exit 2
fi

mkdir -p \
  "${state_root}/backup" \
  "${state_root}/restic" \
  "${state_root}/rclone" \
  "${state_root}/staging"
chmod 0750 "${state_root}" "${state_root}/backup" "${state_root}/staging"
chmod 0700 "${state_root}/restic" "${state_root}/rclone"

exec 9>"${state_root}/backup.lock"
if ! flock -n 9; then
  echo "Another backup is already running." >&2
  exit 3
fi

stage="$(mktemp -d "${state_root}/staging/${backup_id}.XXXXXX")"
chmod 2770 "${stage}"
export BACKUP_STAGING_PATH="${stage}"
export BACKUP_REPOSITORY_PATH="${state_root}/restic"
export BACKUP_UID
BACKUP_UID="$(id -u)"
export BACKUP_GID
BACKUP_GID="$(id -g)"

compose=(
  docker compose
  --env-file "${env_file}"
  -f "${compose_file}"
  --profile operations
)
quiesced_services=()

cleanup() {
  local exit_code=$?
  if (( ${#quiesced_services[@]} > 0 )); then
    "${compose[@]}" start "${quiesced_services[@]}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${stage:-}" && "${stage}" == "${state_root}/staging/"* ]]; then
    chmod -R u+w "${stage}" 2>/dev/null || true
    rm -rf -- "${stage}"
  fi
  exit "${exit_code}"
}
trap cleanup EXIT INT TERM

for service in backend worker; do
  if "${compose[@]}" ps --status running --services | rg -qx "${service}"; then
    quiesced_services+=("${service}")
  fi
done
if (( ${#quiesced_services[@]} > 0 )); then
  "${compose[@]}" stop --timeout 30 "${quiesced_services[@]}"
fi

"${compose[@]}" run --rm --no-deps backup-postgres
"${compose[@]}" run --rm --no-deps backup-prepare

if (( ${#quiesced_services[@]} > 0 )); then
  "${compose[@]}" start "${quiesced_services[@]}"
  quiesced_services=()
fi

if [[ ! -f "${state_root}/restic/config" ]]; then
  "${compose[@]}" run --rm --no-deps backup-restic init
fi
"${compose[@]}" run --rm --no-deps backup-restic \
  backup /backup-stage --host rag-chatbot --tag nightly --tag "${backup_id}"
"${compose[@]}" run --rm --no-deps backup-restic check --read-data-subset=5%
"${compose[@]}" run --rm --no-deps backup-restic \
  forget --host rag-chatbot --keep-last "${retention}" --prune

snapshot_json="$("${compose[@]}" run --rm --no-deps backup-restic \
  snapshots --host rag-chatbot --latest 1 --json)"
snapshot_id="$(
  python3 "${project_root}/scripts/restic_snapshot_id.py" <<<"${snapshot_json}"
)"

status_args=(
  --backup-id "${backup_id}"
  --snapshot-id "${snapshot_id}"
  --destination "${destination}"
)
if [[ "${BACKUP_SKIP_REMOTE:-0}" == 1 ]]; then
  python3 "${project_root}/scripts/backup_status.py" \
    --path "${state_root}/backup/last-attempt.json" \
    "${status_args[@]}"
  echo "Local encrypted backup ${snapshot_id} passed; off-machine upload was skipped."
  exit 0
fi

rclone_config="${state_root}/rclone/rclone.conf"
if [[ ! -s "${rclone_config}" ]]; then
  echo "Authenticated rclone config is missing; run deploy/configure-gdrive-backup.sh." >&2
  exit 4
fi
config_mode="$(stat -c '%a' "${rclone_config}")"
if (( 10#${config_mode} > 640 )); then
  echo "Refusing overly permissive ${rclone_config}; use mode 0600." >&2
  exit 4
fi

remote="${BACKUP_RCLONE_REMOTE:-gdrive:rag-chatbot-restic}"
"${compose[@]}" run --rm --no-deps backup-rclone \
  sync /repository "${remote}" --checkers 4 --transfers 2 --metadata
"${compose[@]}" run --rm --no-deps backup-rclone \
  check /repository "${remote}" --one-way

python3 "${project_root}/scripts/backup_status.py" \
  --path "${state_root}/backup/last-success.json" \
  --off-machine-uploaded \
  "${status_args[@]}"
cp --preserve=mode,timestamps \
  "${state_root}/backup/last-success.json" \
  "${state_root}/backup/last-attempt.json"
echo "Encrypted off-machine backup ${snapshot_id} completed and verified."
