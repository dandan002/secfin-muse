mock_provider "aws" {
  override_during = plan
}

variables {
  hosted_zone_name = "example.com"
  domain_name      = "mocks.example.com"
  allowed_cidrs    = ["203.0.113.0/24"]
}

run "needs_an_environment_workspace" {
  command         = plan
  expect_failures = [terraform_data.env_name] # tests run in the "default" workspace
}
