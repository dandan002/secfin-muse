# Offline plan against a mocked AWS provider: `terraform init -backend=false && terraform test`.

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
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:x" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = { repository_url = "111111111111.dkr.ecr.us-east-1.amazonaws.com/ldm/mocks" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111111111111:role/x" }
  }
  mock_resource "aws_lb_target_group" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:111111111111:targetgroup/x/1" }
  }
  mock_resource "aws_service_discovery_service" {
    defaults = { arn = "arn:aws:servicediscovery:us-east-1:111111111111:service/srv-1" }
  }
  mock_resource "aws_acm_certificate" {
    defaults = { arn = "arn:aws:acm:us-east-1:111111111111:certificate/x", domain_validation_options = [] }
  }
}

variables {
  hosted_zone_name = "example.com"
  domain_name      = "mocks.example.com"
  allowed_cidrs    = ["203.0.113.0/24"]
  image_tag        = "abc123"
}

run "one_task_per_service_with_compose_style_wiring" {
  command = plan

  assert {
    condition     = length(aws_ecs_service.svc) == 8
    error_message = "seven mocks plus Desk Chat"
  }
  assert {
    condition     = alltrue([for s in aws_ecs_service.svc : s.desired_count == 1 && s.deployment_maximum_percent == 100])
    error_message = "in-memory schedulers need exactly one task, never two during a deploy"
  }
  assert {
    condition     = local.public_urls["ib"] == "https://ib.mocks.example.com" && local.fix_host == "fix.mocks.example.com"
    error_message = "hostnames"
  }
  assert {
    condition = jsondecode(aws_ecs_task_definition.svc["ngt"].container_definitions)[0].portMappings == [
      { containerPort = 8030, protocol = "tcp" }, { containerPort = 9878, protocol = "tcp" },
    ]
    error_message = "ngt exposes REST and FIX"
  }
  assert {
    condition = contains(
      jsondecode(aws_ecs_task_definition.svc["control"].container_definitions)[0].environment,
      { name = "MOCK_SFP_URL", value = "http://sfp.mocks.local:8050" },
    )
    error_message = "control fans out to services by Cloud Map name"
  }
  assert {
    condition     = !can(jsondecode(aws_ecs_task_definition.svc["chat"].container_definitions)[0].secrets)
    error_message = "Desk Chat (static nginx) gets no secrets"
  }
  assert {
    condition     = endswith(jsondecode(aws_ecs_task_definition.svc["chat"].container_definitions)[0].image, ":abc123")
    error_message = "image tag"
  }
  assert {
    condition     = length(aws_nat_gateway.this) == 0 && alltrue([for s in aws_ecs_service.svc : s.network_configuration[0].assign_public_ip])
    error_message = "default: no NAT, public IPs"
  }
}

run "paused_and_nat" {
  command = plan
  variables {
    running     = false
    nat_gateway = true
  }
  assert {
    condition     = alltrue([for s in aws_ecs_service.svc : s.desired_count == 0 && !s.network_configuration[0].assign_public_ip])
    error_message = "running=false scales to zero; NAT puts tasks in private subnets"
  }
}

run "rejects_open_internet" {
  command = plan
  variables {
    allowed_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.allowed_cidrs]
}
