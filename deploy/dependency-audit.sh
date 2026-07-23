#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
artifact_dir="${project_root}/artifacts/p11/security"
audit_version="2.10.1"
audit_python="${AUDIT_PYTHON:-3.13}"
audit_cache="${UV_CACHE_DIR:-/tmp/etoeragcb-pip-audit-cache}"
python_dir="${UV_PYTHON_INSTALL_DIR:-/tmp/etoeragcb-uv-python}"
mkdir -p "${artifact_dir}"

audit_lock() {
  local component="$1"
  UV_CACHE_DIR="${audit_cache}" \
    UV_PYTHON_INSTALL_DIR="${python_dir}" \
    uv tool run --python "${audit_python}" --from "pip-audit==${audit_version}" pip-audit \
      --require-hashes \
      --strict \
      -r "${project_root}/${component}/requirements.lock" \
      --format json \
      --output "${artifact_dir}/pip-audit-${component}.json"
}

audit_lock backend
audit_lock streamlit_app
echo "Dependency audits passed; evidence is under artifacts/p11/security/."
