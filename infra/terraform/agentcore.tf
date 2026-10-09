# agentcore-kb: network, IAM and image registry for the knowledge-base agent
# (agentcore-kb/). The AgentCore Runtime, Gateway and Harness themselves are
# created with boto3 scripts in agentcore-kb/.
#
# The MCP server runs on AgentCore Runtime inside this VPC so it can reach
# pgvector on the EC2 host. The VPC has no NAT gateway, so the runtime reaches
# AWS services through interface endpoints. Those bill hourly (~$8/month each
# in eu-west-2) whether used or not, so they exist only while
# agentcore_endpoints_enabled is true:
#
#   terraform apply -var agentcore_endpoints_enabled=true    # before using the agent
#   terraform apply -var agentcore_endpoints_enabled=false   # afterwards
#
# Endpoint private DNS applies to the whole VPC, so the EC2 host's own calls
# to Bedrock, SSM and Logs also use the endpoints while they exist; their
# security group admits both the runtime and the EC2 host.

locals {
  agentcore_name           = "agentcore-kb"
  agentcore_interface_svcs = toset(["ecr.api", "ecr.dkr", "logs", "bedrock-runtime", "ssm"])
  pg_password_parameter    = "/rag-systems/prod/agentcore-kb-pg-password"
  account_id               = data.aws_caller_identity.current.account_id
  agentcore_trust = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    }]
  })
}

# ---------- Network ----------

resource "aws_security_group" "agentcore_runtime" {
  name        = "${local.agentcore_name}-runtime"
  description = "AgentCore Runtime ENIs for the knowledge-base MCP server; outbound only"
  vpc_id      = aws_vpc.app.id
  tags        = { Name = "${local.agentcore_name}-runtime" }
}

resource "aws_vpc_security_group_egress_rule" "runtime_to_pgvector" {
  security_group_id            = aws_security_group.agentcore_runtime.id
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "pgvector on the EC2 host"
}

resource "aws_vpc_security_group_egress_rule" "runtime_to_endpoints" {
  security_group_id = aws_security_group.agentcore_runtime.id
  cidr_ipv4         = aws_vpc.app.cidr_block
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "AWS service interface endpoints in this VPC"
}

resource "aws_vpc_security_group_egress_rule" "runtime_to_s3" {
  security_group_id = aws_security_group.agentcore_runtime.id
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "ECR image layers through the S3 gateway endpoint"
}

# The EC2 security group's only inbound rule: pgvector from the runtime.
resource "aws_vpc_security_group_ingress_rule" "pgvector_from_runtime" {
  security_group_id            = aws_security_group.app.id
  referenced_security_group_id = aws_security_group.agentcore_runtime.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "pgvector from the agentcore-kb MCP server"
}

resource "aws_security_group" "endpoints" {
  name        = "${local.agentcore_name}-endpoints"
  description = "Interface endpoints: HTTPS from the AgentCore runtime and the EC2 host"
  vpc_id      = aws_vpc.app.id
  tags        = { Name = "${local.agentcore_name}-endpoints" }
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_runtime" {
  security_group_id            = aws_security_group.endpoints.id
  referenced_security_group_id = aws_security_group.agentcore_runtime.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_ec2" {
  security_group_id            = aws_security_group.endpoints.id
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_endpoint" "interface" {
  for_each            = var.agentcore_endpoints_enabled ? local.agentcore_interface_svcs : toset([])
  vpc_id              = aws_vpc.app.id
  service_name        = "com.amazonaws.${var.aws_region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.app.id]
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true
  tags                = { Name = "${local.agentcore_name}-${each.key}" }
}

# Free, so it always exists. Default (full access) policy: the EC2 host also
# reads the artifact bucket through it.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.app.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.app.id]
  tags              = { Name = "${local.agentcore_name}-s3" }
}

# ---------- Image registry ----------

resource "aws_ecr_repository" "mcp" {
  name                 = "${local.agentcore_name}-mcp"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_lifecycle_policy" "mcp" {
  repository = aws_ecr_repository.mcp.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 5 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

# ---------- IAM: MCP server runtime ----------

resource "aws_iam_role" "agentcore_runtime" {
  name               = "${local.agentcore_name}-runtime"
  assume_role_policy = local.agentcore_trust
}

resource "aws_iam_role_policy" "agentcore_runtime" {
  name = "${local.agentcore_name}-runtime"
  role = aws_iam_role.agentcore_runtime.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = aws_ecr_repository.mcp.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = "arn:aws:bedrock:${var.aws_region}::foundation-model/amazon.titan-embed-text-v2:0"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.pg_password_parameter}"
      },
    ]
  })
}

# ---------- IAM: Gateway (calls the runtime) ----------

resource "aws_iam_role" "agentcore_gateway" {
  name               = "${local.agentcore_name}-gateway"
  assume_role_policy = local.agentcore_trust
}

