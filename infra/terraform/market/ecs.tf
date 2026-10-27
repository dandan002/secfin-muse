resource "aws_ecr_repository" "mocks" {
  name                 = "${local.name}/mocks"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "desk_chat" {
  name                 = "${local.name}/desk-chat"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "keep_recent" {
  for_each   = { mocks = aws_ecr_repository.mocks.name, desk_chat = aws_ecr_repository.desk_chat.name }
  repository = each.value
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 20 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_ecs_cluster" "this" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_service_discovery_private_dns_namespace" "this" {
  name = local.namespace
  vpc  = aws_vpc.this.id
}

resource "aws_service_discovery_service" "mock" {
  for_each = local.mocks
  name     = each.key

  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.this.id
    routing_policy = "MULTIVALUE"
    dns_records {
      type = "A"
      ttl  = 10
    }
  }
}

resource "aws_cloudwatch_log_group" "svc" {
  for_each          = local.services
  name              = "/ecs/${local.name}/${each.key}"
  retention_in_days = var.log_retention_days
}

# -- IAM ------------------------------------------------------------------------------------
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
  name_prefix        = "${local.name}-exec-"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_secrets" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = concat(
      [aws_secretsmanager_secret.db_url.arn, aws_secretsmanager_secret.admin_token.arn],
      aws_secretsmanager_secret.llm_key[*].arn,
    )
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

# The mocks call no AWS APIs; the task role only enables `aws ecs execute-command`.
resource "aws_iam_role" "task" {
  name_prefix        = "${local.name}-task-"
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

# -- tasks and services ---------------------------------------------------------------------
locals {
  # Container fields only the Python mocks get (Desk Chat is a static nginx image).
  mock_container = {
    for k, m in local.mocks : k => {
      command = [
        "uvicorn", m.app, "--host", "0.0.0.0", "--port", tostring(m.port),
        "--proxy-headers", "--forwarded-allow-ips", "*", # behind the ALB: keep https in redirects
      ]
      environment = concat(
        local.mock_env,
        [{ name = "PORT", value = tostring(m.port) }],
        lookup(local.service_env, k, []),
      )
      secrets = local.mock_secrets
    }
  }
}

resource "aws_ecs_task_definition" "svc" {
  for_each                 = local.services
  family                   = "${local.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256 # ~100 MB RSS per mock after the full demo
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([merge(
    {
      name      = each.key
      essential = true
      image     = "${each.value.mock ? aws_ecr_repository.mocks.repository_url : aws_ecr_repository.desk_chat.repository_url}:${var.image_tag}"
      portMappings = concat(
        [{ containerPort = each.value.port, protocol = "tcp" }],
        each.key == "ngt" ? [{ containerPort = local.fix_port, protocol = "tcp" }] : [],
      )
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.svc[each.key].name
          awslogs-region        = var.region
          awslogs-stream-prefix = each.key
        }
      }
    },
    try(local.mock_container[each.key], {}),
  )])
}

resource "aws_ecs_service" "svc" {
  for_each               = local.services
  name                   = each.key
  cluster                = aws_ecs_cluster.this.id
  task_definition        = aws_ecs_task_definition.svc[each.key].arn
  launch_type            = "FARGATE"
  desired_count          = var.running ? 1 : 0
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
    subnets          = local.task_subnets
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = !var.nat_gateway
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.svc[each.key].arn
    container_name   = each.key
    container_port   = each.value.port
  }

  dynamic "load_balancer" {
    for_each = each.key == "ngt" ? [1] : []
    content {
      target_group_arn = aws_lb_target_group.fix.arn
      container_name   = each.key
      container_port   = local.fix_port
    }
  }

  dynamic "service_registries" {
    for_each = each.value.mock ? [aws_service_discovery_service.mock[each.key].arn] : []
    content {
      registry_arn = service_registries.value
    }
  }

  tags       = { module = each.key }
  depends_on = [aws_lb_listener_rule.svc, aws_lb_listener.fix, aws_rds_cluster_instance.this]
}
