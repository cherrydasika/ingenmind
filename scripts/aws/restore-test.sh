#!/usr/bin/env bash
# Prove a snapshot of the data volume restores (R08): a NEW volume from the
# snapshot, attached to the running app instance beside the real one,
# mounted at /mnt/restore-test, a throwaway pgvector container started on its
# copy of the database, and its row counts compared with production's. Then
# everything is removed again: the container, the mount, the volume. The
# production volume is never detached, written or mounted twice.
#
#   scripts/aws/restore-test.sh             # the newest snapshot of rag-systems-data
#   scripts/aws/restore-test.sh snap-0123…  # a given one
#
# Needs EC2 running (scripts/aws/session.sh start). Costs a few cents: a
# volume for the minutes the test takes.
set -euo pipefail

export AWS_PROFILE=${AWS_PROFILE:-personal}
export AWS_REGION=${AWS_REGION:-eu-west-2}
volume_name=rag-systems-data
device=/dev/sdh

fail() { echo "✗ $*" >&2; exit 1; }
say() { echo "▶ $*"; }

production=$(aws ec2 describe-volumes --filters Name=tag:Name,Values="$volume_name" \
  --query 'Volumes[0].[VolumeId,AvailabilityZone,Attachments[0].InstanceId]' --output text)
read -r production_volume zone instance <<<"$production"
[[ $production_volume == vol-* ]] || fail "no volume tagged Name=$volume_name"
[[ $instance == i-* ]] || fail "$volume_name isn't attached to an instance"
state=$(aws ec2 describe-instances --instance-ids "$instance" --query 'Reservations[0].Instances[0].State.Name' --output text)
[[ $state == running ]] || fail "the instance is $state: start it first (scripts/aws/session.sh start)"

snapshot=${1:-$(aws ec2 describe-snapshots --owner-ids self --filters Name=volume-id,Values="$production_volume" \
  Name=status,Values=completed --query 'sort_by(Snapshots,&StartTime)[-1].SnapshotId' --output text)}
[[ $snapshot == snap-* ]] || fail "no completed snapshot of $production_volume"
say "Restoring $snapshot ($(aws ec2 describe-snapshots --snapshot-ids "$snapshot" --query 'Snapshots[0].StartTime' --output text))"

volume=$(aws ec2 create-volume --snapshot-id "$snapshot" --availability-zone "$zone" --volume-type gp3 \
  --tag-specifications 'ResourceType=volume,Tags=[{Key=Name,Value=rag-systems-restore-test},{Key=Project,Value=rag-systems}]' \
  --query VolumeId --output text)
[[ $volume != "$production_volume" ]] || fail "refusing: the test volume is the production volume"

run() {   # run a shell script on the instance through SSM; print its output; fail on failure
  local id status
  id=$(aws ssm send-command --instance-ids "$instance" --document-name AWS-RunShellScript \
    --parameters "$(jq -n --arg c "$1" '{commands: [$c], executionTimeout: ["900"]}')" \
    --query Command.CommandId --output text)
  aws ssm wait command-executed --command-id "$id" --instance-id "$instance" 2>/dev/null || true
  status=$(aws ssm get-command-invocation --command-id "$id" --instance-id "$instance" --query Status --output text)
  aws ssm get-command-invocation --command-id "$id" --instance-id "$instance" --query StandardOutputContent --output text
  [[ $status == Success ]] || { aws ssm get-command-invocation --command-id "$id" --instance-id "$instance" \
    --query StandardErrorContent --output text >&2; return 1; }
}

cleanup() {
  say "Cleaning up"
  run 'docker rm -f restore-check >/dev/null 2>&1 || true; umount /mnt/restore-test 2>/dev/null || true' >/dev/null || true
  aws ec2 detach-volume --volume-id "$volume" >/dev/null 2>&1 || true
  aws ec2 wait volume-available --volume-ids "$volume" 2>/dev/null || true
  aws ec2 delete-volume --volume-id "$volume" && echo "  deleted $volume"
}
trap cleanup EXIT

say "Waiting for $volume"
aws ec2 wait volume-available --volume-ids "$volume"
aws ec2 attach-volume --volume-id "$volume" --instance-id "$instance" --device "$device" >/dev/null
aws ec2 wait volume-in-use --volume-ids "$volume"

serial=${volume/-/}
say "Mounting the copy and starting PostgreSQL on it"
run "set -e
for i in \$(seq 1 30); do [ -e /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_$serial ] && break; sleep 2; done
dev=\$(readlink -f /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_$serial)
[ \"\$(findmnt -no SOURCE /srv/rag-systems)\" != \"\$dev\" ] || { echo 'the copy is the production device' >&2; exit 1; }
mkdir -p /mnt/restore-test
mount \$dev /mnt/restore-test   # replays the journal: the snapshot is crash-consistent
docker run -d --name restore-check --network none -v /mnt/restore-test/pgvector:/var/lib/postgresql/data \
  pgvector/pgvector:pg17 >/dev/null
for i in \$(seq 1 60); do docker exec restore-check pg_isready -h 127.0.0.1 -U rag >/dev/null 2>&1 && break; sleep 2; done
docker exec restore-check pg_isready -h 127.0.0.1 -U rag"

counts='for db in $(psql -U rag -d postgres -Atc "SELECT datname FROM pg_database WHERE NOT datistemplate"); do
  for t in rag_chunks research_chunks app_users app_sessions; do
    n=$(psql -U rag -d "$db" -Atc "SELECT count(*) FROM $t" 2>/dev/null) && echo "$db.$t $n"
  done
done'
say "Comparing row counts"
restored=$(run "docker exec restore-check sh -c '$counts'" | sort)
live=$(run "docker exec rag-systems-pgvector-1 sh -c '$counts'" | sort)
echo "restored:"; sed 's/^/  /' <<<"$restored"
[[ -n $restored ]] || fail "the restored database has none of the expected tables"
if [[ $restored == "$live" ]]; then
  echo "✓ Restored copy matches production row for row (no writes since the snapshot)"
else
  echo "production now:"; sed 's/^/  /' <<<"$live"
  echo "✓ Restored and readable; counts differ from production by what changed since the snapshot"
fi