resource "aws_iam_role_policy" "agentcore_gateway" {
  name = "${local.agentcore_name}-gateway"
  role = aws_iam_role.agentcore_gateway.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["bedrock-agentcore:InvokeAgentRuntime"]
      Resource = [
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:runtime/agentcore_kb_mcp-*",
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:runtime/agentcore_kb_mcp-*/*",
      ]
    }]
  })
}

# ---------- IAM: Harness (model + gateway) ----------

resource "aws_iam_role" "agentcore_harness" {
  name               = "${local.agentcore_name}-harness"
  assume_role_policy = local.agentcore_trust
}

resource "aws_iam_role_policy" "agentcore_harness" {
  name = "${local.agentcore_name}-harness"
  role = aws_iam_role.agentcore_harness.id
  # Based on the AgentCore harness execution role sample, without Browser and
  # Code Interpreter (unused). The harness provisions its own runtime and an
  # AgentCore Memory (memory/agentcore_kb-*) for session history.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "BedrockModel"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = concat(
          [data.aws_bedrock_inference_profile.haiku.inference_profile_arn],
          [for model in data.aws_bedrock_inference_profile.haiku.models : model.model_arn]
        )
      },
      {
        Sid      = "KnowledgeBaseGateway"
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:InvokeGateway"]
        Resource = "arn:aws:bedrock-agentcore:${var.aws_region}:${local.account_id}:gateway/agentcore-kb-*"
      },
      {
        Sid    = "SessionMemory"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:CreateEvent", "bedrock-agentcore:DeleteEvent", "bedrock-agentcore:GetEvent",
          "bedrock-agentcore:ListEvents", "bedrock-agentcore:RetrieveMemoryRecords",
        ]
        Resource = "arn:aws:bedrock-agentcore:${var.aws_region}:${local.account_id}:memory/agentcore_kb-*"
      },
      {
        Sid    = "WorkloadIdentity"
        Effect = "Allow"
        Action = ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT"]
        Resource = [
          "arn:aws:bedrock-agentcore:${var.aws_region}:${local.account_id}:workload-identity-directory/default",
          "arn:aws:bedrock-agentcore:${var.aws_region}:${local.account_id}:workload-identity-directory/default/workload-identity/harness_agentcore_kb-*",
        ]
      },
      {
        Sid      = "ManagedImageFromEcrPublic"
        Effect   = "Allow"
        Action   = ["ecr-public:GetAuthorizationToken", "sts:GetServiceBearerToken"]
        Resource = "*"
      },
      {
        Sid      = "LogGroups"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:DescribeLogStreams"]
        Resource = "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"
      },
      {
        Sid      = "LogStreams"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"
      },
      {
        Sid      = "Tracing"
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
        Resource = "*"
      },
      {
        Sid       = "Metrics"
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "bedrock-agentcore" } }
      },
    ]
  })
}

# ---------- IAM: the RAG web app calls the agent ----------

# The web UI's Agent mode (app/agent.py): read the harness and its gateway,
# list the gateway's tools, and invoke the harness. The harness runs its
# tools with its own role, so the EC2 role needs nothing beyond this.
resource "aws_iam_role_policy" "app_agent" {
  name = "rag-systems-agentcore-kb-agent"
  role = aws_iam_role.app.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        # InvokeHarness is also authorised as InvokeAgentRuntime on the harness.
        Action = [
          "bedrock-agentcore:GetHarness", "bedrock-agentcore:InvokeHarness",
          "bedrock-agentcore:InvokeAgentRuntime",
        ]
        Resource = "arn:aws:bedrock-agentcore:${var.aws_region}:${local.account_id}:harness/agentcore_kb-*"
      },
      {
        # Read-only view of the agents' memory on the Retrieval page.
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:ListSessions", "bedrock-agentcore:ListEvents", "bedrock-agentcore:ListMemoryRecords"]
        Resource = "arn:aws:bedrock-agentcore:${var.aws_region}:${local.account_id}:memory/agentcore_kb-*"
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:GetGateway", "bedrock-agentcore:InvokeGateway"]
        Resource = "arn:aws:bedrock-agentcore:${var.aws_region}:${local.account_id}:gateway/agentcore-kb-*"
      },
    ]
  })
}

# ---------- IAM: the research agent's web-search key ----------

# app/research.py reads the Tavily API key from this SecureString (created by
# hand, never in Terraform state).
locals {
  tavily_key_parameter = "/rag-systems/prod/tavily-api-key"
}

resource "aws_iam_role_policy" "app_research" {
  name = "rag-systems-research-search-key"
  role = aws_iam_role.app.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = "arn:aws:ssm:${var.aws_region}:${local.account_id}:parameter${local.tavily_key_parameter}"
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService"                      = "ssm.${var.aws_region}.amazonaws.com"
            "kms:EncryptionContext:PARAMETER_ARN" = "arn:aws:ssm:${var.aws_region}:${local.account_id}:parameter${local.tavily_key_parameter}"
          }
        }
      },
    ]
  })
}
