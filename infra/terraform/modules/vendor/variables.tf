variable "name" {
  type        = string
  description = "Prefix for every resource, e.g. ld-demo-ngt."
}

variable "service" {
  type        = string
  description = "System name: desk (the platform's API) or a mock (control, ib, gateway, ngt, wematch, sfp, custodian, analytics)."
}

variable "region" {
  type = string
}

variable "hostname" {
  type        = string
  description = "Public HTTPS hostname its tunnel carries, e.g. mock-ngt.loandesk.dev (for the outputs; edge routes it)."
}

variable "tunnel_token_parameter" {
  type        = string
  description = "ARN of the SSM parameter holding this service's Cloudflare Tunnel token (written by edge)."
}

variable "cloudflared_image" {
  type    = string
  default = "cloudflare/cloudflared:2026.10.7"
}

variable "repository_prefix" {
  type        = string
  description = "ECR namespace, e.g. loandesk-demo; this system's repos are <prefix>/<service>[-desk-chat]."
}

variable "image_tag" {
  type        = string
  description = "Tag of this system's images (its project's last commit)."
}

variable "port" {
  type = number
}

variable "app_module" {
  type        = string
  description = "ASGI app, e.g. mock_ngt.app:app."
}

variable "database_url_env" {
  type        = string
  default     = "MOCK_DATABASE_URL"
  description = "The variable the app reads its database URL from."
}

variable "cpu" {
  type    = number
  default = 256 # ~100 MB RSS per mock after the full demo
}

variable "memory" {
  type    = number
  default = 1024 # the smallest Fargate size with room for the app, Postgres and cloudflared
}

variable "environment" {
  type    = list(object({ name = string, value = string }))
  default = []
}

variable "secrets" {
  type    = list(object({ name = string, valueFrom = string }))
  default = []
}

variable "ui" {
  type        = object({ name = string, hostname = string, tunnel_token_parameter = string })
  default     = null
  description = "Also serve a static UI (the desk's web app, mock-ib's Desk Chat) as a second service with its own tunnel."
}

variable "postgres_image" {
  type        = string
  description = "The Postgres image with the snapshot hooks (infra/postgres), e.g. <registry>/loandesk-demo/postgres:<tag>."
}

variable "snapshot" {
  type        = object({ bucket = string, key = string })
  default     = null
  description = "Where Postgres saves itself when the task stops and restores from on the next start. null: start empty every time."
}

variable "running" {
  type        = bool
  default     = true
  description = "false removes everything billed by the hour (the task and its service); the free base stays."
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "vpc_cidr" {
  type        = string
  default     = "10.40.0.0/16"
  description = "Each system has its own VPC; they never peer, so they can all use the same range."
}
