# Offline plans of one system against a mocked AWS provider:
#   cd infra/terraform/modules/vendor && terraform init && terraform test

mock_provider "aws" {
  override_during = plan # mock computed values at plan time, as the real provider knows these

  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-2a", "us-east-2b", "us-east-2c"] }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111111111111:role/x" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = { repository_url = "111111111111.dkr.ecr.us-east-2.amazonaws.com/repo" }
  }
}

override_resource {
  target          = random_password.db
  override_during = plan
  values          = { result = "testpassword" }
}

variables {
  name                   = "ld-demo-ngt"
  service                = "ngt"
  region                 = "us-east-2"
  hostname               = "mock-ngt.example.com"
  tunnel_token_parameter = "arn:aws:ssm:us-east-2:111111111111:parameter/loandesk-demo/tunnel/ngt"
  repository_prefix      = "loandesk-demo"
  image_tag              = "abc123"
  port                   = 8030
  app_module             = "mock_ngt.app:app"
  environment            = [{ name = "MOCK_SEED", value = "20261027" }]
  secrets                = [{ name = "MOCK_ADMIN_TOKEN", valueFrom = "arn:aws:ssm:us-east-2:111111111111:parameter/admin" }]
  postgres_image         = "111111111111.dkr.ecr.us-east-2.amazonaws.com/loandesk-demo/postgres:pg1"
  snapshot               = { bucket = "snapshots", key = "ngt.sql.gz" }
}

run "system_behind_its_tunnel" {
  command = plan

  assert {
    condition = (
      [for c in jsondecode(aws_ecs_task_definition.app["ngt"].container_definitions) : c.name] == ["ngt", "postgres", "cloudflared"]
      && jsondecode(aws_ecs_task_definition.app["ngt"].container_definitions)[0].dependsOn == [{ containerName = "postgres", condition = "HEALTHY" }]
      && anytrue([for e in jsondecode(aws_ecs_task_definition.app["ngt"].container_definitions)[0].environment :
      e.name == "MOCK_DATABASE_URL" && strcontains(e.value, "@127.0.0.1:5432/app")])
    )
    error_message = "its own Postgres, in the task, started first and reached over loopback; then the tunnel"
  }
  assert {
    condition = (
      jsondecode(aws_ecs_task_definition.app["ngt"].container_definitions)[2].secrets == [{ name = "TUNNEL_TOKEN", valueFrom = "arn:aws:ssm:us-east-2:111111111111:parameter/loandesk-demo/tunnel/ngt" }]
      && jsondecode(aws_ecs_task_definition.app["ngt"].container_definitions)[2].dependsOn == [{ containerName = "ngt", condition = "HEALTHY" }]
      && jsondecode(aws_ecs_task_definition.app["ngt"].container_definitions)[2].image == "cloudflare/cloudflared:2026.10.7"
    )
    error_message = "cloudflared carries the hostname in once the app is healthy"
  }
  assert {
    condition     = strcontains(jsondecode(aws_ecs_task_definition.app["ngt"].container_definitions)[0].healthCheck.command[1], "/health")
    error_message = "with no load balancer, ECS watches /health itself"
  }
  assert {
    condition     = aws_vpc_security_group_egress_rule.tasks.cidr_ipv4 == "0.0.0.0/0" && aws_vpc_security_group_egress_rule.tasks.ip_protocol == "-1"
    error_message = "out to Cloudflare, ECR and S3; the module defines no ingress rule at all"
  }
  assert {
    condition = alltrue([for s in aws_ecs_service.app :
      s.desired_count == 1 && s.deployment_maximum_percent == 100 && s.deployment_minimum_healthy_percent == 0
    && s.network_configuration[0].assign_public_ip && length(s.load_balancer) == 0])
    error_message = "exactly one task, never two during a deploy, no load balancer"
  }
  assert {
    condition     = [for s in jsondecode(aws_ecs_task_definition.app["ngt"].container_definitions)[0].secrets : s.name] == ["MOCK_ADMIN_TOKEN"]
    error_message = "the passed-in secrets"
  }
  assert {
    condition = (keys(aws_ecr_repository.this) == ["mock"] && aws_ecr_repository.this["mock"].name == "loandesk-demo/ngt"
    && endswith(jsondecode(aws_ecs_task_definition.app["ngt"].container_definitions)[0].image, "/repo:abc123"))
    error_message = "its own image registry, tagged with its project's version"
  }
  assert {
    condition     = output.urls == { ngt = "https://mock-ngt.example.com" }
    error_message = "its hostname"
  }
}

