variable "aws_region" {
  description = "AWS Region for the EC2 foundation."
  type        = string
  default     = "eu-west-2"
}

variable "instance_type" {
  description = "ARM64 EC2 size. Set explicitly after reviewing the monthly estimate."
  type        = string

  validation {
    condition     = startswith(var.instance_type, "t4g.")
    error_message = "Choose a t4g instance type for the ARM64 AMI."
  }
}

variable "data_volume_size_gb" {
  description = "Persistent encrypted EBS volume for pgvector and self-hosted Langfuse data."
  type        = number
  default     = 60

  validation {
    condition     = var.data_volume_size_gb >= 20
    error_message = "The data volume must be at least 20 GiB."
  }
}
variable "agentcore_endpoints_enabled" {
  description = "Create the interface endpoints the agentcore-kb runtime needs (billed hourly). Enable while using the agent; disable afterwards."
  type        = bool
  default     = false
}

variable "backups_enabled" {
  description = "Take the daily snapshots of the data volume (backup.tf)."
  type        = bool
  default     = true
}

variable "backup_time_utc" {
  description = "When the daily snapshot starts, HH:MM in UTC."
  type        = string
  default     = "03:00"

  validation {
    condition     = can(regex("^([01][0-9]|2[0-3]):[0-5][0-9]$", var.backup_time_utc))
    error_message = "Use HH:MM, 24-hour, UTC."
  }
}

variable "backup_retain_count" {
  description = "How many daily snapshots to keep (the oldest is deleted when a new one is taken)."
  type        = number
  default     = 14

  validation {
    condition     = var.backup_retain_count >= 1 && var.backup_retain_count <= 1000
    error_message = "Keep between 1 and 1000 snapshots."
  }
}
