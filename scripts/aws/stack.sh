#!/usr/bin/env bash
# Start, stop, or inspect the Compose stack on EC2. Run as root from a
# verified release; the release directory is the one containing this script.
#
#   sudo bash scripts/aws/stack.sh status
#   sudo bash scripts/aws/stack.sh start   # refuses unless the EBS data volume is mounted
#   sudo bash scripts/aws/stack.sh stop    # refuses while an ingestion job is queued/running
#   sudo bash scripts/aws/stack.sh stop --force
set -euo pipefail

[[ $(id -u) -eq 0 ]] || { echo "Run as root on the EC2 instance" >&2; exit 1; }
release_dir=$(realpath "$(dirname "$0")/../..")
cd "$release_dir"

mount_dir=/srv/rag-systems
data_dirs=(pgvector langfuse/postgres langfuse/redis langfuse/clickhouse langfuse/clickhouse-logs langfuse/minio)
# One fixed project name for every release, so starting a new release replaces
# the previous release's containers instead of creating a second stack.
project=rag-systems
compose=(docker compose -p "$project" -f docker-compose.yml -f docker-compose.langfuse.yml -f docker-compose.aws.yml)

# pgvector listens on the host's private VPC address (for the agentcore-kb
# runtime), never on a public interface. IMDSv2 is required on this instance.
imds_token=$(curl -fsS -m 2 -X PUT http://169.254.169.254/latest/api/token \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null || true)
PGVECTOR_BIND_IP=$(curl -fsS -m 2 -H "X-aws-ec2-metadata-token: $imds_token" \
  http://169.254.169.254/latest/meta-data/local-ipv4 2>/dev/null || true)
export PGVECTOR_BIND_IP=${PGVECTOR_BIND_IP:-127.0.0.1}

fail() { echo "✗ $*" >&2; exit 1; }

check_mount() {
  mountpoint -q "$mount_dir" || fail "$mount_dir is not mounted; run prepare-host.sh or check /etc/fstab"
  local fstab_uuid mounted_uuid
  fstab_uuid=$(awk -v dir="$mount_dir" '$2 == dir && $1 ~ /^UUID=/ {sub(/^UUID=/, "", $1); print $1}' /etc/fstab)
  mounted_uuid=$(findmnt -no UUID "$mount_dir")
  [[ -n $fstab_uuid && $fstab_uuid == "$mounted_uuid" ]] \
    || fail "$mount_dir is mounted from UUID '$mounted_uuid', expected '$fstab_uuid' from /etc/fstab"
  for dir in "${data_dirs[@]}"; do
    [[ -d $mount_dir/$dir ]] || fail "missing $mount_dir/$dir; was prepare-host.sh run on this volume?"
  done
  echo "✓ $mount_dir mounted from UUID $mounted_uuid"
}

check_tools() {
  [[ -f .env ]] || fail "$release_dir/.env missing; run fetch-secrets.sh"
  [[ -f data/urls.json ]] || fail "$release_dir/data/urls.json missing"
  local buildx
  buildx=$(docker buildx version | awk '{print $2}' | sed 's/^v//')
  [[ $(printf '%s\n' 0.17.0 "$buildx" | sort -V | head -1) == 0.17.0 ]] \
    || fail "Docker Buildx $buildx is older than 0.17; re-run install-docker.sh"
  "${compose[@]}" config --quiet
  echo "✓ .env, urls.json, Buildx $buildx, Compose config"
}

active_jobs() {
  "${compose[@]}" exec -T pgvector psql -U rag -d rag -tAc \
    "SELECT count(*) FROM ingestion_jobs WHERE status IN ('queued', 'running')"
}

wait_for() {  # wait_for <name> <url> <timeout-seconds>
  local waited=0
  until curl -fs -o /dev/null -m 3 "$2"; do
    (( waited >= $3 )) && fail "$1 not answering after $3s ($2)"
    sleep 3; waited=$((waited + 3))
  done
  echo "✓ $1 answering"
}

status() {
  if mountpoint -q "$mount_dir"; then
    df -h "$mount_dir" | tail -1
  else
    echo "✗ $mount_dir not mounted"
  fi
  "${compose[@]}" ps -a --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
}

case "${1:-}" in
  start)
    check_mount
    check_tools
    # --build: an image whose requirements changed is rebuilt (cached layers
    # otherwise); a plain `up` keeps the old image and its old packages.
    "${compose[@]}" up -d --build
    wait_for "web app" http://127.0.0.1:18000/ 180
    wait_for "Langfuse" http://127.0.0.1:3000/api/public/health 300
    status
    ;;
  stop)
    if [[ ${2:-} != --force ]] && "${compose[@]}" ps --status running --services | grep -qx pgvector; then
      jobs=$(active_jobs)
      [[ $jobs == 0 ]] || fail "$jobs ingestion job(s) queued or running; wait, or use: stop --force"
    fi
    "${compose[@]}" stop -t 60
    sync
    status
    ;;
  status)
    status
    ;;
  *)
    echo "Usage: sudo bash scripts/aws/stack.sh start|stop [--force]|status" >&2
    exit 2
    ;;
esac
