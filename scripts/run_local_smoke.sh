#!/usr/bin/env bash
set -euo pipefail

sdk_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="${sdk_dir}/compose.smoke.yml"
project_name="kanopy-sdk-smoke"

cleanup() {
  docker compose -p "${project_name}" -f "${compose_file}" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "Building and starting isolated Kanopy API, Postgres, Redis, and MinIO..."
docker compose -p "${project_name}" -f "${compose_file}" up -d --build --wait backend

echo "Creating a one-hour API key in the disposable database..."
seed_json="$(docker compose -p "${project_name}" -f "${compose_file}" exec -T backend python - < "${sdk_dir}/scripts/seed_local_smoke.py" | tail -n 1)"
api_key="$(printf '%s' "${seed_json}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')"

echo "Installing and running the SDK in a clean Python container..."
docker run --rm --network host \
  --env SMOKE_API_KEY="${api_key}" \
  --env SMOKE_BASE_URL="http://localhost:18000/api/v1" \
  --volume "${sdk_dir}:/sdk:ro" \
  --workdir /sdk \
  python:3.11-slim \
  sh -c 'cp -R /sdk /tmp/kanopy-sdk && python -m pip install --quiet --disable-pip-version-check --root-user-action=ignore /tmp/kanopy-sdk && python /sdk/scripts/local_smoke.py'

echo "Tearing down disposable containers and volumes..."
