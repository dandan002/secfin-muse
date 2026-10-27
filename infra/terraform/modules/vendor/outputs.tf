output "urls" {
  value = { for k, a in local.apps : k => "https://${a.hostname}" }
}

output "fix_endpoint" {
  value = var.fix == null ? null : "${var.fix.hostname}:${var.fix.port}"
}

output "cluster" {
  value = aws_ecs_cluster.this.name
}

output "database" {
  value = aws_db_instance.this.address
}
