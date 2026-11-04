mock_provider "aws" {
  override_during = plan

  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111111111111" }
  }
}

variables {
  domain_name  = "example.com"
  state_bucket = "tfstate-example"
}

run "needs_an_environment_workspace" {
  command         = plan
  expect_failures = [terraform_data.env_name] # tests run in the "default" workspace
}
