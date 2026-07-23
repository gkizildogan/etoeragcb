#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${ENV_FILE:-${project_root}/deploy/.env}"
destination="$(python3 "${project_root}/scripts/env_value.py" "${env_file}" BACKUP_DESTINATION)"
folder_id="${destination#gdrive://folder/}"
if [[ -z "${folder_id}" || "${folder_id}" == "${destination}" ]]; then
  echo "BACKUP_DESTINATION must use gdrive://folder/<folder-id>." >&2
  exit 2
fi

mkdir -p "${project_root}/deploy/state/rclone"
chmod 0700 "${project_root}/deploy/state/rclone"
export BACKUP_UID
BACKUP_UID="$(id -u)"
export BACKUP_GID
BACKUP_GID="$(id -g)"

echo "Before continuing, set Google Drive folder ${folder_id} to General access: Restricted."
echo "Create a remote named 'gdrive', choose Google Drive, full drive scope, and set"
echo "root_folder_id to ${folder_id}. Complete OAuth in your browser when prompted."
read -r -p "Type RESTRICTED to confirm the folder is no longer public: " confirmation
if [[ "${confirmation}" != RESTRICTED ]]; then
  echo "Configuration cancelled." >&2
  exit 3
fi

docker compose \
  --env-file "${env_file}" \
  -f "${project_root}/deploy/compose.yml" \
  --profile operations \
  run --rm --no-deps backup-rclone-configure config

config_file="${project_root}/deploy/state/rclone/rclone.conf"
if [[ ! -s "${config_file}" ]]; then
  echo "rclone did not create ${config_file}." >&2
  exit 4
fi
chmod 0600 "${config_file}"
docker compose \
  --env-file "${env_file}" \
  -f "${project_root}/deploy/compose.yml" \
  --profile operations \
  run --rm --no-deps backup-rclone lsd gdrive:
echo "Authenticated Google Drive backup remote is configured."
