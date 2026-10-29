# One external system per state. The workspace names it: <env>-<service>, e.g. demo-ngt.
#
#   terraform workspace select -or-create demo-ngt
#   terraform apply -var-file=../demo.tfvars
#
# Apply `shared` for the environment first (admin token, mock-control's egress IP, snapshots, the
# Postgres image). Each system owns its image registry; infra/up.sh creates it, pushes the
# project's image, then applies.

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
  description = "Overrides the workspace name (<env>-<service>); for tests."
}

locals {
  stack   = coalesce(var.stack, terraform.workspace)
  parts   = regex("^(?P<env>[a-z0-9]{1,8})-(?P<service>control|ib|gateway|ngt|wematch|sfp|custodian)$", local.stack)
  env     = local.parts.env
  service = local.parts.service
  prefix  = "loandesk-${local.env}"

  catalog = {
    control   = { port = 8000 }
    ib        = { port = 8010 }
    gateway   = { port = 8020 }
    ngt       = { port = 8030 }
    wematch   = { port = 8040 }
    sfp       = { port = 8050 }
    custodian = { port = 8060 }
  }
  port     = local.catalog[local.service].port
  hostname = "${local.service}.${var.domain_name}"
  is       = { for s in keys(local.catalog) : s => s == local.service }

  # Every system is reached at its public URL, like a real vendor. The exception is mock-gateway
  # calling its own simulated counterparties, which stays inside its container.
  urls = {
    for s, c in local.catalog : s => (
      s == local.service && s == "gateway" ? "http://127.0.0.1:${c.port}" : "https://${s}.${var.domain_name}"
    )
  }

  environment = concat(
    [for s, url in local.urls : { name = "MOCK_${upper(s)}_URL", value = url }],
    [
      { name = "MOCK_SEED", value = tostring(var.seed) },
      { name = "MOCK_LLM_MODE", value = var.llm_mode },
    ],
    local.is.ngt ? [
      { name = "MOCK_NGT_FIX_HOST", value = "0.0.0.0" },
      { name = "MOCK_NGT_FIX_PORT", value = "9878" },
    ] : [],
    local.is.gateway ? [{ name = "MOCK_GATEWAY_DOWNSTREAM_URL", value = var.gateway_downstream_url }] : [],
  )

  secrets = concat(
    [{ name = "MOCK_ADMIN_TOKEN", valueFrom = data.aws_ssm_parameter.admin_token.arn }],
    [{ name = "MOCK_KEY_SECRET", valueFrom = data.aws_ssm_parameter.key_secret.arn }],
    [for p in data.aws_ssm_parameter.llm_key : { name = "META_AI_API", valueFrom = p.arn }],
  )

  postgres_image = "${data.aws_ecr_repository.postgres.repository_url}:${var.postgres_image_tag}"
  snapshot = {
    bucket = "${local.prefix}-snapshots-${data.aws_caller_identity.this.account_id}"
    key    = "${local.service}.sql.gz"
  }

  # Released while every system is stopped (see shared), so it may not exist; a running system
  # always has one, as up.sh applies shared first.
  control_egress = one(data.aws_eips.control_egress.allocation_ids) == null ? null : {
    id = one(data.aws_eips.control_egress.allocation_ids), ip = one(data.aws_eips.control_egress.public_ips)
  }

  # mock-control is the operators' console. Everything else is an external system its clients
  # (and mock-control, from its fixed egress IP) call into.
  ingress_cidrs = local.is.control ? var.allowed_cidrs : distinct(concat(
    var.allowed_cidrs, var.client_cidrs, local.control_egress == null ? [] : ["${local.control_egress.ip}/32"],
  ))
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

data "aws_route53_zone" "this" {
  name         = var.hosted_zone_name
  private_zone = false
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

data "aws_eips" "control_egress" {
  tags = { Name = "${local.prefix}-control-egress" }
}

data "aws_ecr_repository" "postgres" {
  name = "${local.prefix}/postgres"
}

resource "terraform_data" "egress_ip" {
  lifecycle {
    precondition {
      condition     = !(var.running && local.is.control && local.control_egress == null)
      error_message = "mock-control's egress IP is missing: apply shared first (infra/up.sh does)."
    }
  }
}

module "system" {
  source = "../modules/vendor"

  name              = "ld-${local.env}-${local.service}"
  service           = local.service
  region            = var.region
  hostname          = local.hostname
  zone_id           = data.aws_route53_zone.this.zone_id
  repository_prefix = local.prefix
  image_tag         = var.image_tag
  port              = local.port
  app_module        = "mock_${local.service}.app:app"
  environment       = local.environment
  secrets           = local.secrets
  postgres_image    = local.postgres_image
  snapshot          = local.snapshot

  ingress_cidrs = local.ingress_cidrs
  # mock-control egresses through a NAT on the shared Elastic IP, so the others can allowlist it.
  nat_eip_allocation_id = local.is.control ? try(local.control_egress.id, null) : null

  fix       = local.is.ngt ? { port = 9878, hostname = "fix.${var.domain_name}" } : null
  desk_chat = local.is.ib ? { hostname = "chat.${var.domain_name}" } : null

  running            = var.running
  log_retention_days = var.log_retention_days
}

output "urls" {
  value = module.system.urls
}

output "fix_endpoint" {
  value = module.system.fix_endpoint
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
