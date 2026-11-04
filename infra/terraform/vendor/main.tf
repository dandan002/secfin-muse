# One system per state. The workspace names it: <env>-<system>, e.g. demo-ngt or demo-desk.
#
#   terraform workspace select -or-create demo-ngt
#   terraform apply -var-file=../demo.tfvars
#
# Apply `shared` (admin token, snapshots, the Postgres image) and `edge` (the Cloudflare Tunnels and
# their tokens) for the environment first. Each system owns its image registry; infra/up.sh creates it,
# pushes the project's image, then applies.
#
# The desk is the LoanDesk platform, hosted the same way as a mock: its API with its own VPC, Postgres
# and tunnel, and its web app as a second service (as mock-ib serves Desk Chat). It calls the mocks at
# their public URLs like any client.

terraform {
  required_version = ">= 1.11"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  backend "s3" {}
}

variable "stack" {
  type        = string
  default     = null
  description = "Overrides the workspace name (<env>-<system>); for tests."
}

locals {
  stack   = coalesce(var.stack, terraform.workspace)
  parts   = regex("^(?P<env>[a-z0-9]{1,8})-(?P<service>${join("|", keys(local.catalog))})$", local.stack)
  env     = local.parts.env
  service = local.parts.service
  prefix  = "loandesk-${local.env}"
  is      = { for s in keys(local.catalog) : s => s == local.service }

  port     = local.catalog[local.service].port
  hostname = local.tunnels[local.service].hostname
  ui       = lookup(local.uis, local.service, null)

  # Every mock is reached at its public URL, like a real vendor. The exception is mock-gateway calling its
  # own simulated counterparties, which stays inside its container.
  urls = {
    for s in local.mocks : s => (
      s == local.service && s == "gateway" ? "http://127.0.0.1:${local.catalog[s].port}" : "https://${local.tunnels[s].hostname}"
    )
  }

  # The desk reads the same MOCK_* names (settings.py accepts both) plus its own LOANDESK_* ones.
  environment = concat(
    [for s, url in local.urls : { name = "MOCK_${upper(s)}_URL", value = url }],
    [{ name = "MOCK_SEED", value = tostring(var.seed) }],
    local.is.desk ? [
      { name = "LOANDESK_CLERK_ISSUER", value = var.clerk_issuer },
      { name = "LOANDESK_CORS_ORIGINS", value = jsonencode(["https://${local.tunnels.web.hostname}"]) },
      { name = "LOANDESK_SYNC_ENABLED", value = tostring(var.desk_sync) },
      { name = "LOANDESK_AGENT_MODE", value = "off" },
      ] : [
      { name = "MOCK_LLM_MODE", value = var.llm_mode },
    ],
    local.is.gateway ? [{ name = "MOCK_GATEWAY_DOWNSTREAM_URL", value = var.gateway_downstream_url }] : [],
  )

  # The desk only needs the admin token (it asks each mock for its own keys); the mocks derive theirs.
  secrets = concat(
    [{ name = "MOCK_ADMIN_TOKEN", valueFrom = data.aws_ssm_parameter.admin_token.arn }],
    local.is.desk ? [] : [{ name = "MOCK_KEY_SECRET", valueFrom = data.aws_ssm_parameter.key_secret.arn }],
    [for p in data.aws_ssm_parameter.llm_key : { name = "META_AI_API", valueFrom = p.arn }],
  )

  postgres_image = "${data.aws_ecr_repository.postgres.repository_url}:${var.postgres_image_tag}"
  snapshot = {
    bucket = "${local.prefix}-snapshots-${data.aws_caller_identity.this.account_id}"
    key    = "${local.service}.sql.gz"
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      project     = "loandesk-ai"
      account     = "market"
      environment = local.env
      system      = local.service
      managed-by  = "terraform"
    }
  }
}

data "aws_caller_identity" "this" {}

data "aws_ssm_parameter" "admin_token" {
  name            = "/${local.prefix}/admin-token"
  with_decryption = false # only its ARN is needed; ECS reads the value
}

data "aws_ssm_parameter" "key_secret" {
  name            = "/${local.prefix}/key-secret"
  with_decryption = false
}

data "aws_ssm_parameter" "llm_key" {
  count           = local.is.ib && var.meta_ai_api_key != "" ? 1 : 0
  name            = "/${local.prefix}/meta-ai-api"
  with_decryption = false
}

# Written by edge, one per ECS service.
data "aws_ssm_parameter" "tunnel" {
  for_each        = toset(concat([local.service], local.ui == null ? [] : [local.ui]))
  name            = "/${local.prefix}/tunnel/${each.key}"
  with_decryption = false
}

data "aws_ecr_repository" "postgres" {
  name = "${local.prefix}/postgres"
}

# The desk's API is on the open internet; without sign-in anyone could trade on it.
resource "terraform_data" "desk_sign_in" {
  lifecycle {
    precondition {
      condition     = !(var.running && local.is.desk && var.clerk_issuer == "")
      error_message = "The desk needs clerk_issuer (its API is public behind the tunnel)."
    }
  }
}

module "system" {
  source = "../modules/vendor"

  name                   = "ld-${local.env}-${local.service}"
  service                = local.service
  region                 = var.region
  hostname               = local.hostname
  tunnel_token_parameter = data.aws_ssm_parameter.tunnel[local.service].arn
  repository_prefix      = local.prefix
  image_tag              = var.image_tag
  port                   = local.port
  app_module             = local.is.desk ? "loandesk.app:app" : "mock_${local.service}.app:app"
  database_url_env       = local.is.desk ? "LOANDESK_DATABASE_URL" : "MOCK_DATABASE_URL"
  cpu                    = local.is.desk ? 512 : 256
  environment            = local.environment
  secrets                = local.secrets
  postgres_image         = local.postgres_image
  snapshot               = local.snapshot

  ui = local.ui == null ? null : {
    name = local.ui, hostname = local.tunnels[local.ui].hostname, tunnel_token_parameter = data.aws_ssm_parameter.tunnel[local.ui].arn
  }

  running            = var.running
  log_retention_days = var.log_retention_days
}

output "urls" {
  value = module.system.urls
}

output "cluster" {
  value = module.system.cluster
}

output "ecr" {
  value = module.system.ecr
}

output "running" {
  value = module.system.running
}
