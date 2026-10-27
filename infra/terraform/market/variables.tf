variable "region" {
  type    = string
  default = "us-east-1"
}

variable "hosted_zone_name" {
  type        = string
  description = "Existing Route 53 public hosted zone, e.g. example.com."
}

variable "domain_name" {
  type        = string
  description = "Subdomain the mocks live under, e.g. mocks.example.com. Services get <name>.<domain_name>."
}

variable "allowed_cidrs" {
  type        = list(string)
  description = "CIDRs allowed to reach the ALB and the FIX NLB. The mocks have unauthenticated surfaces (control /reset, Desk Chat X-User), so keep this tight."

  validation {
    condition     = length(var.allowed_cidrs) > 0 && !contains(var.allowed_cidrs, "0.0.0.0/0")
    error_message = "Give at least one CIDR, and not 0.0.0.0/0: the mocks are not safe on the open internet."
  }
}

variable "image_tag" {
  type        = string
  default     = "latest"
  description = "Tag for both images (infra/deploy.sh passes the git SHA)."
}

variable "running" {
  type        = bool
  default     = true
  description = "false scales every service to 0 tasks (cost saver between demos; Aurora keeps running)."
}

variable "nat_gateway" {
  type        = bool
  default     = false
  description = "true: tasks in private subnets behind a NAT gateway. false: tasks in public subnets with public IPs, ingress locked to the load balancers."
}

variable "db_min_acu" {
  type    = number
  default = 0.5
}

variable "db_max_acu" {
  type    = number
  default = 2
}

variable "db_deletion_protection" {
  type    = bool
  default = false
}

variable "seed" {
  type    = number
  default = 20261027
}

variable "llm_mode" {
  type    = string
  default = "off"

  validation {
    condition     = contains(["off", "replay", "record", "live"], var.llm_mode)
    error_message = "llm_mode must be off, replay, record or live."
  }
}

variable "meta_ai_api_key" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Optional key for LLM personas (META_AI_API). Stored in Secrets Manager."
}

variable "gateway_downstream_url" {
  type        = string
  default     = ""
  description = "Optional LoanDesk platform URL that mock-gateway pushes locates to (MOCK_GATEWAY_DOWNSTREAM_URL)."
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "monthly_budget_usd" {
  type    = number
  default = 250
}

variable "budget_email" {
  type        = string
  default     = ""
  description = "If set, AWS Budgets emails this address at 80% actual and 100% forecast spend."
}
