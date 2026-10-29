# Auto-stop: two triggers, one stop path. Both start a CodeBuild job that runs `infra/down.sh <env>`
# against the copy of infra/ that up.sh uploads, so systems stop even when no laptop is awake.
#
#   session limit   up.sh --for 3h  ->  one-off EventBridge Scheduler schedule  ->  CodeBuild
#   credit guard    Budget (spend before credits) > credit_guard_usd  ->  SNS  ->  Lambda  ->  CodeBuild
#
# down.sh only removes what's billed by the hour; it never touches data that matters.

locals {
  autostop_name   = "${local.prefix}-autostop"
  autostop_source = "autostop/${local.env}/infra.zip" # up.sh writes it next to the state
  terraform_cli   = "1.17.0"                          # what down.sh runs in the job
}

data "aws_caller_identity" "this" {}

data "aws_iam_policy_document" "assume" {
  for_each = toset(["codebuild", "scheduler", "lambda"])
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["${each.key}.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.this.account_id]
    }
  }
}

# -- the stop job -----------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "autostop" {
  name              = "/codebuild/${local.autostop_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "autostop" {
  name               = local.autostop_name
  assume_role_policy = data.aws_iam_policy_document.assume["codebuild"].json
}

# Terraform refreshes every resource in a system's state before it removes the hourly ones, so the
# job reads everything and writes only what down.sh deletes.
resource "aws_iam_role_policy_attachment" "autostop_read" {
  role       = aws_iam_role.autostop.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "autostop" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.autostop.arn}:*"]
  }
  statement {
    sid       = "StateAndSource"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${var.state_bucket}/*"]
  }
  statement {
    sid       = "StateList"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.state_bucket}"]
  }
  statement {
    sid = "StopHourlyResources"
    actions = [
      "elasticloadbalancing:DeleteLoadBalancer",
      "elasticloadbalancing:DeleteListener",
      "elasticloadbalancing:DeleteRule",
      "elasticloadbalancing:DeleteTargetGroup",
      "elasticloadbalancing:DeregisterTargets",
      "ec2:DeleteNatGateway",
      "ec2:DeleteRouteTable", # the private route table points at the NAT, so it goes with it
      "ec2:DisassociateRouteTable",
      "ec2:DisassociateAddress", # control's egress IP, released once everything is stopped
      "ec2:ReleaseAddress",
      "ecs:UpdateService",
      "ecs:DeleteService",
      "ecs:DeregisterTaskDefinition",
      "ecs:DeleteTaskDefinitions",
    ]
    resources = ["*"] # the project holds only this simulator
  }
  statement {
    sid       = "DropDnsRecords"
    actions   = ["route53:ChangeResourceRecordSets"]
    resources = ["arn:aws:route53:::hostedzone/${data.aws_route53_zone.this.zone_id}"]
  }
}

data "aws_route53_zone" "this" {
  name         = var.hosted_zone_name
  private_zone = false
}

resource "aws_iam_role_policy" "autostop" {
  role   = aws_iam_role.autostop.id
  policy = data.aws_iam_policy_document.autostop.json
}

resource "aws_codebuild_project" "autostop" {
  name          = local.autostop_name
  description   = "Stops every running ${local.env} system (infra/down.sh ${local.env})."
  service_role  = aws_iam_role.autostop.arn
  build_timeout = 30

  source {
    type      = "S3"
    location  = "${var.state_bucket}/${local.autostop_source}"
    buildspec = <<-EOT
      version: 0.2
      phases:
        install:
          commands:
            - curl -fsSL -o /tmp/terraform.zip https://releases.hashicorp.com/terraform/${local.terraform_cli}/terraform_${local.terraform_cli}_linux_amd64.zip
            - unzip -o -q /tmp/terraform.zip -d /usr/local/bin && terraform version
        build:
          commands:
            - bash infra/down.sh ${local.env}
    EOT
  }

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type = "BUILD_GENERAL1_SMALL"
    image        = "aws/codebuild/amazonlinux-x86_64-standard:5.0"
    type         = "LINUX_CONTAINER"
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.autostop.name
    }
  }
}

# -- trigger 1: the session limit (up.sh creates the one-off schedule with this role) ------------
resource "aws_iam_role" "autostop_scheduler" {
  name               = "${local.autostop_name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.assume["scheduler"].json
}

data "aws_iam_policy_document" "start_stop_job" {
  statement {
    actions   = ["codebuild:StartBuild"]
    resources = [aws_codebuild_project.autostop.arn]
  }
}

resource "aws_iam_role_policy" "autostop_scheduler" {
  role   = aws_iam_role.autostop_scheduler.id
  policy = data.aws_iam_policy_document.start_stop_job.json
}

# -- trigger 2: the credit guard ----------------------------------------------------------------
# Spend is counted before credits: on the free plan, net spend stays $0 until the credits are gone.
# AWS Budgets refreshes a few times a day, so this is a backstop, not a meter.
resource "aws_budgets_budget" "credit_guard" {
  name         = "${local.prefix}-credit-guard"
  budget_type  = "COST"
  limit_amount = tostring(var.credit_guard_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_types {
    include_credit = false
    include_refund = false
  }

  dynamic "notification" {
    for_each = var.budget_email != "" ? [1] : []
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = 80
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.budget_email]
    }
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_sns_topic_arns  = [aws_sns_topic.credit_guard.arn]
    subscriber_email_addresses = var.budget_email != "" ? [var.budget_email] : []
  }

  depends_on = [aws_sns_topic_policy.credit_guard]
}

resource "aws_sns_topic" "credit_guard" {
  name = "${local.prefix}-credit-guard"
}

data "aws_iam_policy_document" "credit_guard_topic" {
  statement {
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.credit_guard.arn]
    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.this.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "credit_guard" {
  arn    = aws_sns_topic.credit_guard.arn
  policy = data.aws_iam_policy_document.credit_guard_topic.json
}

data "archive_file" "credit_guard" {
  type                    = "zip"
  output_path             = "${path.module}/.terraform/credit-guard.zip"
  source_content_filename = "index.py"
  source_content          = <<-EOT
    import os
    import boto3


    def handler(event, context):
        build = boto3.client("codebuild").start_build(projectName=os.environ["PROJECT"])
        return build["build"]["id"]
  EOT
}

resource "aws_iam_role" "credit_guard" {
  name               = "${local.prefix}-credit-guard"
  assume_role_policy = data.aws_iam_policy_document.assume["lambda"].json
}

resource "aws_iam_role_policy" "credit_guard" {
  role   = aws_iam_role.credit_guard.id
  policy = data.aws_iam_policy_document.start_stop_job.json
}

resource "aws_iam_role_policy_attachment" "credit_guard_logs" {
  role       = aws_iam_role.credit_guard.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_cloudwatch_log_group" "credit_guard" {
  name              = "/aws/lambda/${local.prefix}-credit-guard"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "credit_guard" {
  function_name    = "${local.prefix}-credit-guard"
  role             = aws_iam_role.credit_guard.arn
  runtime          = "python3.13"
  handler          = "index.handler"
  filename         = data.archive_file.credit_guard.output_path
  source_code_hash = data.archive_file.credit_guard.output_base64sha256
  timeout          = 30
  environment {
    variables = { PROJECT = aws_codebuild_project.autostop.name }
  }
  depends_on = [aws_cloudwatch_log_group.credit_guard, aws_iam_role_policy_attachment.credit_guard_logs]
}

resource "aws_lambda_permission" "credit_guard" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.credit_guard.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.credit_guard.arn
}

resource "aws_sns_topic_subscription" "credit_guard" {
  topic_arn = aws_sns_topic.credit_guard.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.credit_guard.arn
}

# What up.sh needs to upload the job's copy of infra/ and schedule the session limit.
output "autostop_project" {
  value = aws_codebuild_project.autostop.name
}

output "autostop_scheduler_role" {
  value = aws_iam_role.autostop_scheduler.arn
}

output "autostop_source" {
  value = "s3://${var.state_bucket}/${local.autostop_source}"
}

output "region" {
  value = var.region
}
