# LoanDesk AI: AWS hosting

Terraform for the **market account** from the spec: every external-system mock in `mocks/` on ECS
Fargate. The firm-side platform isn't built yet; when it is, it gets its own root module
(`terraform/firm`) and reaches these services the way it would reach a vendor.

```
                        allowed_cidrs only
                               │
          ┌────────────────────┴─────────────────────┐
          ▼                                          ▼
  ALB :443 (*.mocks.example.com, ACM)        NLB :9878 (fix.mocks.example.com)
   host rule per service                             │ TCP
          │                                          │
  ┌───────┴──────────────────────────────────────────┴──────────────┐
  │ ECS Fargate, 1 task each                                        │
  │  control:8000  ib:8010  gateway:8020  ngt:8030(+FIX 9878)       │
  │  wematch:8040  sfp:8050  custodian:8060  chat:80 (Desk Chat)    │
  │       ▲ each other via Cloud Map: http://<svc>.mocks.local:<port> │
  └───────┼─────────────────────────────────────────────────────────┘
          ▼
  Aurora Serverless v2 Postgres 16: db loandesk_mocks, one schema per mock + control.clock
  Secrets Manager: database URL, MOCK_ADMIN_TOKEN, optional META_AI_API
```

| Piece | Choice | Why |
| --- | --- | --- |
| Compute | ECS Fargate, 0.25 vCPU / 512 MB per service, **exactly one task**, deploys stop-then-start (`min 0% / max 100%`) | Schedulers, WebSocket hubs, FIX sessions and faults live in process memory; two tasks would double-fire venue fills and persona replies. Measured ~100 MB RSS per mock after the full demo. |
| Discovery | Cloud Map private DNS (`ib.mocks.local`) | Same `MOCK_*_URL` wiring as docker-compose; one task per service needs nothing smarter. |
| Edge | HTTPS ALB, host-based routing `<svc>.<domain_name>`, 900 s idle timeout for WebSockets; TCP NLB for FIX | No app changes: the services don't support path prefixes. NLB health-checks ngt's `/health`. |
| Access | Security groups admit only `allowed_cidrs` (0.0.0.0/0 is rejected) | Control `/reset`, `/faults` and the Desk Chat `X-User` switcher are unauthenticated by design. |
| Database | One Aurora Serverless v2 cluster (0.5–2 ACU) | Every mock reads the `control.clock` row, so they must share a database. Schemas keep ledgers separate. |
| Desk Chat | The existing nginx image as a service at `chat.<domain_name>` | Same allowlist and cert as everything else. `VITE_IB_URL` is baked in at build time. |
| Egress | Public subnets + public IPs by default; `nat_gateway = true` moves tasks private | Similar cost (8 public IPv4s ≈ NAT gateway); public is simpler. Ingress is SG-locked either way. |
| Ops | CloudWatch Logs per service, Container Insights, `ecs execute-command`, optional AWS Budgets alert | |

## Deploy

Prereqs: Terraform ≥ 1.11, Docker, AWS CLI with credentials for the market account, an S3 state
bucket, and a Route 53 public hosted zone.

```bash
cd infra/terraform/market
cp backend.hcl.example backend.hcl            # edit bucket/region
cp terraform.tfvars.example demo.tfvars       # edit zone, domain, allowed_cidrs
terraform init -backend-config=backend.hcl
terraform workspace select -or-create demo
../../deploy.sh -var-file=demo.tfvars         # creates ECR, builds + pushes both images (tag = git SHA), applies
```

Then drive it like the local stack:

```bash
export MOCK_ADMIN_TOKEN=$(aws secretsmanager get-secret-value --query SecretString --output text \
  --secret-id "$(terraform output -raw admin_token_secret_arn)")
curl -XPOST https://control.mocks.example.com/scenarios/demo/run -H 'content-type: application/json' -d '{"mode":"live","speed":10}'
cd ../../../mocks && uv run python scripts/smoke.py \
  --url-template 'https://{name}.mocks.example.com' --host fix.mocks.example.com
```

* Park between demos: `terraform apply -var-file=demo.tfvars -var running=false` (scales to 0; Aurora stays).
* Tear down: `terraform destroy -var-file=demo.tfvars` (ECR repos and secrets are force-deleted).
* Offline checks (no AWS needed): `terraform init -backend=false && terraform validate && terraform test`.

