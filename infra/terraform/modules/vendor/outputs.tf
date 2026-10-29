output "urls" {
  description = "Where the system answers while running."
  value       = { for k, a in local.apps : k => "https://${a.hostname}" }
}

output "running" {
  value = var.running
}

output "fix_endpoint" {
  value = var.fix == null ? null : "${var.fix.hostname}:${var.fix.port}"
}

output "cluster" {
  value = aws_ecs_cluster.this.name
}

output "ecr" {
  description = "This system's image repositories: mock, and desk-chat for mock-ib."
  value       = { for k, r in aws_ecr_repository.this : k => r.repository_url }
}
