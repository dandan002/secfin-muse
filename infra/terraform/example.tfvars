# cp example.tfvars demo.tfvars   (one file drives the shared stack and every system)
hosted_zone_name = "example.com"
domain_name      = "mocks.example.com"
allowed_cidrs    = ["203.0.113.10/32"] # operators (office / VPN egress); never 0.0.0.0/0
# client_cidrs   = ["192.0.2.0/28"]    # the LoanDesk platform's NAT IPs, once it exists

# budget_email    = "you@example.com"
# llm_mode        = "replay"           # off | replay | record | live
# meta_ai_api_key = "..."              # or TF_VAR_meta_ai_api_key; needed for record/live
# running         = false              # park the tasks; databases keep running