run "postgres_saves_and_restores_its_snapshot" {
  command = plan

  assert {
    condition = (
      jsondecode(aws_ecs_task_definition.app["ngt"].container_definitions)[1].image == "111111111111.dkr.ecr.us-east-2.amazonaws.com/loandesk-demo/postgres:pg1"
      && contains(jsondecode(aws_ecs_task_definition.app["ngt"].container_definitions)[1].environment, { name = "SNAPSHOT_URL", value = "s3://snapshots/ngt.sql.gz" })
      && jsondecode(aws_ecs_task_definition.app["ngt"].container_definitions)[1].stopTimeout == 120
    )
    error_message = "the snapshot image, its S3 key, and time to save on stop"
  }
  assert {
    condition     = strcontains(jsondecode(aws_ecs_task_definition.app["ngt"].container_definitions)[1].healthCheck.command[1], "-h 127.0.0.1")
    error_message = "healthy only once it takes TCP, i.e. after the restore"
  }
  assert {
    condition     = length(aws_iam_role_policy.task_snapshot) == 1
    error_message = "the task may read and write its snapshot"
  }
}

run "without_a_snapshot" {
  command = plan
  variables {
    snapshot = null
  }
  assert {
    condition = (length(aws_iam_role_policy.task_snapshot) == 0
    && !anytrue([for e in jsondecode(aws_ecs_task_definition.app["ngt"].container_definitions)[1].environment : e.name == "SNAPSHOT_URL"]))
    error_message = "plain Postgres, empty every start"
  }
}

run "the_desk_reads_its_own_database_variable" {
  command = plan
  variables {
    service          = "desk"
    port             = 8100
    app_module       = "loandesk.app:app"
    database_url_env = "LOANDESK_DATABASE_URL"
    cpu              = 512
  }
  assert {
    condition = (anytrue([for e in jsondecode(aws_ecs_task_definition.app["desk"].container_definitions)[0].environment : e.name == "LOANDESK_DATABASE_URL"])
    && aws_ecs_task_definition.app["desk"].cpu == "512")
    error_message = "LOANDESK_DATABASE_URL, and more CPU"
  }
}

run "with_a_ui" {
  command = plan
  variables {
    ui = { name = "chat", hostname = "mock-chat.example.com", tunnel_token_parameter = "arn:aws:ssm:us-east-2:111111111111:parameter/loandesk-demo/tunnel/chat" }
  }

  assert {
    condition     = keys(aws_ecs_service.app) == ["chat", "ngt"] && output.urls.chat == "https://mock-chat.example.com"
    error_message = "a second service"
  }
  assert {
    condition = (aws_ecr_repository.this["ui"].name == "loandesk-demo/ngt-chat"
      && [for c in jsondecode(aws_ecs_task_definition.app["chat"].container_definitions) : c.name] == ["chat", "cloudflared"]
      && !can(jsondecode(aws_ecs_task_definition.app["chat"].container_definitions)[0].secrets)
    && jsondecode(aws_ecs_task_definition.app["chat"].container_definitions)[1].secrets[0].valueFrom == "arn:aws:ssm:us-east-2:111111111111:parameter/loandesk-demo/tunnel/chat")
    error_message = "the UI: static image, no secrets, no database, its own tunnel"
  }
}

run "stopped" {
  command = plan
  variables {
    running = false
  }
  assert {
    condition     = (length(aws_ecs_service.app) + length(aws_ecs_task_definition.app)) == 0
    error_message = "nothing billed by the hour while stopped"
  }
  assert {
    condition     = keys(aws_ecr_repository.this) == ["mock"] && aws_vpc.this.cidr_block == "10.40.0.0/16"
    error_message = "the free base stays, so the next start is quick"
  }
}
