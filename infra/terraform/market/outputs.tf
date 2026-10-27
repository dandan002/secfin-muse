output "urls" {
  description = "Public HTTPS endpoint per service (chat = Desk Chat UI)."
  value       = local.public_urls
}

output "fix_endpoint" {
  value = "${local.fix_host}:${local.fix_port}"
}

output "desk_chat_ib_url" {
  description = "VITE_IB_URL build arg for the Desk Chat image (the browser calls mock-ib directly)."
  value       = local.public_urls["ib"]
}

output "ecr_mocks" {
  value = aws_ecr_repository.mocks.repository_url
}

output "ecr_desk_chat" {
  value = aws_ecr_repository.desk_chat.repository_url
}

output "cluster" {
  value = aws_ecs_cluster.this.name
}

output "admin_token_secret_arn" {
  description = "aws secretsmanager get-secret-value --secret-id <arn> --query SecretString --output text"
  value       = aws_secretsmanager_secret.admin_token.arn
}
