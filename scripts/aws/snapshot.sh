#!/usr/bin/env bash
# Snapshot EC2's data volume (rag-systems-data: PostgreSQL with the knowledge
# base, Langfuse) from your workstation. Take one before resetting the
# knowledge system on EC2: a reset cannot be undone otherwise.
#
#   scripts/aws/snapshot.sh create "before reset for flights"   # waits until it completes
#   scripts/aws/snapshot.sh list
#
# A snapshot of a running instance is crash-consistent: PostgreSQL recovers
# from its write-ahead log on restore, as after a power cut. For a quiet copy,
# stop the stack first (scripts/aws/session.sh stop). Restoring is a new
# volume from the snapshot, attached in place of the old one (see
# infra/README.md, "Backup"). Snapshots bill for the data they hold; delete
# old ones with `aws ec2 delete-snapshot`.
set -euo pipefail

export AWS_PROFILE=${AWS_PROFILE:-personal}
export AWS_REGION=${AWS_REGION:-eu-west-2}
volume_name=rag-systems-data

fail() { echo "✗ $*" >&2; exit 1; }

volume_id() {
  local id
  id=$(aws ec2 describe-volumes --filters Name=tag:Name,Values="$volume_name" \
    --query 'Volumes[0].VolumeId' --output text)
  [[ $id == vol-* ]] || fail "no volume tagged Name=$volume_name"
  echo "$id"
}

case "${1:-}" in
  create)
    note=${2:-manual}
    volume=$(volume_id)
    snapshot=$(aws ec2 create-snapshot --volume-id "$volume" \
      --description "rag-systems data: $note" \
      --tag-specifications "ResourceType=snapshot,Tags=[{Key=Name,Value=$volume_name},{Key=Note,Value=$note}]" \
      --query SnapshotId --output text)
    echo "▶ snapshot $snapshot of $volume started; waiting until it completes (a few minutes)…"
    aws ec2 wait snapshot-completed --snapshot-ids "$snapshot"
    echo "✓ $snapshot completed"
    ;;
  list)
    aws ec2 describe-snapshots --owner-ids self --filters Name=tag:Name,Values="$volume_name" \
      --query 'reverse(sort_by(Snapshots, &StartTime))[].[SnapshotId, StartTime, State, VolumeSize, Description]' \
      --output table
    ;;
  *)
    echo "Usage: scripts/aws/snapshot.sh create [note] | list" >&2
    exit 2
    ;;
esac
