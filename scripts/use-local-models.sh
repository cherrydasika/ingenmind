#!/usr/bin/env bash
# Run IngenMind on local models with Ollama: no API key, nothing leaves the
# machine except the web pages the agents search and scrape.
#
#   scripts/use-local-models.sh            switch .env to local models, then start
#   scripts/use-local-models.sh --undo     go back to the .env from before the switch
#
# The agents run on Hermes 3 (8B). Guardrails and the answer check run on
# qwen3 (8B), thinking a little for the answer check (see below): as the
# guardrail it got 1 of 54 decisions wrong on the probe
# (python -m guardrail_probe) and 0 of 14 on a planning domain, where Hermes 3
# 8B got 7 and 4 wrong, refusing questions in scope. Both fit in memory at
# once (about 14 GB), so no step waits for a model to load. Larger checkers
# did no better here: qwen3:14b got 3 and 1 wrong, qwen3:30b 3 and 0 but
# needs about 19 GB and, next to Docker, made Ollama swap models every step.
# Change the models with AGENT_MODEL / CHECK_MODEL, e.g.
#   CHECK_MODEL=hermes3:8b-16k scripts/use-local-models.sh   (Hermes for everything)
# A name ending in -<N>k is made from its base model with an N-thousand-token
# context window by scripts/start.sh (Ollama's own default is smaller).
set -euo pipefail

cd "$(dirname "$0")/.."

AGENT_MODEL=${AGENT_MODEL:-hermes3:8b-16k}
CHECK_MODEL=${CHECK_MODEL:-qwen3:8b-16k}
BACKUP=.env.before-local-models

if [[ "${1:-}" == "--undo" ]]; then
  [[ -f $BACKUP ]] || { echo "✗ No $BACKUP to go back to."; exit 1; }
  mv "$BACKUP" .env
  echo "✓ .env is back as it was. Restart the app: scripts/start.sh"
  exit 0
fi

[[ -f .env ]] || cp .env.example .env
[[ -f $BACKUP ]] || cp .env "$BACKUP"

# set KEY VALUE: replace the active line, or add one. Earlier values of the
# keys this script owns are commented out, so they're easy to find again.
set_env() {
  if grep -qE "^$1=" .env; then
    sed -i '' -E "s|^$1=.*|$1=$2|" .env
  else
    printf '%s=%s\n' "$1" "$2" >> .env
  fi
}

grep -q '^# ---- Local models' .env || printf '\n# ---- Local models (scripts/use-local-models.sh) ----\n' >> .env
set_env LLM_PROVIDER ollama
set_env LLM_MODEL "$AGENT_MODEL"
set_env LLM_FOR_GUARDRAILS "ollama:$CHECK_MODEL"
set_env LLM_FOR_EVALUATORS "ollama:$CHECK_MODEL"
# Thinking off it passed answers with a planted wrong number (9 of 30 wrong on
# python -m evaluator_probe); thinking a little, 3 of 30, all too strict.
set_env OLLAMA_THINK_FOR_EVALUATORS low
# Setting up a domain (research plan, knowledge areas, sources, page labels):
# Hermes classed every area as static on the live check (test_blueprint_live);
# qwen3 thinking found the live ones and the regulator. Slower, but it runs once.
set_env LLM_FOR_SETUP "ollama:$CHECK_MODEL"
set_env OLLAMA_THINK_FOR_SETUP low
set_env EMBEDDING_PROVIDER local
grep -q '^OLLAMA_BASE_URL=' .env || set_env OLLAMA_BASE_URL http://host.docker.internal:11434/v1
# Lets an admin switch to a hosted model on their own API key later (Overview →
# Chat model): the key is stored encrypted with this (a Fernet key).
grep -qE '^INTEGRATIONS_KEY=.+' .env || set_env INTEGRATIONS_KEY "$(openssl rand -base64 32 | tr '+/' '-_')"

echo "✓ .env uses local models: agents on $AGENT_MODEL, checks on $CHECK_MODEL"
echo "  (the old .env is in $BACKUP; scripts/use-local-models.sh --undo puts it back)"
if ! grep -qE '^TAVILY_API_KEY=.+' .env; then
  echo "  Web search for research needs TAVILY_API_KEY in .env (free tier: tavily.com); scraping works without it."
fi
echo
exec scripts/start.sh "$@"
