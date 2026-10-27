# One Aurora Serverless v2 cluster, one database, one schema per mock (created by the services).

data "aws_rds_engine_version" "postgres" {
  engine  = "aurora-postgresql"
  version = "16"
  latest  = true
}

resource "random_password" "db" {
  length  = 32
  special = false # it goes into a URL
}

resource "aws_db_subnet_group" "this" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_rds_cluster" "this" {
  cluster_identifier          = local.name
  engine                      = "aurora-postgresql"
  engine_mode                 = "provisioned"
  engine_version              = data.aws_rds_engine_version.postgres.version_actual
  database_name               = "loandesk_mocks"
  master_username             = "mocks"
  master_password             = random_password.db.result
  db_subnet_group_name        = aws_db_subnet_group.this.name
  vpc_security_group_ids      = [aws_security_group.db.id]
  storage_encrypted           = true
  deletion_protection         = var.db_deletion_protection
  skip_final_snapshot         = !var.db_deletion_protection
  final_snapshot_identifier   = var.db_deletion_protection ? "${local.name}-final" : null
  backup_retention_period     = 1
  allow_major_version_upgrade = false

  serverlessv2_scaling_configuration {
    # Each service pools up to 30 connections (7 x 30 = 210); max_connections scales with max ACU,
    # and 2 ACU allows well over that.
    min_capacity = var.db_min_acu
    max_capacity = var.db_max_acu
  }

  lifecycle {
    ignore_changes = [engine_version] # minor upgrades happen in place; don't fight them
  }
}

resource "aws_rds_cluster_instance" "this" {
  identifier         = "${local.name}-1"
  cluster_identifier = aws_rds_cluster.this.id
  instance_class     = "db.serverless"
  engine             = aws_rds_cluster.this.engine
  engine_version     = aws_rds_cluster.this.engine_version
}

# -- secrets --------------------------------------------------------------------------------
resource "aws_secretsmanager_secret" "db_url" {
  name_prefix             = "${local.name}/database-url-"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "db_url" {
  secret_id     = aws_secretsmanager_secret.db_url.id
  secret_string = "postgresql+asyncpg://mocks:${random_password.db.result}@${aws_rds_cluster.this.endpoint}:5432/loandesk_mocks"
}

resource "random_password" "admin_token" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "admin_token" {
  name_prefix             = "${local.name}/admin-token-"
  description             = "X-Admin-Token for every mock's /_admin routes"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "admin_token" {
  secret_id     = aws_secretsmanager_secret.admin_token.id
  secret_string = random_password.admin_token.result
}

resource "aws_secretsmanager_secret" "llm_key" {
  count                   = var.meta_ai_api_key != "" ? 1 : 0
  name_prefix             = "${local.name}/meta-ai-api-"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "llm_key" {
  count         = length(aws_secretsmanager_secret.llm_key)
  secret_id     = aws_secretsmanager_secret.llm_key[0].id
  secret_string = var.meta_ai_api_key
}
