#!/usr/bin/env bash
set -Eeuo pipefail
umask 007

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${ENV_FILE:-${project_root}/deploy/.env}"
main_compose_file="${project_root}/deploy/compose.yml"
restore_compose_file="${project_root}/deploy/restore-compose.yml"
state_root="${project_root}/deploy/state"
drill_id="$(date -u +%Y%m%dT%H%M%SZ)"
project_name="rag-chatbot-restore-${drill_id,,}"
mkdir -p "${state_root}/staging"
restore_output="$(mktemp -d "${state_root}/staging/restore-${drill_id}.XXXXXX")"
chmod 2770 "${restore_output}"

export BACKUP_STAGING_PATH="${restore_output}"
export RESTORE_STAGING_PATH="${restore_output}/backup-stage"
export BACKUP_UID
BACKUP_UID="$(id -u)"
export BACKUP_GID
BACKUP_GID="$(id -g)"

main_compose=(
  docker compose
  --env-file "${env_file}"
  -f "${main_compose_file}"
  --profile operations
)
restore_compose=(
  docker compose
  --project-name "${project_name}"
  --env-file "${env_file}"
  -f "${restore_compose_file}"
)

cleanup() {
  local exit_code=$?
  "${restore_compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  if [[ -n "${restore_output:-}" && "${restore_output}" == "${state_root}/staging/"* ]]; then
    chmod -R u+w "${restore_output}" 2>/dev/null || true
    rm -rf -- "${restore_output}"
  fi
  exit "${exit_code}"
}
trap cleanup EXIT INT TERM

if [[ "${ALLOW_EMPTY_RESTORE:-0}" == 1 ]]; then
  default_restore_from_remote=0
else
  default_restore_from_remote=1
fi
restore_from_remote="${RESTORE_FROM_REMOTE:-${default_restore_from_remote}}"
if [[ "${restore_from_remote}" == 1 ]]; then
  rclone_config="${state_root}/rclone/rclone.conf"
  if [[ ! -s "${rclone_config}" ]]; then
    echo "Strict restore requires authenticated off-machine recovery; configure rclone first." >&2
    exit 2
  fi
  export BACKUP_REPOSITORY_PATH="${restore_output}/remote-repository"
  mkdir -p "${BACKUP_REPOSITORY_PATH}"
  remote="${BACKUP_RCLONE_REMOTE:-gdrive:rag-chatbot-restic}"
  "${main_compose[@]}" run --rm --no-deps backup-rclone-restore \
    copy "${remote}" /repository --checkers 4 --transfers 2 --metadata
  backup_source=off-machine
elif [[ "${restore_from_remote}" == 0 ]]; then
  export BACKUP_REPOSITORY_PATH="${state_root}/restic"
  if [[ ! -f "${BACKUP_REPOSITORY_PATH}/config" ]]; then
    echo "No local encrypted restic repository exists; run deploy/backup.sh first." >&2
    exit 2
  fi
  backup_source=local
else
  echo "RESTORE_FROM_REMOTE must be 0 or 1." >&2
  exit 2
fi

"${main_compose[@]}" run --rm --no-deps backup-restic check --read-data
"${main_compose[@]}" run --rm --no-deps backup-restic-restore \
  restore latest --host rag-chatbot --target /restore-output --verify
if [[ ! -f "${RESTORE_STAGING_PATH}/manifest.json" ]]; then
  echo "Restic restore did not produce the expected backup-stage tree." >&2
  exit 3
fi

"${restore_compose[@]}" up -d --build --wait postgres qdrant
"${restore_compose[@]}" run --rm storage-init
"${restore_compose[@]}" exec -T postgres sh -eu -c \
  'test "$(stat -c %s /restore-stage/postgres.dump)" -gt 0;
   sha256sum /restore-stage/postgres.dump'
"${restore_compose[@]}" exec -T postgres sh -eu -c \
  'export PGPASSWORD="$(cat /run/secrets/postgres_password)";
   cp /restore-stage/postgres.dump /tmp/postgres.dump;
   test "$(stat -c %s /tmp/postgres.dump)" -gt 0;
   pg_restore --host=postgres --username=rag --dbname=rag --exit-on-error --no-owner --no-privileges /tmp/postgres.dump'

"${restore_compose[@]}" run --rm --no-deps restore-tool \
  python -m app.operations.backup restore-assets \
  --stage /restore-stage \
  --documents-root /restore-documents \
  --collection rag_chunks_v1
verify_args=(
  python -m app.operations.backup verify-restore \
  --stage /restore-stage \
  --documents-root /restore-documents \
  --database-url-file /run/secrets/database_url \
  --signing-secret-file /run/secrets/signing_secret \
  --collection rag_chunks_v1 \
  --report /restore-stage/restore-report.json \
  --backup-source "${backup_source}"
)
if [[ "${ALLOW_EMPTY_RESTORE:-0}" == 1 ]]; then
  verify_args+=(--allow-empty)
fi
"${restore_compose[@]}" run --rm --no-deps restore-tool "${verify_args[@]}"

mkdir -p "${project_root}/artifacts/p11"
cp "${RESTORE_STAGING_PATH}/restore-report.json" \
  "${project_root}/artifacts/p11/restore-${drill_id}.json"
if [[ "${ALLOW_EMPTY_RESTORE:-0}" == 1 ]]; then
  echo "Clean restore mechanics passed with empty application data; this is not the full P11 gate."
fi
echo "Restore report: artifacts/p11/restore-${drill_id}.json"
