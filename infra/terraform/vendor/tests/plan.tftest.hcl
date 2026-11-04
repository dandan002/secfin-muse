# Offline plans against a mocked AWS provider: `terraform init -backend=false && terraform test`.

mock_provider "aws" {
  override_during = plan # mock computed values at plan time, as the real provider knows these

  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-2a", "us-east-2b", "us-east-2c"] }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_data "aws_ssm_parameter" {
    defaults = { arn = "arn:aws:ssm:us-east-2:111111111111:parameter/loandesk-demo/x" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111111111111" }
  }
  mock_data "aws_ecr_repository" {
    defaults = { repository_url = "111111111111.dkr.ecr.us-east-2.amazonaws.com/loandesk-demo/postgres" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111111111111:role/x" }
  }
}

variables {
  domain_name  = "example.com"
  state_bucket = "tfstate-example"
  image_tag    = "abc123"
  clerk_issuer = "https://xyz.clerk.accounts.dev"
}

run "a_mock_has_its_own_hostname_and_calls_the_others_publicly" {
  command = plan
  variables {
    stack = "demo-control"
  }
  assert {
    condition     = output.urls == { control = "https://mock-control.example.com" } && output.cluster == "ld-demo-control"
    error_message = "one level under the domain, per-system naming"
  }
  assert {
    condition     = contains(local.environment, { name = "MOCK_SFP_URL", value = "https://mock-sfp.example.com" })
    error_message = "control calls each system at its public URL"
  }
  assert {
    condition     = keys(data.aws_ssm_parameter.tunnel) == ["control"]
    error_message = "its own tunnel"
  }
}

run "ib_serves_desk_chat" {
  command = plan
  variables {
    stack = "demo-ib"
  }
  assert {
    condition     = output.urls == { ib = "https://mock-ib.example.com", chat = "https://mock-chat.example.com" }
    error_message = "ib API and Desk Chat"
  }
  assert {
    condition     = keys(output.ecr) == ["mock", "ui"] && keys(data.aws_ssm_parameter.tunnel) == ["chat", "ib"]
    error_message = "ib owns both of its image registries, and a tunnel per service"
  }
}

run "gateway_calls_its_own_counterparties_locally" {
  command = plan
  variables {
    stack = "demo-gateway"
  }
  assert {
    condition     = contains(local.environment, { name = "MOCK_GATEWAY_URL", value = "http://127.0.0.1:8020" })
    error_message = "self-callbacks stay in the container"
  }
}

run "the_desk_is_the_platform" {
  command = plan
  variables {
    stack = "demo-desk"
  }
  assert {
    condition     = output.urls == { desk = "https://api.example.com", web = "https://desk.example.com" } && keys(data.aws_ssm_parameter.tunnel) == ["desk", "web"]
    error_message = "the API at api.<domain>, the web app at desk.<domain>, a tunnel each"
  }
  assert {
    condition = (contains(local.environment, { name = "LOANDESK_CLERK_ISSUER", value = "https://xyz.clerk.accounts.dev" })
      && contains(local.environment, { name = "LOANDESK_CORS_ORIGINS", value = "[\"https://desk.example.com\"]" })
      && contains(local.environment, { name = "LOANDESK_SYNC_ENABLED", value = "false" })
    && contains(local.environment, { name = "MOCK_NGT_URL", value = "https://mock-ngt.example.com" }))
    error_message = "signs in through Clerk, trusts its web app, calls the mocks publicly (sync off by default)"
  }
  assert {
    condition     = [for s in local.secrets : s.name] == ["MOCK_ADMIN_TOKEN"]
    error_message = "the admin token only; no mock key secret"
  }
}

run "the_desk_wont_run_without_sign_in" {
  command = plan
  variables {
    stack        = "demo-desk"
    clerk_issuer = ""
  }
  expect_failures = [terraform_data.desk_sign_in]
}

run "the_desk_can_stand_by_without_sign_in" {
  command = plan
  variables {
    stack        = "demo-desk"
    clerk_issuer = ""
    running      = false
  }
}

run "llm_personas_get_the_key" {
  command = plan
  variables {
    stack           = "demo-ib"
    llm_mode        = "live"
    meta_ai_api_key = "sk-test"
  }
  assert {
    condition     = [for s in local.secrets : s.name] == ["MOCK_ADMIN_TOKEN", "MOCK_KEY_SECRET", "META_AI_API"] && startswith(local.secrets[0].valueFrom, "arn:aws:ssm:")
    error_message = "ib gets the LLM key"
  }
}

run "live_llm_needs_a_key" {
  command = plan
  variables {
    stack    = "demo-ib"
    llm_mode = "live"
  }
  expect_failures = [var.llm_mode]
}

run "each_system_resumes_from_its_own_snapshot" {
  command = plan
  variables {
    stack              = "demo-sfp"
    postgres_image_tag = "pg1"
  }
  assert {
    condition = (local.snapshot == { bucket = "loandesk-demo-snapshots-111111111111", key = "sfp.sql.gz" }
    && local.postgres_image == "111111111111.dkr.ecr.us-east-2.amazonaws.com/loandesk-demo/postgres:pg1")
    error_message = "the environment's Postgres image, saving to <system>.sql.gz"
  }
}
