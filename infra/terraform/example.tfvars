# cp example.tfvars demo.tfvars   (one file drives the shared stack and every system)
hosted_zone_name = "example.com"
domain_name      = "mocks.example.com"
allowed_cidrs    = ["203.0.113.10/32"]      # operators (office / VPN egress); never 0.0.0.0/0
state_bucket     = "my-org-terraform-state" # the bucket in backend.hcl; the auto-stop job reads from it
# client_cidrs   = ["192.0.2.0/28"]    # the LoanDesk platform's NAT IPs, once it exists

# credit_guard_usd = 40                # stop everything once the month's spend (before credits) passes this
# budget_email    = "you@example.com"  # emails at 80% and 100% of credit_guard_usd
# llm_mode        = "replay"           # off | replay | record | live
# meta_ai_api_key = "..."              # or TF_VAR_meta_ai_api_key; needed for record/live
