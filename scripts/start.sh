#!/usr/bin/env bash
# Bring the whole stack up on demand (nothing starts at login by design):
#   1. Rancher Desktop (Docker), in the background, and wait for the engine
#   2. PostgreSQL + ingestion worker + the web app, plus the Langfuse overlay
#   3. Wait until each service answers, then print the links
#
# Usage: scripts/start.sh [--no-langfuse]
set -euo pipefail

cd "$(dirname "$0")/.."

WITH_LANGFUSE=1
[[ "${1:-}" == "--no-langfuse" ]] && WITH_LANGFUSE=0

wait_for() {  # wait_for <name> <url> <timeout-seconds>
  local name=$1 url=$2 timeout=$3 waited=0
  printf "  %-12s" "$name"
  until curl -s -o /dev/null -m 3 "$url"; do
    if (( waited >= timeout )); then echo "✗ not answering after ${timeout}s ($url)"; return 1; fi
    sleep 3; waited=$((waited + 3))
  done
  echo "✓ up"
}

echo "▶ Docker (Rancher Desktop)"
if ! docker info >/dev/null 2>&1; then
  open -ga "Rancher Desktop"
  printf "  starting"
  waited=0
  until docker info >/dev/null 2>&1; do
    if (( waited >= 240 )); then echo; echo "✗ Docker didn't start within 4 minutes — open Rancher Desktop and check it."; exit 1; fi
    printf "."; sleep 5; waited=$((waited + 5))
  done
  echo " ✓"
else
  echo "  already running ✓"
fi

echo "▶ Containers"
compose_env=(--env-file .env)
[[ -f .env.aws ]] && compose_env+=(--env-file .env.aws)
if (( WITH_LANGFUSE )); then
  docker compose "${compose_env[@]}" -f docker-compose.yml -f docker-compose.langfuse.yml up -d
else
  docker compose "${compose_env[@]}" up -d
fi

echo "▶ Waiting for services"
docker compose "${compose_env[@]}" exec -T pgvector pg_isready -U rag -d rag
docker compose "${compose_env[@]}" exec -T worker python -c "import ingestion_worker"
wait_for "webapp" http://localhost:18000/api/config 180
if (( WITH_LANGFUSE )); then
  wait_for "langfuse" http://localhost:3000/api/public/health 300
fi

echo
echo "✅ Ready — open http://localhost:18000 (its Home page lists every service)."
