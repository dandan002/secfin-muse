# LoanDesk AI "market" account: every external-system mock on ECS Fargate.
#
# The mocks keep schedulers, WebSocket hubs, FIX sessions and fault state in process memory,
# so each runs as exactly one task and deploys stop the old task before starting the new one.
# They share one Postgres database (one schema each) because every service reads the
# control.clock row.

locals {
  name      = "ldm-${terraform.workspace}"
  namespace = "mocks.local"
  fix_port  = 9878

  mocks = {
    control   = { port = 8000, app = "mock_control.app:app" }
    ib        = { port = 8010, app = "mock_ib.app:app" }
    gateway   = { port = 8020, app = "mock_gateway.app:app" }
    ngt       = { port = 8030, app = "mock_ngt.app:app" }
    wematch   = { port = 8040, app = "mock_wematch.app:app" }
    sfp       = { port = 8050, app = "mock_sfp.app:app" }
    custodian = { port = 8060, app = "mock_custodian.app:app" }
  }

  # Every ECS service: the Python mocks plus the Desk Chat static build on nginx.
  services = merge(
    { for k, m in local.mocks : k => { port = m.port, health = "/health", mock = true } },
    { chat = { port = 80, health = "/", mock = false } },
  )

  public_hosts  = { for k, _ in local.services : k => "${k}.${var.domain_name}" }
  public_urls   = { for k, h in local.public_hosts : k => "https://${h}" }
  fix_host      = "fix.${var.domain_name}"
  internal_urls = { for k, m in local.mocks : k => "http://${k}.${local.namespace}:${m.port}" }

  # Same variables docker-compose sets, with Cloud Map names in place of compose hostnames.
  mock_env = concat(
    [for k, url in local.internal_urls : { name = "MOCK_${upper(k)}_URL", value = url }],
    [
      { name = "MOCK_SEED", value = tostring(var.seed) },
      { name = "MOCK_LLM_MODE", value = var.llm_mode },
    ],
  )
  service_env = {
    ngt = [
      { name = "MOCK_NGT_FIX_HOST", value = "0.0.0.0" },
      { name = "MOCK_NGT_FIX_PORT", value = tostring(local.fix_port) },
    ]
    gateway = [{ name = "MOCK_GATEWAY_DOWNSTREAM_URL", value = var.gateway_downstream_url }]
  }

  mock_secrets = concat(
    [
      { name = "MOCK_DATABASE_URL", valueFrom = aws_secretsmanager_secret.db_url.arn },
      { name = "MOCK_ADMIN_TOKEN", valueFrom = aws_secretsmanager_secret.admin_token.arn },
    ],
    [for s in aws_secretsmanager_secret.llm_key : { name = "META_AI_API", valueFrom = s.arn }],
  )
}

data "aws_availability_zones" "available" {
  state = "available"
}
