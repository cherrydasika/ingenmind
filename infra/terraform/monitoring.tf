# CloudWatch alarms on the instance (R55): the server itself, below what the
# app's own alerts (Monitoring → Alerts) can see. Each alarm notifies the SNS
# topic; with alarm_email set, AWS emails that address once to confirm the
# subscription. Memory and disk come from the CloudWatch agent, which
# scripts/aws/cloudwatch-agent.sh installs on the instance. While the
# instance is stopped there are no data points, and missing data is not a
# breach. About $0.40 a month for the alarms and $0.90 for the agent's three
# metrics.

variable "alarm_email" {
  description = "Where the CloudWatch alarms are emailed (SNS asks this address to confirm). Empty: the topic only."
  type        = string
  default     = ""
}

resource "aws_sns_topic" "alarms" {
  name = "rag-systems-alarms"
}

resource "aws_sns_topic_subscription" "alarm_email" {
  count     = var.alarm_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

resource "aws_iam_role_policy_attachment" "cloudwatch_agent" {
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

locals {
  alarms = {
    status-check = {
      description = "The instance or its host failed a status check"
      namespace   = "AWS/EC2"
      metric      = "StatusCheckFailed"
      threshold   = 1
      periods     = 2
      dimensions  = { InstanceId = aws_instance.app.id }
    }
    cpu = {
      description = "CPU above 90% for 15 minutes"
      namespace   = "AWS/EC2"
      metric      = "CPUUtilization"
      threshold   = 90
      periods     = 3
      dimensions  = { InstanceId = aws_instance.app.id }
    }
    memory = {
      description = "Memory above 90% for 10 minutes"
      namespace   = "CWAgent"
      metric      = "mem_used_percent"
      threshold   = 90
      periods     = 2
      dimensions  = { InstanceId = aws_instance.app.id }
    }
    root-disk = {
      description = "The root disk (Docker images) above 85%"
      namespace   = "CWAgent"
      metric      = "disk_used_percent"
      threshold   = 85
      periods     = 1
      dimensions  = { InstanceId = aws_instance.app.id, path = "/" }
    }
    data-disk = {
      description = "The data volume (PostgreSQL, Langfuse) above 85%"
      namespace   = "CWAgent"
      metric      = "disk_used_percent"
      threshold   = 85
      periods     = 1
      dimensions  = { InstanceId = aws_instance.app.id, path = "/srv/rag-systems" }
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "app" {
  for_each = local.alarms

  alarm_name          = "rag-systems-${each.key}"
  alarm_description   = each.value.description
  namespace           = each.value.namespace
  metric_name         = each.value.metric
  dimensions          = each.value.dimensions
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = each.value.periods
  threshold           = each.value.threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}
