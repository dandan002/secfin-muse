# Per-environment pieces that aren't any one system's: the simulator's admin token,
# mock-control's egress IP (which every external system allowlists, the way vendors whitelist a
# client's IPs), the session snapshots, the Postgres image and the auto-stop machinery. Nothing
# here is on a request path.
#
#   terraform workspace select -or-create demo   # the environment name

terraform {
  required_version = ">= 1.11"
  required_providers {
    archive = { source = "hashicorp/archive", version = "~> 2.7" }
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
  }
  backend "s3" {}
}

locals {
  env      = terraform.workspace
  prefix   = "loandesk-${local.env}"
  services = ["control", "ib", "gateway", "ngt", "wematch", "sfp", "custodian", "analytics"]
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "loandesk-ai", account = "market", environment = local.env, managed-by = "terraform" }
  }
}

resource "terraform_data" "env_name" {
  lifecycle {
    precondition {
      condition     = can(regex("^[a-z0-9]{1,8}$", local.env)) && local.env != "default"
      error_message = "Select a workspace named after the environment (1-8 lowercase letters/digits), e.g. demo."
    }
  }
}

resource "random_password" "admin_token" {
  length  = 40
  special = false
}

# SecureString parameters (standard tier, AWS-managed key) cost nothing while idle, unlike secrets.
resource "aws_ssm_parameter" "admin_token" {
  name        = "/${local.prefix}/admin-token"
  description = "X-Admin-Token for every mock's /_admin routes (mock-control sends it)"
  type        = "SecureString"
  value       = random_password.admin_token.result
}

resource "random_password" "key_secret" {
  length  = 48
  special = false
}

resource "aws_ssm_parameter" "key_secret" {
  name        = "/${local.prefix}/key-secret"
  description = "MOCK_KEY_SECRET: master secret for the mocks' bootstrap API keys, webhook secrets and FIX passwords"
  type        = "SecureString"
  value       = random_password.key_secret.result
}

resource "aws_ssm_parameter" "llm_key" {
  count = var.meta_ai_api_key != "" ? 1 : 0
  name  = "/${local.prefix}/meta-ai-api"
  type  = "SecureString"
  value = var.meta_ai_api_key
}

# mock-control's NAT egresses on this address. An idle Elastic IP is billed by the hour, so
# infra/down.sh releases it once every system is stopped and the next up.sh allocates a new one;
# each system picks up the current address when it starts.
resource "aws_eip" "control_egress" {
  domain = "vpc"
  tags   = { Name = "${local.prefix}-control-egress" }
}

# -- session snapshots --------------------------------------------------------------------------
# Each mock's Postgres saves a dump here when its task stops and restores it on the next start
# (infra/postgres), so a session picks up where it left off. Versioned, so a bad dump can be rolled
# back; destroying the environment deletes them.
resource "aws_s3_bucket" "snapshots" {
  bucket        = "${local.prefix}-snapshots-${data.aws_caller_identity.this.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "snapshots" {
  bucket = aws_s3_bucket.snapshots.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "snapshots" {
  bucket = aws_s3_bucket.snapshots.id
  rule {
    id     = "expire-old-dumps"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
  depends_on = [aws_s3_bucket_versioning.snapshots]
}

resource "aws_s3_bucket_public_access_block" "snapshots" {
  bucket                  = aws_s3_bucket.snapshots.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# The Postgres image every mock's task runs (infra/postgres): the official image plus the snapshot
# hooks. One per environment; infra/up.sh builds and pushes it.
resource "aws_ecr_repository" "postgres" {
  name                 = "${local.prefix}/postgres"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}

resource "aws_ecr_lifecycle_policy" "postgres" {
  repository = aws_ecr_repository.postgres.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 5 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

output "ib_url" {
  description = "VITE_IB_URL for the Desk Chat build (the browser calls mock-ib directly)."
  value       = "https://ib.${var.domain_name}"
}

output "urls" {
  description = "Where each system will live once its vendor stack is applied (chat = Desk Chat UI)."
  value = merge(
    { for s in local.services : s => "https://${s}.${var.domain_name}" },
    { chat = "https://chat.${var.domain_name}" },
  )
}

output "fix_endpoint" {
  value = "fix.${var.domain_name}:9878"
}

output "control_egress_ip" {
  value = aws_eip.control_egress.public_ip
}

output "admin_token_parameter" {
  description = "aws ssm get-parameter --with-decryption --name <name> --query Parameter.Value --output text"
  value       = aws_ssm_parameter.admin_token.name
}

output "snapshot_bucket" {
  value = aws_s3_bucket.snapshots.bucket
}

output "postgres_repository" {
  value = aws_ecr_repository.postgres.repository_url
}
