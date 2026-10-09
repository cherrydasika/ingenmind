#!/usr/bin/env bash
# Switch EC2 to a published release in one step. Run as root from the
# active release (normally /opt/rag-systems/current):
#
#   sudo bash /opt/rag-systems/current/scripts/aws/deploy.sh COMMIT_SHA
#
# 1. fetch and verify s3://$ARTIFACT_BUCKET/releases/COMMIT_SHA (fetch-release.sh)
# 2. prepare it: .env from SSM (fetch-secrets.sh), runtime data copied from the active release
# 3. stop the active release (refuses while an ingestion job is queued/running)
# 4. remove containers left by releases started under their own project names
# 5. re-run install-docker.sh if it changed
# 6. start the new release (stack.sh start); if that fails, start the previous one again
# 7. point /opt/rag-systems/current at the new release
set -euo pipefail

[[ $(id -u) -eq 0 ]] || { echo "Run as root on the EC2 instance" >&2; exit 1; }
[[ $# -eq 1 && $1 =~ ^[a-f0-9]{40}$ ]] || { echo "Usage (as root): deploy.sh COMMIT_SHA (40 hex characters)" >&2; exit 1; }

root=/opt/rag-systems
releases=$root/releases
link=$root/current
project=rag-systems
compose_files=(-f docker-compose.yml -f docker-compose.langfuse.yml -f docker-compose.aws.yml)
script_dir=$(realpath "$(dirname "$0")")
new=$releases/$1

fail() { echo "✗ $*" >&2; exit 1; }
step() { echo; echo "▶ $*"; }

# The active release: the symlink, or (before the first deploy.sh run) the
# release whose containers exist, or the release this script was run from.
if [[ -L $link ]]; then
  current=$(realpath "$link")
else
  current=$(docker ps -a --filter label=com.docker.compose.service=webapp \
    --format '{{.Label "com.docker.compose.project.working_dir"}}' | head -1)
  current=${current:-$(realpath "$script_dir/../..")}
fi
[[ -d $current && $current == "$releases"/* ]] || fail "cannot determine the active release (got '$current')"
[[ $new != "$current" ]] || fail "$1 is already the active release"
echo "active release: $current"
echo "new release:    $new"

step "Fetch and verify"
bucket=${ARTIFACT_BUCKET:-rag-systems-artifacts-$(aws sts get-caller-identity --query Account --output text)-eu-west-2}
if [[ -d $new ]]; then
  echo "already fetched"
else
  bash "$current/scripts/aws/fetch-release.sh" "$bucket" "$1"
fi
[[ -f $new/scripts/aws/stack.sh ]] || fail "$new has no scripts/aws/stack.sh; it predates this deploy flow"

step "Prepare"
bash "$new/scripts/aws/fetch-secrets.sh" "$new"
install -m 0644 "$current/data/urls.json" "$new/data/urls.json"
if [[ -d $current/data/evaluations ]]; then
  rm -rf "$new/data/evaluations"
  cp -a "$current/data/evaluations" "$new/data/"
fi
echo "copied data/urls.json ($(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))))" "$new/data/urls.json") URLs)"

step "Stop the active release"
if [[ -f $current/scripts/aws/stack.sh ]]; then
  bash "$current/scripts/aws/stack.sh" stop
else
  (cd "$current" && docker compose "${compose_files[@]}" stop -t 60)
fi

step "Remove containers from per-release project names"
docker ps -a --format '{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.project.working_dir"}}' \
  | sort -u | while IFS='|' read -r name dir; do
      [[ -n $name && $name != "$project" && $dir == "$releases"/* && -d $dir ]] || continue
      echo "removing project $name"
      (cd "$dir" && docker compose -p "$name" "${compose_files[@]}" down)
    done

if ! cmp -s "$current/scripts/aws/install-docker.sh" "$new/scripts/aws/install-docker.sh"; then
  step "install-docker.sh changed; re-running it"
  bash "$new/scripts/aws/install-docker.sh"
fi

step "Start the new release"
if ! bash "$new/scripts/aws/stack.sh" start; then
  echo "✗ new release failed to start; restoring $current" >&2
  bash "$new/scripts/aws/stack.sh" stop --force || true
  if [[ -f $current/scripts/aws/stack.sh ]]; then
    bash "$current/scripts/aws/stack.sh" start
  else
    (cd "$current" && docker compose "${compose_files[@]}" up -d)
  fi
  fail "deploy of $1 failed; $current is running again"
fi

ln -sfn "$new" "$link"
echo
echo "✓ deployed $1"
echo "  $link -> $new  (previous: $current)"
