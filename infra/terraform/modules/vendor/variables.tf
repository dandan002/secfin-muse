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

variable "image" {
  type        = string
  description = "Mocks image with tag."
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
  type        = object({ image = string, hostname = string })
  default     = null
  description = "Also serve the Desk Chat UI (mock-ib's terminal) from this system."
}

variable "nat_eip_allocation_id" {
  type        = string
  default     = null
  description = "If set, tasks run in private subnets behind a NAT gateway with this Elastic IP (a fixed egress address)."
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_deletion_protection" {
  type    = bool
  default = false
}

variable "running" {
  type    = bool
  default = true
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
