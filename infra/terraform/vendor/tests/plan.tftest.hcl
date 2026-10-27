# Offline plans against a mocked AWS provider: `terraform init -backend=false && terraform test`.

mock_provider "aws" {
  override_during = plan # mock computed values at plan time, as the real provider knows these

  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b", "us-east-1c"] }
  }
  mock_data "aws_route53_zone" {
    defaults = { zone_id = "Z123" }
  }
  mock_data "aws_rds_engine_version" {
    defaults = { version_actual = "16.9" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_data "aws_ecr_repository" {
    defaults = { repository_url = "111111111111.dkr.ecr.us-east-1.amazonaws.com/loandesk-demo/mocks" }
  }
  mock_data "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:loandesk-demo/admin-token" }
  }
  mock_data "aws_eip" {
    defaults = { id = "eipalloc-0control", public_ip = "198.51.100.7" }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:db-url" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111111111111:role/x" }
  }
  mock_resource "aws_lb_target_group" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:111111111111:targetgroup/x/1" }
  }
  mock_resource "aws_acm_certificate" {
    defaults = { arn = "arn:aws:acm:us-east-1:111111111111:certificate/x", domain_validation_options = [] }
  }
}

variables {
  hosted_zone_name = "example.com"
  domain_name      = "mocks.example.com"
  allowed_cidrs    = ["203.0.113.0/24"]
  client_cidrs     = ["192.0.2.10/32"]
  image_tag        = "abc123"
}

run "vendor_is_reachable_by_operators_clients_and_control" {
  command = plan
  variables {
    stack = "demo-ngt"
  }

  assert {
    condition     = local.ingress_cidrs == tolist(["203.0.113.0/24", "192.0.2.10/32", "198.51.100.7/32"])
    error_message = "operators, clients and mock-control's egress IP"
  }
  assert {
    condition     = output.urls == { ngt = "https://ngt.mocks.example.com" } && output.fix_endpoint == "fix.mocks.example.com:9878"
    error_message = "endpoints"
  }
  assert {
    condition     = output.cluster == "ld-demo-ngt"
    error_message = "per-system naming"
  }
}

run "control_is_operators_only_and_calls_vendors_publicly" {
  command = plan
  variables {
    stack = "demo-control"
  }

  assert {
    condition     = local.ingress_cidrs == tolist(["203.0.113.0/24"])
    error_message = "only operators reach the simulator console"
  }
  assert {
    condition     = contains(local.environment, { name = "MOCK_SFP_URL", value = "https://sfp.mocks.example.com" })
    error_message = "control calls each system at its public URL"
  }
}

run "ib_serves_desk_chat" {
  command = plan
  variables {
    stack = "demo-ib"
  }
  assert {
    condition     = output.urls == { ib = "https://ib.mocks.example.com", chat = "https://chat.mocks.example.com" }
    error_message = "ib API and Desk Chat"
  }
}

run "gateway_calls_its_own_counterparties_locally" {
  command = plan
  variables {
    stack = "demo-gateway"
  }
  assert {
    condition     = contains(local.environment, { name = "MOCK_GATEWAY_URL", value = "http://127.0.0.1:8020" })
    error_message = "self-callbacks stay in the container"
  }
}

run "rejects_open_internet" {
  command = plan
  variables {
    stack        = "demo-sfp"
    client_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.client_cidrs]
}

run "llm_personas_get_the_key" {
  command = plan
  variables {
    stack           = "demo-ib"
    llm_mode        = "live"
    meta_ai_api_key = "sk-test"
  }
  assert {
    condition     = [for s in local.secrets : s.name] == ["MOCK_ADMIN_TOKEN", "META_AI_API"]
    error_message = "ib gets the LLM key"
  }
}

run "live_llm_needs_a_key" {
  command = plan
  variables {
    stack    = "demo-ib"
    llm_mode = "live"
  }
  expect_failures = [var.llm_mode]
}
