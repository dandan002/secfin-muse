mock_provider "aws" {
  override_during = plan

  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111111111111" }
  }
  mock_data "aws_route53_zone" {
    defaults = { zone_id = "Z123" }
  }
}

variables {
  hosted_zone_name = "example.com"
  domain_name      = "mocks.example.com"
  allowed_cidrs    = ["203.0.113.0/24"]
  state_bucket     = "tfstate-example"
}

run "needs_an_environment_workspace" {
  command         = plan
  expect_failures = [terraform_data.env_name] # tests run in the "default" workspace
}
