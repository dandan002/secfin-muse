# Per-environment pieces that aren't any one system's: the simulator's admin token and
# mock-control's fixed egress IP (which every external system allowlists, the way vendors
# whitelist a client's IPs). Nothing here is on a request path.
#
#   terraform workspace select -or-create demo   # the environment name

terraform {
  required_version = ">= 1.11"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
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

resource "aws_secretsmanager_secret" "admin_token" {
  name                    = "${local.prefix}/admin-token"
  description             = "X-Admin-Token for every mock's /_admin routes (mock-control sends it)"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "admin_token" {
  secret_id     = aws_secretsmanager_secret.admin_token.id
  secret_string = random_password.admin_token.result
}

resource "aws_secretsmanager_secret" "llm_key" {
  count                   = var.meta_ai_api_key != "" ? 1 : 0
  name                    = "${local.prefix}/meta-ai-api"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "llm_key" {
  count         = length(aws_secretsmanager_secret.llm_key)
  secret_id     = aws_secretsmanager_secret.llm_key[0].id
  secret_string = var.meta_ai_api_key
}

resource "aws_eip" "control_egress" {
  domain = "vpc"
  tags   = { Name = "${local.prefix}-control-egress" }
}

resource "aws_budgets_budget" "monthly" {
  count        = var.budget_email != "" ? 1 : 0
  name         = "${local.prefix}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = [format("user:environment$%s", local.env)] # tag must be activated for cost allocation
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }
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

output "admin_token_secret_arn" {
  description = "aws secretsmanager get-secret-value --secret-id <arn> --query SecretString --output text"
  value       = aws_secretsmanager_secret.admin_token.arn
}
