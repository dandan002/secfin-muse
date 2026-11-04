# One system: its own VPC, Postgres, ECS service and image registry, reached through its own
# Cloudflare Tunnel. It shares nothing at runtime with the others; they meet only over public HTTPS,
# the way a desk reaches its vendors. The mocks and the desk (the LoanDesk platform) both run on it.
#
# No load balancer, certificate or DNS here: a cloudflared container in the task dials out to
# Cloudflare, which terminates HTTPS for the system's hostname and sends requests down the tunnel to
# the app on the task's loopback. The task opens no inbound port at all. (The tunnels, DNS records
# and tunnel tokens are the edge root's; this module reads a token from SSM.)
#
# Each system keeps its scheduler, WebSocket hub and faults in process memory, so it runs as exactly
# one task and deploys stop the old task before starting the new one.
#
# `running` splits the system in two. The base (VPC, security group, image registry, logs, IAM)
# costs nothing and always exists. The task and its service exist only while running = true, so
# infra/up.sh and infra/down.sh are plain applies and a stopped system costs ~$0.

terraform {
  required_providers {
    aws    = { source = "hashicorp/aws" }
    random = { source = "hashicorp/random" }
  }
}

locals {
  # Every ECS service in this system: the app itself, plus its static UI if it has one (the desk's web
  # app, mock-ib's Desk Chat).
  apps = merge(
    { (var.service) = { port = var.port, hostname = var.hostname, tunnel = var.tunnel_token_parameter, python = true } },
    var.ui == null ? {} : { (var.ui.name) = { port = 80, hostname = var.ui.hostname, tunnel = var.ui.tunnel_token_parameter, python = false } },
  )
}

data "aws_availability_zones" "available" {
  state = "available"
}

# -- network ----------------------------------------------------------------------------------
# Public subnets only: the task needs a public IP to reach Cloudflare, ECR and S3 (no NAT to pay
# for), and its security group lets nothing in.
locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}

resource "aws_subnet" "public" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.this.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index)
  tags              = { Name = "${var.name}-public-${local.azs[count.index]}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = length(local.azs)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "tasks" {
  name        = "${var.name}-tasks"
  description = "No inbound: requests arrive through the Cloudflare Tunnel the task dials out on"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_egress_rule" "tasks" {
  security_group_id = aws_security_group.tasks.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# -- database ---------------------------------------------------------------------------------
# The system's own Postgres runs as a container in its task (see the task definition). A session is
# ~1000 trades, so there is nothing to provision and nothing billed while stopped: when the task
# stops, Postgres dumps itself to the snapshot in S3, and the next task restores it before the app
# connects (infra/postgres). Only the app reaches it, over the task's loopback.
resource "random_password" "db" {
  length  = 32
  special = false # it goes into a URL
}

# -- images: this system's own registry ----------------------------------------------------------
locals {
  repos = merge({ mock = var.service }, var.ui == null ? {} : { ui = "${var.service}-${var.ui.name}" })
}

resource "aws_ecr_repository" "this" {
  for_each             = local.repos
  name                 = "${var.repository_prefix}/${each.value}"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "keep_recent" {
  for_each   = aws_ecr_repository.this
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 20 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }
      action       = { type = "expire" }
    }]
  })
}

# -- compute ----------------------------------------------------------------------------------
resource "aws_ecs_cluster" "this" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "disabled" # billed per metric; the service's basic CPU/memory metrics are enough here
  }
}

