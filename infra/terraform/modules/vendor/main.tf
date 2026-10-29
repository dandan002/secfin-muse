# One external system: its own VPC, Postgres, ECS service, load balancer, certificate and DNS name.
# It shares nothing at runtime with the other mocks; they meet only over public HTTPS (and FIX),
# the way a desk reaches its vendors.
#
# The mock keeps its scheduler, WebSocket hub, FIX sessions and faults in process memory, so it
# runs as exactly one task and deploys stop the old task before starting the new one.
#
# `running` splits the system in two. The base (VPC, security groups, certificate, image registry,
# logs, IAM) costs nothing and always exists. Everything billed by the hour (load balancers, NAT,
# the task and its public IP) exists only while running = true, so infra/up.sh and infra/down.sh
# are plain applies and a stopped system costs ~$0.

terraform {
  required_providers {
    aws    = { source = "hashicorp/aws" }
    random = { source = "hashicorp/random" }
  }
}

locals {
  nat = var.nat_eip_allocation_id != null
  on  = var.running ? 1 : 0

  # Every ECS service in this system: the mock itself, plus Desk Chat for mock-ib.
  apps = merge(
    { (var.service) = { port = var.port, hostname = var.hostname, health = "/health", mock = true } },
    { for k, h in { chat = try(var.desk_chat.hostname, "") } : k => { port = 80, hostname = h, health = "/", mock = false } if var.desk_chat != null },
  )
  hostnames = concat([for a in local.apps : a.hostname], var.fix == null ? [] : [var.fix.hostname])
}

data "aws_availability_zones" "available" {
  state = "available"
}

# -- network ----------------------------------------------------------------------------------
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

resource "aws_subnet" "private" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.this.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 10 + count.index)
  tags              = { Name = "${var.name}-private-${local.azs[count.index]}" }
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

resource "aws_nat_gateway" "this" {
  count         = local.nat ? local.on : 0
  allocation_id = var.nat_eip_allocation_id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = var.name }
  depends_on    = [aws_internet_gateway.this]
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  dynamic "route" {
    for_each = aws_nat_gateway.this
    content {
      cidr_block     = "0.0.0.0/0"
      nat_gateway_id = route.value.id
    }
  }
  tags = { Name = "${var.name}-private" }
}

