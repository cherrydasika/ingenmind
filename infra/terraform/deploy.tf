# Deploying from GitHub Actions (#25), and Session Manager preferences.
#
# The deploy workflow (.github/workflows/deploy.yml) runs after each publish,
# in the "production" environment, which only main may use. It may
# do one thing on EC2: run scripts/aws/deploy.sh for a full commit SHA,
# through the command document below. It cannot run any other command, on
# any other instance.

resource "aws_ssm_document" "deploy" {
  name            = "rag-systems-deploy"
  document_type   = "Command"
  document_format = "JSON"
  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Deploy a published release: scripts/aws/deploy.sh COMMIT (falls back to the previous release if it fails)."
    parameters = {
      commit = {
        type           = "String"
        description    = "The full commit SHA of a published release"
        allowedPattern = "^[0-9a-f]{40}$"
      }
    }
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "deploy"
      inputs = {
        timeoutSeconds = "3000"
        runCommand     = ["bash /opt/rag-systems/current/scripts/aws/deploy.sh {{ commit }}"]
      }
    }]
  })
}

resource "aws_iam_role" "deployer" {
  name = "rag-systems-deployer"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com" }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          # Only a job in this repository's "production" environment (required reviewer: the maintainer).
          "token.actions.githubusercontent.com:sub" = "repo:cherrydasika@32058696/ingenmind@1411512145:environment:production"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "deployer" {
  name = "rag-systems-deploy-command"
  role = aws_iam_role.deployer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "RunTheDeployDocumentOnTheAppInstanceOnly"
        Effect   = "Allow"
        Action   = ["ssm:SendCommand"]
        Resource = [aws_ssm_document.deploy.arn, aws_instance.app.arn]
      },
      {
        Sid      = "ReadCommandResultsAndInstanceState"
        Effect   = "Allow"
        Action   = ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations", "ec2:DescribeInstances"]
        Resource = "*"
      }
    ]
  })
}

# Session Manager's preferences for this account and region (the tunnels to the
# app and Langfuse): sessions close after 60 minutes idle, the most allowed
# (the default was 20).
resource "aws_ssm_document" "session_preferences" {
  name            = "SSM-SessionManagerRunShell"
  document_type   = "Session"
  document_format = "JSON"
  content = jsonencode({
    schemaVersion = "1.0"
    description   = "Session Manager preferences (rag-systems)"
    sessionType   = "Standard_Stream"
    inputs = {
      idleSessionTimeout          = "60"
      maxSessionDuration          = ""
      runAsEnabled                = false
      runAsDefaultUser            = ""
      s3BucketName                = ""
      s3KeyPrefix                 = ""
      s3EncryptionEnabled         = true
      cloudWatchLogGroupName      = ""
      cloudWatchEncryptionEnabled = true
      cloudWatchStreamingEnabled  = false
      kmsKeyId                    = ""
      shellProfile                = { linux = "", windows = "" }
    }
  })
}
