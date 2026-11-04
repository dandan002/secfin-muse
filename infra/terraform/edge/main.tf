# The public side of an environment, on Cloudflare's free plan: one Cloudflare Tunnel per ECS service
# with its DNS name. Free and always there, so apply it once (infra/edge.sh) and again only when the
# catalog changes; up.sh and down.sh never touch Cloudflare.
#
#   mock-ngt.<domain>  ──HTTPS──►  Cloudflare  ──tunnel──►  cloudflared in ngt's task  ──►  127.0.0.1:8030
#
# A stopped system's hostname answers with Cloudflare's 530 until its task connects again. Each tunnel's
# token goes to SSM, where the system's task reads it (cloudflared's TUNNEL_TOKEN).
#
#   export CLOUDFLARE_API_TOKEN=...   # Account: Cloudflare Tunnel Edit; Zone (the domain): DNS Edit
#   terraform workspace select -or-create demo
#
# The demo environment's tunnels, records and tokens were made through the Cloudflare API with these same
# names, settings and SSM paths; to manage them here, import them first (terraform import, by tunnel and
# record id) rather than applying over them.

terraform {
  required_version = ">= 1.11"
  required_providers {
    aws        = { source = "hashicorp/aws", version = "~> 6.0" }
    cloudflare = { source = "cloudflare/cloudflare", version = "~> 5.0" }
    random     = { source = "hashicorp/random", version = "~> 3.6" }
  }
  backend "s3" {}
}

variable "env" {
  type        = string
  default     = null
  description = "Overrides the workspace name; for tests."
}

locals {
  env        = coalesce(var.env, terraform.workspace)
  prefix     = "loandesk-${local.env}"
  account_id = data.cloudflare_zone.this.account.id
  zone_id    = data.cloudflare_zone.this.zone_id
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "loandesk-ai", account = "market", environment = local.env, managed-by = "terraform" }
  }
}

provider "cloudflare" {} # CLOUDFLARE_API_TOKEN

resource "terraform_data" "env_name" {
  lifecycle {
    precondition {
      condition     = can(regex("^[a-z0-9]{1,8}$", local.env)) && local.env != "default"
      error_message = "Select a workspace named after the environment (1-8 lowercase letters/digits), e.g. demo."
    }
  }
}

data "cloudflare_zone" "this" {
  filter = { name = var.domain_name }
}

# -- tunnels ------------------------------------------------------------------------------------
resource "random_id" "tunnel_secret" {
  for_each    = local.tunnels
  byte_length = 32
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "this" {
  for_each      = local.tunnels
  account_id    = local.account_id
  name          = "${local.prefix}-${each.key}"
  config_src    = "cloudflare" # ingress rules below, not a file in the container
  tunnel_secret = random_id.tunnel_secret[each.key].b64_std
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "this" {
  for_each   = local.tunnels
  account_id = local.account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.this[each.key].id
  config = {
    ingress = [
      { hostname = each.value.hostname, service = "http://127.0.0.1:${each.value.port}" },
      { service = "http_status:404" },
    ]
  }
}

resource "cloudflare_dns_record" "tunnel" {
  for_each = local.tunnels
  zone_id  = local.zone_id
  name     = each.value.hostname
  type     = "CNAME"
  content  = "${cloudflare_zero_trust_tunnel_cloudflared.this[each.key].id}.cfargotunnel.com"
  proxied  = true
  ttl      = 1 # automatic
  comment  = "${local.prefix}: ${each.key}'s tunnel (infra/terraform/edge)"
}

# Plain http:// redirects to https:// for every hostname (the desk's tokens never travel in clear).
resource "cloudflare_zone_setting" "always_use_https" {
  zone_id    = local.zone_id
  setting_id = "always_use_https"
  value      = "on"
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "this" {
  for_each   = local.tunnels
  account_id = local.account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.this[each.key].id
}

resource "aws_ssm_parameter" "tunnel_token" {
  for_each    = local.tunnels
  name        = "/${local.prefix}/tunnel/${each.key}"
  description = "cloudflared TUNNEL_TOKEN for ${each.value.hostname}"
  type        = "SecureString"
  value       = data.cloudflare_zero_trust_tunnel_cloudflared_token.this[each.key].token
}

output "account_id" {
  value = local.account_id
}

output "urls" {
  value = { for k, t in local.tunnels : k => "https://${t.hostname}" }
}
