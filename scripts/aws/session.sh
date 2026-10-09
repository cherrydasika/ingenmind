#!/usr/bin/env bash
# Start or stop a whole work session from your workstation, so nothing that
# bills by the hour is left running: the EC2 instance, the Compose stack on it,
# and the agentcore-kb interface endpoints.
#
#   scripts/aws/session.sh status
#   scripts/aws/session.sh start            # EC2 + stack
#   scripts/aws/session.sh start --agent    # ... plus the agentcore-kb endpoints
#   scripts/aws/session.sh stop             # endpoints off, stack stopped, EC2 stopped
#
# Needs the AWS CLI, Terraform (initialised in infra/terraform) and credentials
# for the account (AWS_PROFILE, default "personal"). The AgentCore Runtime,
# Gateway and Harness bill per use only and are left in place.
set -euo pipefail

export AWS_PROFILE=${AWS_PROFILE:-personal}
export AWS_REGION=${AWS_REGION:-eu-west-2}
repo=$(cd "$(dirname "$0")/../.." && pwd)
tf_dir=$repo/infra/terraform
instance_type=${TF_INSTANCE_TYPE:-t4g.xlarge}

fail() { echo "✗ $*" >&2; exit 1; }
step() { echo; echo "▶ $*"; }

instance_id() {
  aws ec2 describe-instances \
    --filters Name=tag:Name,Values=rag-systems-app Name=instance-state-name,Values=pending,running,stopping,stopped \
    --query 'Reservations[0].Instances[0].InstanceId' --output text
}

instance_state() {
  aws ec2 describe-instances --instance-ids "$1" --query 'Reservations[0].Instances[0].State.Name' --output text
}

endpoint_count() {
  aws ec2 describe-vpc-endpoints \
    --filters Name=tag:Name,Values='agentcore-kb-*' Name=vpc-endpoint-type,Values=Interface \
      Name=vpc-endpoint-state,Values=pendingAcceptance,pending,available \
    --query 'length(VpcEndpoints)' --output text
}

endpoints() {  # endpoints true|false
  (cd "$tf_dir" && terraform apply -input=false -auto-approve -lock-timeout=120s \
    -target='aws_vpc_endpoint.interface' \
    -var instance_type="$instance_type" -var agentcore_endpoints_enabled="$1" >/dev/null)
  echo "  interface endpoints now: $(endpoint_count)"
}

# Run a command on the instance through SSM and wait for it.
on_instance() {  # on_instance <instance-id> <command> <comment>
  local id command_id status
  id=$(aws ssm send-command --instance-ids "$1" --document-name AWS-RunShellScript \
    --parameters "$(python3 -c 'import json,sys; print(json.dumps({"commands": [sys.argv[1]], "executionTimeout": ["900"]}))' "$2")" \
    --comment "$3" --query Command.CommandId --output text)
  while :; do
    sleep 5
    status=$(aws ssm get-command-invocation --command-id "$id" --instance-id "$1" --query Status --output text 2>/dev/null || echo Pending)
    case $status in Pending|InProgress|Delayed) ;; *) break ;; esac
  done
  aws ssm get-command-invocation --command-id "$id" --instance-id "$1" --query StandardOutputContent --output text | sed 's/^/  /'
  [[ $status == Success ]] || fail "$3: $status"
}

wait_for_ssm() {  # the SSM agent registers a minute or so after boot
  for _ in $(seq 1 60); do
    [[ $(aws ssm describe-instance-information --filters Key=InstanceIds,Values="$1" \
      --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null) == Online ]] && return
    sleep 5
  done
  fail "SSM agent on $1 did not come online"
}

status() {
  local id
  id=$(instance_id)
  echo "EC2 $id: $(instance_state "$id")"
  echo "agentcore-kb interface endpoints: $(endpoint_count) (about \$0.011/hour each)"
}

case "${1:-}" in
  status)
    status
    ;;
  start)
    id=$(instance_id)
    step "Start EC2 $id"
    aws ec2 start-instances --instance-ids "$id" >/dev/null
    aws ec2 wait instance-running --instance-ids "$id"
    wait_for_ssm "$id"
    if [[ ${2:-} == --agent ]]; then
      step "Interface endpoints on"
      endpoints true
    fi
    step "Start the stack"
    on_instance "$id" "bash /opt/rag-systems/current/scripts/aws/stack.sh start 2>&1 | grep -vE '^ (Container|Network) '" "session start"
    status
    ;;
  stop)
    id=$(instance_id)
    # Endpoints first: they bill even when nothing else runs.
    step "Interface endpoints off"
    endpoints false
    if [[ $(instance_state "$id") == running ]]; then
      step "Stop the stack"
      on_instance "$id" "bash /opt/rag-systems/current/scripts/aws/stack.sh stop 2>&1 | grep -vE '^ (Container|Network) '" "session stop"
      step "Stop EC2 $id"
      aws ec2 stop-instances --instance-ids "$id" >/dev/null
      aws ec2 wait instance-stopped --instance-ids "$id"
    fi
    status
    ;;
  *)
    echo "Usage: scripts/aws/session.sh start [--agent] | stop | status" >&2
    exit 2
    ;;
esac
