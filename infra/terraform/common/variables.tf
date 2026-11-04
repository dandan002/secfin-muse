# Shared by the `shared`, `edge` and `vendor` roots (symlinked into each) so one tfvars file drives
# every stack: terraform apply -var-file=../demo.tfvars

variable "region" {
  type    = string
  default = "us-east-2"
}

variable "domain_name" {
  type        = string
  description = "A Cloudflare zone, e.g. loandesk.dev. Every system gets a hostname one level under it (catalog.tf)."
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
  description = "Optional key for mock-ib's LLM personas (META_AI_API). Stored in SSM by the shared stack."
}

variable "gateway_downstream_url" {
  type        = string
  default     = ""
  description = "Optional LoanDesk platform URL that mock-gateway pushes locates to (MOCK_GATEWAY_DOWNSTREAM_URL)."
}

# -- the desk (the LoanDesk platform) ----------------------------------------------------------
variable "clerk_issuer" {
  type        = string
  default     = ""
  description = "The Clerk instance's Frontend API URL. The desk's API is public, so it won't run without one."
}

variable "clerk_publishable_key" {
  type        = string
  default     = ""
  description = "pk_test_... / pk_live_...: public, baked into the web app's build (infra/up.sh)."
}

variable "desk_trader_roles" {
  type        = list(string)
  default     = []
  description = "Clerk roles (public metadata, in the loandesk token) the desk lets in, e.g. [\"trader\", \"head\"]. Run infra/clerk_setup.py on the instance first."
}

variable "desk_sync" {
  type        = bool
  default     = false
  description = "Have the desk poll the mocks. Off while they aren't running, so its logs stay quiet."
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
