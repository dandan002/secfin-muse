# Offline plans of one external system against a mocked AWS provider:
#   cd infra/terraform/modules/vendor && terraform init && terraform test

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
  mock_resource "aws_ecr_repository" {
    defaults = { repository_url = "111111111111.dkr.ecr.us-east-1.amazonaws.com/repo" }
  }
  mock_resource "aws_acm_certificate" {
    defaults = { arn = "arn:aws:acm:us-east-1:111111111111:certificate/x", domain_validation_options = [] }
  }
}

variables {
  name              = "ld-demo-ngt"
  service           = "ngt"
  region            = "us-east-1"
  hostname          = "ngt.mocks.example.com"
  zone_id           = "Z123"
  ingress_cidrs     = ["203.0.113.0/24", "198.51.100.7/32"]
  repository_prefix = "loandesk-demo"
  image_tag         = "abc123"
  port              = 8030
  app_module        = "mock_ngt.app:app"
  environment       = [{ name = "MOCK_SEED", value = "20261027" }]
  secrets           = [{ name = "MOCK_ADMIN_TOKEN", valueFrom = "arn:aws:secretsmanager:us-east-1:111111111111:secret:admin" }]
  fix               = { port = 9878, hostname = "fix.mocks.example.com" }
}

run "system_with_fix" {
  command = plan

  assert {
    condition     = aws_db_instance.this.identifier == "ld-demo-ngt" && aws_db_instance.this.db_name == "mock" && !aws_db_instance.this.publicly_accessible
    error_message = "its own private database"
  }
  assert {
    condition     = length(aws_lb.fix) == 1 && length(aws_nat_gateway.this) == 0 && keys(aws_ecs_service.app) == ["ngt"]
    error_message = "one service, NLB for FIX, no NAT"
  }
  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.lb) == 6 # 2 CIDRs x (80, 443, 9878)
    error_message = "allowlisted ports"
  }
  assert {
    condition = alltrue([for s in aws_ecs_service.app :
    s.desired_count == 1 && s.deployment_maximum_percent == 100 && s.deployment_minimum_healthy_percent == 0 && s.network_configuration[0].assign_public_ip])
    error_message = "exactly one task, never two during a deploy"
  }
  assert {
    condition = jsondecode(aws_ecs_task_definition.app["ngt"].container_definitions)[0].portMappings == [
      { containerPort = 8030, protocol = "tcp" }, { containerPort = 9878, protocol = "tcp" },
    ]
    error_message = "REST and FIX ports"
  }
  assert {
    condition = [for s in jsondecode(aws_ecs_task_definition.app["ngt"].container_definitions)[0].secrets : s.name] == [
      "MOCK_DATABASE_URL", "MOCK_ADMIN_TOKEN",
    ]
    error_message = "own database URL plus the passed-in secrets"
  }
  assert {
    condition = (keys(aws_ecr_repository.this) == ["mock"] && aws_ecr_repository.this["mock"].name == "loandesk-demo/ngt"
    && endswith(jsondecode(aws_ecs_task_definition.app["ngt"].container_definitions)[0].image, "/repo:abc123"))
    error_message = "its own image registry, tagged with its project's version"
  }
}

run "behind_nat_with_fixed_egress" {
  command = plan
  variables {
    fix                   = null
    nat_eip_allocation_id = "eipalloc-0control"
  }

  assert {
    condition     = aws_nat_gateway.this[0].allocation_id == "eipalloc-0control" && !aws_ecs_service.app["ngt"].network_configuration[0].assign_public_ip
    error_message = "private tasks behind the given EIP"
  }
  assert {
    condition     = length(aws_lb.fix) == 0 && length(aws_vpc_security_group_ingress_rule.lb) == 4
    error_message = "no FIX"
  }
}

run "with_desk_chat" {
  command = plan
  variables {
    fix       = null
    desk_chat = { hostname = "chat.mocks.example.com" }
  }

  assert {
    condition     = keys(aws_ecs_service.app) == ["chat", "ngt"] && aws_acm_certificate.this.subject_alternative_names == toset(["chat.mocks.example.com"])
    error_message = "second service on the same ALB and cert"
  }
  assert {
    condition = (aws_ecr_repository.this["desk-chat"].name == "loandesk-demo/ngt-desk-chat"
    && !can(jsondecode(aws_ecs_task_definition.app["chat"].container_definitions)[0].secrets))
    error_message = "Desk Chat: static image, no secrets"
  }
}

run "parked" {
  command = plan
  variables {
    running = false
  }
  assert {
    condition     = aws_ecs_service.app["ngt"].desired_count == 0
    error_message = "running=false scales to zero"
  }
}
