# Shared by the `shared` and `vendor` roots (symlinked into both) so one tfvars file drives
# every stack: terraform apply -var-file=../demo.tfvars

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
  description = "Subdomain the systems live under, e.g. mocks.example.com. Each gets <service>.<domain_name>."
}

variable "allowed_cidrs" {
  type        = list(string)
  description = "Operators: may reach every system, including mock-control and Desk Chat."

  validation {
    condition     = length(var.allowed_cidrs) > 0 && !contains(var.allowed_cidrs, "0.0.0.0/0")
    error_message = "Give at least one CIDR, and not 0.0.0.0/0: the mocks are not safe on the open internet."
  }
}

variable "client_cidrs" {
  type        = list(string)
  default     = []
  description = "Clients of the external systems (e.g. the LoanDesk platform's NAT IPs). mock-control's egress IP is added automatically."

  validation {
    condition     = !contains(var.client_cidrs, "0.0.0.0/0")
    error_message = "Not 0.0.0.0/0."
  }
}

variable "image_tag" {
  type        = string
  default     = "latest"
  description = "Tag for both images (infra/up.sh passes the git SHA)."
}

variable "postgres_image_tag" {
  type        = string
  default     = "latest"
  description = "Tag of the environment's Postgres image (infra/up.sh passes infra/postgres's last commit)."
}

variable "running" {
  type        = bool
  default     = true
  description = "false removes the system's load balancers, NAT and task (infra/down.sh); its free base stays."
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
  validation {
    condition     = contains(["off", "replay"], var.llm_mode) || var.meta_ai_api_key != ""
    error_message = "record and live need meta_ai_api_key."
  }
}

variable "meta_ai_api_key" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Optional key for mock-ib's LLM personas (META_AI_API). Stored in Secrets Manager by the shared stack."
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

variable "state_bucket" {
  type        = string
  description = "The Terraform state bucket (as in backend.hcl). The auto-stop job reads its copy of this config from it."
}

variable "credit_guard_usd" {
  type        = number
  default     = 40
  description = "Month-to-date spend, before credits, at which every running system is stopped."
}

variable "budget_email" {
  type        = string
  default     = ""
  description = "If set, AWS Budgets emails this address at 80% and 100% of credit_guard_usd."
}
