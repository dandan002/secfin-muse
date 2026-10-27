terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Partial config; pass the rest with `terraform init -backend-config=backend.hcl`
  # (see backend.hcl.example). Workspaces (`demo`, `dev`) get separate state keys.
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project     = "loandesk-ai"
      account     = "market"
      environment = terraform.workspace
      managed-by  = "terraform"
    }
  }
}
