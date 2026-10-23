#!/usr/bin/env bash
# The analytics export (R57) from the EC2 server to this workstation, in one
# command: Parquet files and explore.sql for DuckDB, through an SSM session
# (no SSH, no open port, no bucket). Personal text only with --include-text.
#
#   scripts/aws/export-analytics.sh [folder] [--include-text]    # default folder: ./analytics-export
#
# Needs the AWS CLI with the Session Manager plugin and credentials for the
# account (AWS_PROFILE, default "personal"); the instance must be running.
set -euo pipefail

export AWS_PROFILE=${AWS_PROFILE:-personal}
export AWS_REGION=${AWS_REGION:-eu-west-2}
out=${1:-analytics-export}
flag=${2:-}
[[ $out == --include-text ]] && { out=analytics-export; flag=--include-text; }

id=$(aws ec2 describe-instances \
  --filters Name=tag:Name,Values=rag-systems-app Name=instance-state-name,Values=running \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
[[ $id != None && -n $id ]] || { echo "✗ the instance isn't running (scripts/aws/session.sh start)" >&2; exit 1; }

raw=$(mktemp)
# The plugin ends the session when its stdin closes. Without a terminal (a
# script, CI) that is before the data arrives, so hold stdin open with a pipe
# nothing writes to; the session still ends when the export does.
hold=$(mktemp -u)
mkfifo "$hold"
sleep 3600 > "$hold" &
keep=$!
trap 'rm -f "$raw" "$hold"; kill $keep 2>/dev/null || true' EXIT
aws ssm start-session --target "$id" --document-name AWS-StartInteractiveCommand \
  --parameters "command=[\"sudo bash /opt/rag-systems/current/scripts/aws/stack.sh export $flag\"]" > "$raw" < "$hold"
mkdir -p "$out"
tr -d '\r' < "$raw" | sed -n '/^-----BEGIN INGENMIND EXPORT-----$/,/^-----END INGENMIND EXPORT-----$/p' \
  | sed '1d;$d' | base64 -d | tar xzf - -C "$out"
echo "✓ $(ls "$out"/*.parquet | wc -l | tr -d ' ') files in $out; open them with: cd $out && duckdb -init explore.sql"
