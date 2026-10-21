# Scheduled snapshots of the data volume (pgvector and Langfuse) by Data
# Lifecycle Manager: one a day, the newest backup_retain_count kept. EBS
# snapshots are incremental, so a day the instance was stopped costs almost
# nothing. A snapshot of a running stack is crash-consistent: PostgreSQL
# recovers from its write-ahead log, as after a power cut.
# scripts/aws/restore-test.sh proves a snapshot restores.

resource "aws_iam_role" "dlm" {
  name = "rag-systems-dlm"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "dlm.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "dlm" {
  role       = aws_iam_role.dlm.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}

resource "aws_dlm_lifecycle_policy" "data" {
  description        = "Daily snapshots of rag-systems-data"
  execution_role_arn = aws_iam_role.dlm.arn
  state              = var.backups_enabled ? "ENABLED" : "DISABLED"

  policy_details {
    resource_types = ["VOLUME"]
    target_tags    = { Name = "rag-systems-data" }

    schedule {
      name      = "daily"
      copy_tags = true
      tags_to_add = {
        SnapshotCreator = "dlm"
      }

      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = [var.backup_time_utc]
      }

      retain_rule {
        count = var.backup_retain_count
      }
    }
  }
}
