#!/usr/bin/env bash
# Bring the whole stack up on demand (nothing starts at login by design):
#   1. Rancher Desktop (Docker), in the background, and wait for the engine
#   2. With LLM_PROVIDER=ollama in .env: Ollama, and the models .env names
#      (pulled if missing; a name ending in -<N>k, such as hermes3:8b-16k, is
#      made from its base model with an N-thousand-token context window)
#   3. PostgreSQL + ingestion worker + the web app, plus the Langfuse overlay
#   4. Wait until each service answers, then print the links
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

env_value() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true; }

# The ollama models .env names: LLM_MODEL, and ollama:<model> in the
# per-job and fallback settings.
ollama_models() {
  if [[ "$(env_value LLM_PROVIDER)" == "ollama" ]]; then env_value LLM_MODEL; fi
  { grep -E '^(LLM_FOR_[A-Z]+|LLM_FALLBACK)=' .env 2>/dev/null || true; } | cut -d= -f2- | tr ',' '\n' \
    | sed -n 's/^ *ollama:\(.*\)$/\1/p'
}

LOCAL_MODELS=$(ollama_models | sort -u)
if [[ -n "$LOCAL_MODELS" ]]; then
  echo "▶ Ollama (local models)"
  command -v ollama >/dev/null 2>&1 || { echo "✗ .env uses Ollama but it isn't installed: brew install ollama"; exit 1; }
  if ! curl -s -o /dev/null -m 3 http://localhost:11434/api/version; then
    brew services start ollama >/dev/null 2>&1 || (nohup ollama serve >/tmp/ollama.log 2>&1 &)
    waited=0
    until curl -s -o /dev/null -m 3 http://localhost:11434/api/version; do
      if (( waited >= 60 )); then echo "✗ Ollama didn't start (see /tmp/ollama.log)"; exit 1; fi
      sleep 2; waited=$((waited + 2))
    done
  fi
  for model in $LOCAL_MODELS; do
    printf "  %-24s" "$model"
    if ollama show "$model" >/dev/null 2>&1; then echo "✓ here"; continue; fi
    if [[ "$model" =~ ^(.+)-([0-9]+)k$ ]]; then
      base=${BASH_REMATCH[1]} context=${BASH_REMATCH[2]}
      ollama show "$base" >/dev/null 2>&1 || { echo "pulling $base…"; ollama pull "$base"; printf "  %-24s" "$model"; }
      printf 'FROM %s\nPARAMETER num_ctx %d\n' "$base" $((context * 1024)) | ollama create "$model" -f /dev/stdin >/dev/null 2>&1
      echo "✓ made from $base (${context}k context)"
    else
      echo "pulling…"; ollama pull "$model"
    fi
  done
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

# The containers reach the Mac's Ollama as host.docker.internal on Docker
# Desktop, but on Rancher Desktop that name is the VM: use Lima's name for
# the Mac instead, and remember it in .env.
if [[ -n "$LOCAL_MODELS" ]]; then
  reaches() { docker compose "${compose_env[@]}" exec -T webapp python -c \
    "import urllib.request,sys; urllib.request.urlopen(sys.argv[1] + '/api/version', timeout=3)" "$1" >/dev/null 2>&1; }
  current=$(env_value OLLAMA_BASE_URL); current=${current:-http://host.docker.internal:11434/v1}
  printf "  %-12s" "ollama"
  if reaches "${current%/v1}"; then
    echo "✓ reachable from the app"
  elif reaches http://host.lima.internal:11434; then
    if grep -q '^OLLAMA_BASE_URL=' .env; then
      sed -i '' 's|^OLLAMA_BASE_URL=.*|OLLAMA_BASE_URL=http://host.lima.internal:11434/v1|' .env
    else
      echo 'OLLAMA_BASE_URL=http://host.lima.internal:11434/v1' >> .env
    fi
    docker compose "${compose_env[@]}" up -d webapp worker >/dev/null 2>&1
    wait_for "webapp" http://localhost:18000/api/config 180 >/dev/null
    echo "✓ reachable as host.lima.internal (saved in .env)"
  else
    echo "✗ the app can't reach Ollama at $current"
  fi
fi
if (( WITH_LANGFUSE )); then
  wait_for "langfuse" http://localhost:3000/api/public/health 300
fi

echo
echo "✅ Ready — open http://localhost:18000 (its Home page lists every service)."
