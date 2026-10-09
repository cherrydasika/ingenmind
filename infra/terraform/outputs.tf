output "instance_id" {
  description = "EC2 instance for Systems Manager administration."
  value       = aws_instance.app.id
}

output "data_volume_id" {
  description = "Persistent pgvector and Langfuse volume; mount and back it up before deployment."
  value       = aws_ebs_volume.data.id
}

output "public_ip" {
  description = "Outbound-only public address; the security group allows no inbound connections."
  value       = aws_instance.app.public_ip
}

output "artifact_bucket" {
  description = "Private S3 bucket for immutable source archives; never store secrets or Terraform state here."
  value       = aws_s3_bucket.artifacts.id
}

output "artifact_publisher_role_arn" {
  description = "GitHub OIDC role limited to release-object upload."
  value       = aws_iam_role.artifact_publisher.arn
}

output "langfuse_parameter_name" {
  description = "One SecureString parameter to create separately; its value must not enter Terraform state."
  value       = local.secret_parameter
}

output "agentcore_kb" {
  description = "Inputs for the agentcore-kb setup scripts"
  value = {
    ecr_repository_url    = aws_ecr_repository.mcp.repository_url
    runtime_role_arn      = aws_iam_role.agentcore_runtime.arn
    gateway_role_arn      = aws_iam_role.agentcore_gateway.arn
    harness_role_arn      = aws_iam_role.agentcore_harness.arn
    runtime_subnet_id     = aws_subnet.app.id
    runtime_sg_id         = aws_security_group.agentcore_runtime.id
    pg_host               = aws_instance.app.private_ip
    pg_password_parameter = local.pg_password_parameter
    endpoints_enabled     = var.agentcore_endpoints_enabled
  }
}

output "cognito" {
  description = "Sign-in settings for the web app (AUTH_MODE=oidc); the client secret is in cognito_client_secret."
  value = {
    user_pool_id = aws_cognito_user_pool.app.id
    issuer       = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.app.id}"
    client_id    = aws_cognito_user_pool_client.app.id
    hosted_ui    = "https://${aws_cognito_user_pool_domain.app.domain}.auth.${var.aws_region}.amazoncognito.com"
    logout_url   = "https://${aws_cognito_user_pool_domain.app.domain}.auth.${var.aws_region}.amazoncognito.com/logout?client_id=${aws_cognito_user_pool_client.app.id}&logout_uri={return}"
  }
}

output "cognito_client_secret" {
  description = "The web app's Cognito client secret (copied into the SSM env parameter, never printed)."
  value       = aws_cognito_user_pool_client.app.client_secret
  sensitive   = true
}
