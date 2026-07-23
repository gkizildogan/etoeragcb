#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
template="${project_root}/deploy/monitoring/alertmanager.example.yml"
config_file="${ALERTMANAGER_CONFIG_FILE:-${project_root}/deploy/state/alertmanager/alertmanager.yml}"
state_dir="$(dirname "${config_file}")"
secret_file="${SMTP_PASSWORD_FILE:-${project_root}/deploy/secrets/smtp_password}"
email="${ALERT_EMAIL:-}"

if [[ -z "${email}" ]]; then
  read -r -p "Gmail sender/recipient address: " email
fi
if [[ "${email}" != *@*.* || "${email}" == *[[:space:]]* ]]; then
  echo "Enter a valid email address." >&2
  exit 2
fi

read -r -s -p "Google 16-character app password (not your account password): " password
echo
password="${password// /}"
if [[ ! "${password}" =~ ^[A-Za-z0-9]{16}$ ]]; then
  unset password
  echo "Expected a 16-character Google app password." >&2
  exit 2
fi

mkdir -p "${state_dir}" "$(dirname "${secret_file}")"
chmod 0750 "${state_dir}" "$(dirname "${secret_file}")"
escaped_email="${email//\\/\\\\}"
escaped_email="${escaped_email//&/\\&}"
escaped_email="${escaped_email//|/\\|}"
config_temp="$(mktemp "${state_dir}/alertmanager.yml.XXXXXX")"
secret_temp="$(mktemp "${project_root}/deploy/secrets/smtp_password.XXXXXX")"
cleanup() {
  unset password
  rm -f -- "${config_temp}" "${secret_temp}"
}
trap cleanup EXIT

sed "s|alerts@example.com|${escaped_email}|g" "${template}" >"${config_temp}"
printf '%s\n' "${password}" >"${secret_temp}"
unset password
chmod 0640 "${config_temp}" "${secret_temp}"
mv -f -- "${config_temp}" "${config_file}"
mv -f -- "${secret_temp}" "${secret_file}"
echo "SMTP alert configuration created for ${email}."
