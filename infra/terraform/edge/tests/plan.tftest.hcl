# Offline plans against mocked AWS and Cloudflare providers: `terraform init -backend=false && terraform test`.

mock_provider "aws" {
  override_during = plan
}

mock_provider "cloudflare" {
  override_during = plan

  mock_data "cloudflare_zone" {
    defaults = { zone_id = "zone123", account = { id = "acct123" } }
  }
  mock_data "cloudflare_zero_trust_tunnel_cloudflared_token" {
    defaults = { token = "eyJ0b2tlbiI6dHJ1ZX0=" }
  }
  mock_resource "cloudflare_zero_trust_tunnel_cloudflared" {
    defaults = { id = "tunnel-uuid" }
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

run "a_tunnel_and_name_per_service" {
  command = plan
  variables {
    env = "demo"
  }

  assert {
    condition = (keys(cloudflare_zero_trust_tunnel_cloudflared.this) == ["analytics", "chat", "control", "custodian", "desk", "gateway", "ib", "ngt", "sfp", "web", "wematch"]
    && alltrue([for t in cloudflare_zero_trust_tunnel_cloudflared.this : t.config_src == "cloudflare"]))
    error_message = "one remotely configured tunnel per ECS service"
  }
  assert {
    condition = (cloudflare_zero_trust_tunnel_cloudflared_config.this["desk"].config.ingress[0].hostname == "api.example.com"
      && cloudflare_zero_trust_tunnel_cloudflared_config.this["desk"].config.ingress[0].service == "http://127.0.0.1:8100"
      && cloudflare_zero_trust_tunnel_cloudflared_config.this["chat"].config.ingress[0].service == "http://127.0.0.1:80"
    && cloudflare_zero_trust_tunnel_cloudflared_config.this["ngt"].config.ingress[1].service == "http_status:404")
    error_message = "each hostname to its app on the task's loopback, nothing else"
  }
  assert {
    condition = (cloudflare_dns_record.tunnel["ngt"].name == "mock-ngt.example.com" && cloudflare_dns_record.tunnel["ngt"].proxied
    && cloudflare_dns_record.tunnel["ngt"].content == "tunnel-uuid.cfargotunnel.com")
    error_message = "proxied CNAMEs one level under the domain (the free certificate's reach)"
  }
  assert {
    condition     = aws_ssm_parameter.tunnel_token["chat"].name == "/loandesk-demo/tunnel/chat" && aws_ssm_parameter.tunnel_token["chat"].type == "SecureString"
    error_message = "each token where its task reads it"
  }
  assert {
    condition     = output.urls.web == "https://desk.example.com" && output.urls.desk == "https://api.example.com" && cloudflare_dns_record.tunnel["web"].proxied
    error_message = "the web app at desk.<domain>, the API at api.<domain>"
  }
}