## Cost (us-east-1, running 24/7, approximate)

| Item | $/month |
| --- | --- |
| Fargate, 8 × 0.25 vCPU / 0.5 GB | ~72 |
| Aurora Serverless v2 at the 0.5 ACU floor + storage | ~45 |
| Public IPv4 (8 tasks + ALB + NLB) | ~44 |
| ALB | ~22 |
| NLB | ~17 |
| Secrets, Route 53, logs, ECR | ~5 |
| **Total** | **~205** (≈ $7/day) |

`running = false` drops it to about $90/month. Aurora can't idle below 0.5 ACU: seven services poll
the clock row four times a second.

## Mock audit (Oct 2026)

Baseline is healthy: 41/41 integration tests, ruff and mypy are clean, the Desk Chat build works, and
the demo smoke passes 10/10 against a stack with the admin token enforced.

**Fixed in this change**

1. **`MOCK_ADMIN_TOKEN` didn't protect most `/_admin` routes.** The check was a dependency on the
   shared admin router, but every service registers its own `/_admin/*` routes on the app directly:
   `credentials` (all API keys, bot tokens, webhook secrets, FIX passwords), `injected-breaks` and
   `labels` (answer keys), plus `config`, `flags`, `limits` and `loadgen`. With the token set,
   `/_admin/faults` returned 401 but `/_admin/credentials` returned 200. The check now runs in the
   ASGI middleware for every `/_admin` path, over both HTTP and WebSocket (regression test in
   `tests/test_mockcore.py`).
2. `scripts/smoke.py` sends `X-Admin-Token` from `MOCK_ADMIN_TOKEN` and takes `--url-template` so it
   can replay the demo against the hosted stack.

**Hosting constraints (handled in the Terraform)**

3. Single-instance only: in-memory scheduler, hubs, faults and FIX sessions. A restart drops pending
   timers (already documented). Hence one task per service and stop-then-start deploys.
4. `/health` is exempt from simulated outages and error faults, so chaos drills don't make the
   load balancer kill tasks. Keep it that way.
5. Connection pools allow 30 per service (210 in total). Aurora at ≥ 2 max ACU covers that; a small
   RDS instance such as t4g.micro would not.
6. First boot seeds the universe before `/health` answers, so the grace period is 180 s.

**Open items (not changed)**

7. Control's own `/reset`, `/clock`, `/faults` and `/scenarios` routes aren't under `/_admin`, so
   anyone who can reach control can reset the simulation. Today only the IP allowlist protects
   them. Consider requiring the admin token on control's mutating routes. That changes the local
   workflow, so it's left for a decision.
8. Desk Chat human API: no auth (`X-User`, `?as=`). Fine for a demo behind an allowlist. For wider
   access, put ALB + Cognito authentication on `chat` and `ib`.
9. FIX is plaintext TCP with the password in tag 554. An NLB TLS listener with the ACM cert is a
   small follow-up if the client can speak TLS.
10. LLM `replay` mode has nothing to replay in the cloud: `mocks/cassettes/` isn't in the repo or
    the image, and `record` writes to the task's ephemeral disk. Bake a cassette into the image (or
    use S3) before relying on LLM personas there.
11. Integration tests **skip** rather than fail when Postgres is unreachable, and
    `MOCK_TEST_DATABASE_ADMIN_URL` must be a plain `postgresql://` URL. A misconfigured CI would
    pass with 22 tests skipped. There's no CI yet; when it's added, make it fail on skips.
12. The spec puts mock-ngt on DynamoDB. The code uses Postgres like the others, and the shared clock
    row assumes one database. Keeping Postgres is the simpler, consistent choice.

## Not yet built (from the spec's cloud section)

The second "firm" account and PrivateLink between them, API Gateway with mTLS and usage plans for
counterparties, WAF, Cognito roles, MSK, SFTP (Transfer Family) for locate and EOD files, OTel to
X-Ray, and GitHub Actions deploys via OIDC (`deploy.sh` is what that workflow would run). Most of
these belong with the platform, not the mocks.
