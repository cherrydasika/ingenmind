#!/usr/bin/env bash
# Stop the stack. Data is kept (data/ and Docker volumes); scripts/start.sh
# brings everything back.
#
# Usage: scripts/stop.sh [--all]   (--all also quits Rancher Desktop)
set -euo pipefail

cd "$(dirname "$0")/.."

if docker info >/dev/null 2>&1; then
  echo "▶ Stopping containers"
  docker compose -f docker-compose.yml -f docker-compose.langfuse.yml stop
fi

if [[ "${1:-}" == "--all" ]]; then
  echo "▶ Quitting Rancher Desktop"
  ~/.rd/bin/rdctl shutdown >/dev/null 2>&1 || osascript -e 'quit app "Rancher Desktop"'
fi

echo "✅ Stopped."