resource "aws_cloudwatch_log_group" "app" {
  for_each          = local.apps
  name              = "/ecs/${var.name}/${each.key}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name_prefix        = "${var.name}-exec-"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_secrets" {
  statement {
    actions   = ["ssm:GetParameters"]
    resources = distinct(concat([for s in var.secrets : s.valueFrom], [for a in local.apps : a.tunnel]))
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

# The apps call no AWS APIs. The task role enables `aws ecs execute-command`, and lets Postgres read
# and write this system's snapshot.
resource "aws_iam_role" "task" {
  name_prefix        = "${var.name}-task-"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "exec_command" {
  statement {
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "task_exec_command" {
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.exec_command.json
}

data "aws_iam_policy_document" "snapshot" {
  count = var.snapshot == null ? 0 : 1
  statement {
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["arn:aws:s3:::${var.snapshot.bucket}/${var.snapshot.key}"]
  }
  statement {
    # Without it a missing snapshot reads as 403 rather than 404 (the first start).
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.snapshot.bucket}"]
    condition {
      test     = "StringEquals"
      variable = "s3:prefix"
      values   = [var.snapshot.key]
    }
  }
}

resource "aws_iam_role_policy" "task_snapshot" {
  count  = length(data.aws_iam_policy_document.snapshot)
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.snapshot[0].json
}

locals {
  logs = {
    for k in keys(local.apps) : k => {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app[k].name
        awslogs-region        = var.region
        awslogs-stream-prefix = k
      }
    }
  }

  # Container fields only the Python app gets (the UI is a static nginx image).
  python_container = {
    (var.service) = {
      image = "${aws_ecr_repository.this["mock"].repository_url}:${var.image_tag}"
      command = [
        "uvicorn", var.app_module, "--host", "0.0.0.0", "--port", tostring(var.port),
        "--proxy-headers", "--forwarded-allow-ips", "*", # behind Cloudflare: keep https in redirects
      ]
      environment = concat(var.environment, [
        { name = "PORT", value = tostring(var.port) },
        { name = var.database_url_env, value = "postgresql+asyncpg://app:${random_password.db.result}@127.0.0.1:5432/app" },
      ])
      secrets      = var.secrets
      portMappings = [{ containerPort = var.port, protocol = "tcp" }]
      dependsOn    = [{ containerName = "postgres", condition = "HEALTHY" }]
      # Nothing else watches the app (no load balancer), so ECS restarts it if /health stops answering. The
      # slim image has no curl. First boot seeds the universe before /health answers.
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/health', timeout=3)\""]
        interval    = 15
        timeout     = 5
        retries     = 4
        startPeriod = 180
      }
    }
  }

  # The system's own database, in the same task (so on the same loopback). ECS stops the app first (it
  # depends on Postgres), then gives Postgres up to stopTimeout to save its snapshot.
  postgres = {
    name        = "postgres"
    essential   = true
    image       = var.postgres_image
    stopTimeout = 120 # the Fargate maximum; a session's dump takes seconds
    environment = concat([
      { name = "POSTGRES_USER", value = "app" },
      { name = "POSTGRES_PASSWORD", value = random_password.db.result },
      { name = "POSTGRES_DB", value = "app" },
      { name = "AWS_REGION", value = var.region },
      ], var.snapshot == null ? [] : [
      { name = "SNAPSHOT_URL", value = "s3://${var.snapshot.bucket}/${var.snapshot.key}" },
    ])
    healthCheck = {
      # Over TCP: during the restore Postgres listens only on its socket, and the app must wait.
      command     = ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U app -d app"]
      interval    = 5
      timeout     = 3
      retries     = 10
      startPeriod = 60 # covers the restore
    }
    logConfiguration = local.logs[var.service]
  }

  # The way in: dials out to Cloudflare and carries this service's hostname to the app on loopback
  # (the tunnel's ingress rules live in Cloudflare, see edge). It starts once the app is up, so the
  # hostname never routes to a task that can't answer yet.
  cloudflared = {
    for k, a in local.apps : k => {
      name        = "cloudflared"
      essential   = true
      image       = var.cloudflared_image
      command     = ["tunnel", "--no-autoupdate", "--metrics", "127.0.0.1:20241", "run"]
      secrets     = [{ name = "TUNNEL_TOKEN", valueFrom = a.tunnel }]
      dependsOn   = [{ containerName = k, condition = a.python ? "HEALTHY" : "START" }]
      stopTimeout = 10
      logConfiguration = merge(local.logs[k], {
        options = merge(local.logs[k].options, { awslogs-stream-prefix = "cloudflared" })
      })
    }
  }
}

# Task definitions only exist while running, so stopping a system needs no image tag.
resource "aws_ecs_task_definition" "app" {
  for_each                 = var.running ? local.apps : {}
  family                   = "${var.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.python ? var.cpu : 256
  memory                   = each.value.python ? var.memory : 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode(concat([merge(
    {
      name             = each.key
      essential        = true
      image            = "${try(aws_ecr_repository.this["ui"].repository_url, "")}:${var.image_tag}"
      portMappings     = [{ containerPort = each.value.port, protocol = "tcp" }]
      logConfiguration = local.logs[each.key]
    },
    try(local.python_container[each.key], {}),
  )], each.value.python ? [local.postgres] : [], [local.cloudflared[each.key]]))
}

resource "aws_ecs_service" "app" {
  for_each               = aws_ecs_task_definition.app
  name                   = each.key
  cluster                = aws_ecs_cluster.this.id
  task_definition        = each.value.arn
  launch_type            = "FARGATE"
  desired_count          = 1
  enable_execute_command = true

  # Exactly one task, ever: stop the old one before starting the new one, so two schedulers never
  # fire the same venue fills, persona replies or settlements.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true
  }
}
