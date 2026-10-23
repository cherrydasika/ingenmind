#!/usr/bin/env bash
# Install and start the CloudWatch agent on the instance (R55), for the
# memory and disk alarms in infra/terraform/monitoring.tf. Run as root on the
# instance, after Terraform has given its role CloudWatchAgentServerPolicy:
#
#   sudo bash scripts/aws/cloudwatch-agent.sh
#
# Sends three metrics every 5 minutes: memory used, and disk used on / and
# /srv/rag-systems. Safe to run again.
set -euo pipefail

[[ $(id -u) -eq 0 ]] || { echo "Run as root on the EC2 instance" >&2; exit 1; }
command -v /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl >/dev/null \
  || dnf install -y -q amazon-cloudwatch-agent

config=/opt/aws/amazon-cloudwatch-agent/etc/rag-systems.json
cat > "$config" <<'EOF'
{
  "agent": { "metrics_collection_interval": 300 },
  "metrics": {
    "append_dimensions": { "InstanceId": "${aws:InstanceId}" },
    "aggregation_dimensions": [["InstanceId"], ["InstanceId", "path"]],
    "metrics_collected": {
      "mem": { "measurement": ["mem_used_percent"] },
      "disk": { "measurement": ["used_percent"], "resources": ["/", "/srv/rag-systems"], "drop_device": true }
    }
  }
}
EOF
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s -c "file:$config"
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a status
