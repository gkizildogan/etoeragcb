#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${ENV_FILE:-${project_root}/deploy/.env}"
scanner="docker.io/anchore/grype:v0.112.0@sha256:391bfda62888fb4e98ff5c4c81598f7431a3c1eac3f8519d69d1ff00df247c1d"
artifact_dir="${project_root}/artifacts/p11/security"
mkdir -p "${artifact_dir}"

docker compose \
  --env-file "${env_file}" \
  -f "${project_root}/deploy/compose.yml" \
  build backend streamlit

images=(rag-chatbot-backend:0.1.0 rag-chatbot-streamlit:0.1.0)
if [[ "${SCAN_ALL_LOCAL_IMAGES:-0}" == 1 ]]; then
  mapfile -t images < <(
    docker compose \
      --env-file "${env_file}" \
      -f "${project_root}/deploy/compose.yml" \
      --profile "*" \
      config --images |
      sort -u
  )
fi

for image in "${images[@]}"; do
  safe_name="${image//[^A-Za-z0-9_.-]/_}"
  docker run --rm \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --tmpfs /tmp:size=4g,mode=1777 \
    -e GRYPE_DB_CACHE_DIR=/grype-cache \
    -v /var/run/docker.sock:/var/run/docker.sock:ro \
    -v rag_chatbot_grype_cache:/grype-cache \
    -v "${project_root}/deploy/security/grype.yaml:/grype.yaml:ro" \
    "${scanner}" \
    "${image}" \
    --config /grype.yaml \
    --only-fixed \
    --fail-on high \
    --output json >"${artifact_dir}/${safe_name}.json"
  echo "PASS ${image}"
done
