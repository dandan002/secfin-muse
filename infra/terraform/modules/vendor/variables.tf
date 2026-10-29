variable "name" {
  type        = string
  description = "Prefix for every resource, e.g. ld-demo-ngt."
}

variable "service" {
  type        = string
  description = "Mock name: control, ib, gateway, ngt, wematch, sfp or custodian."
}

variable "region" {
  type = string
}

variable "hostname" {
  type        = string
  description = "Public HTTPS hostname, e.g. ngt.mocks.example.com."
}

variable "zone_id" {
  type = string
}

variable "ingress_cidrs" {
  type        = list(string)
  description = "Who may reach this system's endpoints (operators, clients, mock-control's egress IP)."
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

variable "environment" {
  type    = list(object({ name = string, value = string }))
  default = []
}

variable "secrets" {
  type    = list(object({ name = string, valueFrom = string }))
  default = []
}

variable "fix" {
  type        = object({ port = number, hostname = string })
  default     = null
  description = "Also expose a raw TCP port (the FIX session) on an NLB."
}

variable "desk_chat" {
  type        = object({ hostname = string })
  default     = null
  description = "Also serve the Desk Chat UI (mock-ib's terminal) from this system."
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

variable "nat_eip_allocation_id" {
  type        = string
  default     = null
  description = "If set, tasks run in private subnets behind a NAT gateway with this Elastic IP (a fixed egress address)."
}

variable "running" {
  type        = bool
  default     = true
  description = "false removes everything billed by the hour (load balancers, NAT, the task); the free base stays."
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
