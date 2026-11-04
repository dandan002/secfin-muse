# cp example.tfvars demo.tfvars   (one file drives the shared, edge and vendor stacks)
region       = "us-east-2"
domain_name  = "example.com"            # a Cloudflare zone; see common/catalog.tf for the hostnames
state_bucket = "my-org-terraform-state" # the bucket in backend.hcl; the auto-stop job reads from it

# The desk (the platform): its API is public behind the tunnel, so it needs Clerk.
clerk_issuer          = "https://xyz.clerk.accounts.dev"
clerk_publishable_key = "pk_test_..."
# desk_sync           = true               # once the mocks are running

# credit_guard_usd = 40                # stop everything once the month's spend (before credits) passes this
# budget_email    = "you@example.com"  # emails at 80% and 100% of credit_guard_usd
# llm_mode        = "replay"           # off | replay | record | live
# meta_ai_api_key = "..."              # or TF_VAR_meta_ai_api_key; needed for record/live
# Clerk roles (public metadata, via the loandesk token template) the desk lets in; set up with infra/clerk_setup.py
desk_trader_roles     = ["trader", "head"]