resource "aws_route_table_association" "private" {
  count          = length(local.azs)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

resource "aws_security_group" "lb" {
  name        = "${var.name}-lb"
  description = "Public endpoints, allowlisted"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_ingress_rule" "lb" {
  for_each = {
    for pair in setproduct(var.ingress_cidrs, concat([80, 443], var.fix == null ? [] : [var.fix.port])) :
    "${pair[0]}-${pair[1]}" => pair
  }
  security_group_id = aws_security_group.lb.id
  cidr_ipv4         = each.value[0]
  from_port         = each.value[1]
  to_port           = each.value[1]
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "lb" {
  security_group_id = aws_security_group.lb.id
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "-1"
}

resource "aws_security_group" "tasks" {
  name        = "${var.name}-tasks"
  description = "From this system load balancers only, all egress" # EC2 rejects apostrophes and semicolons
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_ingress_rule" "tasks" {
  for_each                     = toset([for p in concat([for a in local.apps : a.port], var.fix == null ? [] : [var.fix.port]) : tostring(p)])
  security_group_id            = aws_security_group.tasks.id
  referenced_security_group_id = aws_security_group.lb.id
  from_port                    = tonumber(each.value)
  to_port                      = tonumber(each.value)
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "tasks" {
  security_group_id = aws_security_group.tasks.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# -- database ---------------------------------------------------------------------------------
# The system's own Postgres runs as a second container in its task (see the task definition). A
# session is ~1000 trades, so there is nothing to provision and nothing billed while stopped: when
# the task stops, Postgres dumps itself to the snapshot in S3, and the next task restores it before
# the mock connects (infra/postgres). Only the mock reaches it, over the task's loopback; the tasks
# security group never opens 5432.
resource "random_password" "db" {
  length  = 32
  special = false # it goes into a URL
}

# -- edge -------------------------------------------------------------------------------------
resource "aws_acm_certificate" "this" {
  domain_name               = var.hostname
  subject_alternative_names = [for a in local.apps : a.hostname if a.hostname != var.hostname]
  validation_method         = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for o in aws_acm_certificate.this.domain_validation_options : o.domain_name => o
  }
  zone_id         = var.zone_id
  name            = each.value.resource_record_name
  type            = each.value.resource_record_type
  records         = [each.value.resource_record_value]
  ttl             = 300
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "this" {
  certificate_arn         = aws_acm_certificate.this.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# The certificate stays while stopped (it's free and re-validating it would slow every start).
resource "aws_lb" "alb" {
  count              = local.on
  name               = var.name
  load_balancer_type = "application"
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.lb.id]
  idle_timeout       = 900 # Desk Chat and the bot/inbox streams hold quiet WebSockets open
}

resource "aws_lb_target_group" "app" {
  for_each             = var.running ? local.apps : {}
  name                 = each.key == var.service ? var.name : "${var.name}-${each.key}"
  port                 = each.value.port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.this.id
  deregistration_delay = 5 # one task; nothing to drain to

  health_check {
    path                = each.value.health
    matcher             = "200"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "https" {
  count             = local.on
  load_balancer_arn = aws_lb.alb[0].arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.this.certificate_arn

  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "application/json"
      message_body = "{\"error\":\"unknown host\"}"
      status_code  = "404"
    }
  }
}

resource "aws_lb_listener" "http" {
  count             = local.on
  load_balancer_arn = aws_lb.alb[0].arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener_rule" "app" {
  for_each     = aws_lb_target_group.app
  listener_arn = aws_lb_listener.https[0].arn
  priority     = 100 + index(keys(local.apps), each.key)

  condition {
    host_header {
      values = [local.apps[each.key].hostname]
    }
  }

  action {
    type             = "forward"
    target_group_arn = each.value.arn
  }
}

resource "aws_route53_record" "app" {
  for_each = var.running ? local.apps : {}
  zone_id  = var.zone_id
  name     = each.value.hostname
  type     = "A"

  alias {
    name                   = aws_lb.alb[0].dns_name
    zone_id                = aws_lb.alb[0].zone_id
    evaluate_target_health = false
  }
}

resource "aws_lb" "fix" {
  count              = var.fix == null ? 0 : local.on
  name               = "${var.name}-tcp"
  load_balancer_type = "network"
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.lb.id]
}

resource "aws_lb_target_group" "fix" {
  count                = length(aws_lb.fix)
  name                 = "${var.name}-tcp"
  port                 = var.fix.port
  protocol             = "TCP"
  target_type          = "ip"
  vpc_id               = aws_vpc.this.id
  deregistration_delay = 5

  health_check {
    protocol = "HTTP"
    port     = tostring(var.port)
    path     = "/health"
    interval = 15
  }
}

resource "aws_lb_listener" "fix" {
  count             = length(aws_lb.fix)
  load_balancer_arn = aws_lb.fix[0].arn
  port              = var.fix.port
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.fix[0].arn
  }
}

resource "aws_route53_record" "fix" {
  count   = length(aws_lb.fix)
  zone_id = var.zone_id
  name    = var.fix.hostname
  type    = "A"

  alias {
    name                   = aws_lb.fix[0].dns_name
    zone_id                = aws_lb.fix[0].zone_id
    evaluate_target_health = false
  }
}

# -- images: this system's own registry ----------------------------------------------------------
locals {
  repos = merge({ mock = var.service }, var.desk_chat == null ? {} : { desk-chat = "${var.service}-desk-chat" })
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
    resources = [for s in var.secrets : s.valueFrom]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

# The mocks call no AWS APIs. The task role enables `aws ecs execute-command`, and lets Postgres
# read and write this system's snapshot.
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
  # Container fields only the Python mock gets (Desk Chat is a static nginx image).
  mock_container = {
    (var.service) = {
      image = "${aws_ecr_repository.this["mock"].repository_url}:${var.image_tag}"
      command = [
        "uvicorn", var.app_module, "--host", "0.0.0.0", "--port", tostring(var.port),
        "--proxy-headers", "--forwarded-allow-ips", "*", # behind the ALB: keep https in redirects
      ]
      environment = concat(var.environment, [
        { name = "PORT", value = tostring(var.port) },
        { name = "MOCK_DATABASE_URL", value = "postgresql+asyncpg://mock:${random_password.db.result}@127.0.0.1:5432/mock" },
      ])
      secrets = var.secrets
      portMappings = concat(
        [{ containerPort = var.port, protocol = "tcp" }],
        var.fix == null ? [] : [{ containerPort = var.fix.port, protocol = "tcp" }],
      )
      dependsOn = [{ containerName = "postgres", condition = "HEALTHY" }]
    }
  }

  # The mock's own database, in the same task (so on the same loopback). ECS stops the mock first
  # (it depends on Postgres), then gives Postgres up to stopTimeout to save its snapshot.
  postgres = {
    name        = "postgres"
    essential   = true
    image       = var.postgres_image
    stopTimeout = 120 # the Fargate maximum; a session's dump takes seconds
    environment = concat([
      { name = "POSTGRES_USER", value = "mock" },
      { name = "POSTGRES_PASSWORD", value = random_password.db.result },
      { name = "POSTGRES_DB", value = "mock" },
      { name = "AWS_REGION", value = var.region },
      ], var.snapshot == null ? [] : [
      { name = "SNAPSHOT_URL", value = "s3://${var.snapshot.bucket}/${var.snapshot.key}" },
    ])
    healthCheck = {
      # Over TCP: during the restore Postgres listens only on its socket, and the mock must wait.
      command     = ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U mock -d mock"]
      interval    = 5
      timeout     = 3
      retries     = 10
      startPeriod = 60 # covers the restore
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app[var.service].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "postgres"
      }
    }
  }
}

# Task definitions only exist while running, so stopping a system needs no image tag.
resource "aws_ecs_task_definition" "app" {
  for_each                 = var.running ? local.apps : {}
  family                   = "${var.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256  # ~100 MB RSS per mock after the full demo
  memory                   = 1024 # the smallest Fargate size with room for the mock and Postgres
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode(concat([merge(
    {
      name         = each.key
      essential    = true
      image        = "${try(aws_ecr_repository.this["desk-chat"].repository_url, "")}:${var.image_tag}"
      portMappings = [{ containerPort = each.value.port, protocol = "tcp" }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.app[each.key].name
          awslogs-region        = var.region
          awslogs-stream-prefix = each.key
        }
      }
    },
    try(local.mock_container[each.key], {}),
  )], each.value.mock ? [local.postgres] : []))
}

resource "aws_ecs_service" "app" {
  for_each               = aws_ecs_task_definition.app
  name                   = each.key
  cluster                = aws_ecs_cluster.this.id
  task_definition        = each.value.arn
  launch_type            = "FARGATE"
  desired_count          = 1
  enable_execute_command = true

  # Exactly one task, ever: stop the old one before starting the new one, so two schedulers
  # never fire the same venue fills, persona replies or settlements.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  # First boot seeds 2,000 instruments and the ledgers before /health answers.
  health_check_grace_period_seconds = 180

  network_configuration {
    subnets          = local.nat ? aws_subnet.private[*].id : aws_subnet.public[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = !local.nat
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app[each.key].arn
    container_name   = each.key
    container_port   = local.apps[each.key].port
  }

  dynamic "load_balancer" {
    for_each = local.apps[each.key].mock ? aws_lb_target_group.fix : []
    content {
      target_group_arn = load_balancer.value.arn
      container_name   = each.key
      container_port   = var.fix.port
    }
  }

  depends_on = [aws_lb_listener_rule.app, aws_lb_listener.fix, aws_nat_gateway.this]
}
