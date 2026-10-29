# LoanDesk AI: AWS hosting

Terraform for the spec's **market account**. Each mock is its own code project
(`mocks/<name>`, with its own lockfile, Dockerfile and image) and is hosted the way the real
external system would be: as a separate system with its own image registry, network, database,
compute, endpoint and Terraform state. The systems share nothing at runtime. They meet only over public HTTPS (and
FIX), with IP allowlists, the way a desk connects to its vendors.

```
 operators (allowed_cidrs)          LoanDesk platform, later (client_cidrs)
        │                                         │
        ▼                                         ▼
┌──────────────────────┐   HTTPS from a fixed   ┌─────────────────────────────────────────────┐
│ mock-control         │   egress IP (NAT+EIP)  │ ngt.<domain> (Venue A)    + fix.<domain>:9878│
│  control.<domain>    │ ─────────────────────► │   own VPC · ALB + NLB · ECS task · RDS       │
│  own VPC · ALB · ECS │   clock pushes, ticks, ├─────────────────────────────────────────────┤
│  task · RDS (clock)  │   resets, faults,      │ ib.<domain> + chat.<domain> (Desk Chat)      │
└──────────────────────┘   scenario verbs       │   own VPC · ALB · 2 ECS tasks · RDS          │
                                                ├─────────────────────────────────────────────┤
                                                │ gateway · wematch · sfp · custodian · analytics│
                                                │   each: own VPC · ALB · ECS task · RDS       │
                                                └─────────────────────────────────────────────┘
```

## What made this possible

Before this change, every mock read the sim clock from one row in a shared database, so they could
not be separated. Now:

* **mock-control** owns the clock in its own database and **pushes** it to each system
  (`PUT /_admin/clock`) on every change and every 5 s. The push needs the admin token.
* Each system stores the last clock it received **in its own database**, so it boots and resumes
  without ever calling mock-control. Pushes carry a revision, so a late one can't overwrite a newer
  clock.
* Each system documents its API in `mocks/<name>/API.md` and ships a standalone client package in
  `mocks/<name>/connector/` for the platform to install. The connectors stay out of the server images.
* Each mock is a separate project (see `mocks/README.md`): own `pyproject.toml`, `uv.lock`,
  Dockerfile and dependencies, built on two small versioned libraries (`mockcore`, `refdata`). No system
  imports another. The harness runs each one from its own environment and talks to them only as a client.
* Locally, docker-compose builds one image and runs one Postgres container per mock. The harness gives
  each mock its own database, and a test asserts that no mock's database holds another's tables.

The only cross-system traffic left is mock-control's fan-out (clock, ticks, resets, faults, scenario
verbs), which goes to each system's public URL, as a simulator driving external venues would.

## Layout

| Path | What | State |
| --- | --- | --- |
| `terraform/shared` | The simulator's admin token, mock-control's Elastic IP, an optional LLM key and budget. Nothing on a request path. | workspace `<env>`, e.g. `demo` |
| `terraform/vendor` | One external system. The workspace picks which one. | workspace `<env>-<system>`, e.g. `demo-ngt` |
| `terraform/modules/vendor` | The system: its own ECR repos, VPC (2 AZs), RDS Postgres 16, ECS cluster + Fargate service, HTTPS ALB with its own ACM cert and DNS name, an NLB for raw TCP (FIX), IAM, logs, secrets. | — |
| `terraform/common/variables.tf` | Variables for both roots (symlinked), so one `<env>.tfvars` drives everything. | — |

Per-system design points:

* **Exactly one task, stop-then-start deploys** (`min 0% / max 100%`). Each mock keeps its scheduler,
  WebSocket hub, FIX sessions and faults in memory, so two copies would double-fire fills, persona
  replies and settlements. A deploy means about a minute of downtime for that one system.
* **Own RDS instance** (`db.t4g.micro`, 20 GB gp3, private subnets, encrypted). One mock pools at most
  30 connections, which a micro handles.
* **Ingress**:
  * mock-control: `allowed_cidrs` only (it's the operators' console).
  * Every other system: `allowed_cidrs` + `client_cidrs` + mock-control's Elastic IP.
  * `0.0.0.0/0` is rejected.
* **Egress**:
  * mock-control runs in private subnets behind a NAT on the shared Elastic IP, a fixed address
    vendors can allowlist.
  * Others run in public subnets with public IPs, with inbound traffic only from their own load
    balancers. That covers LLM calls and webhooks to external callbacks.
* mock-gateway calls its own simulated counterparties at `127.0.0.1`. Every other URL is public.
* **Own image, own version.** Each system has its own ECR repos (`loandesk-<env>/<name>`, plus
  `ib-desk-chat`), built from `mocks/<name>/Dockerfile`. The image tag is the last commit
  touching that project or the libs, so redeploying one system never rebuilds or restarts another.

## Deploy

Prereqs: Terraform ≥ 1.11, Docker, AWS CLI with credentials for the market account, a versioned S3
bucket for state, and a Route 53 public hosted zone.

```bash
cd infra/terraform
cp backend.hcl.example backend.hcl     # bucket, region
cp example.tfvars demo.tfvars          # zone, domain, allowed_cidrs
cd ../..
TF_APPLY_ARGS=-auto-approve PARALLEL=1 infra/deploy.sh demo     # shared, then all 8 systems
infra/deploy.sh demo ngt sfp                                    # later: roll just these
```

For each system, `deploy.sh` creates its ECR repos, builds and pushes its project's image, then
applies its stack. A first deploy creates eight RDS instances. `PARALLEL=1` applies the systems at once (about 15
minutes) instead of one after another (about an hour). Then drive it like the local stack:

```bash
export MOCK_ADMIN_TOKEN=$(aws secretsmanager get-secret-value --query SecretString --output text \
  --secret-id "$(terraform -chdir=infra/terraform/shared output -raw admin_token_secret_arn)")
curl -XPOST https://control.mocks.example.com/scenarios/demo/run -H 'content-type: application/json' -d '{"mode":"live","speed":10}'
cd mocks/harness && uv run python smoke.py --url-template 'https://{name}.mocks.example.com' --host fix.mocks.example.com
```

The smoke script calls the systems directly, so run it from an address in `allowed_cidrs`.

* **Between demos**, destroy. All mock data derives from the seed, so `infra/destroy.sh demo` (or
  `infra/destroy.sh demo ngt` for one system) loses nothing; redeploy and `POST /reset`. The
  cheaper-but-slower alternative is `-var running=false`, which parks the tasks and keeps the
  databases.
* **Offline checks** (no AWS needed), in each of `shared`, `vendor` and `modules/vendor`:
  `terraform init -backend=false && terraform validate && terraform test`.

## Cost (us-east-1, running 24/7, approximate)

| System | Pieces | $/month |
| --- | --- | --- |
| gateway, wematch, sfp, custodian, analytics | each: ALB ~22, RDS micro + storage ~14, Fargate ~9, 3 public IPv4 ~11, secrets/logs ~1 | 5 × ~57 = ~285 |
| ib | the above + Desk Chat task and IP | ~70 |
| ngt | the above + NLB and its 2 IPs | ~82 |
| control | the above + NAT gateway and Elastic IP, minus the task IP | ~90 |
| shared | admin token; per-system ECR is ~$0.10 each | ~2 |
| **Total** | | **~530** (≈ $17/day) |

That's about 2.6× the single-stack layout, mostly from eight ALBs, eight databases and the NAT.
That's the price of real separation. Destroying between demo days takes it to ~$0. Parking with
`running = false` saves about $100/month.

## Mock audit (Oct 2026)

Baseline is healthy: 50 tests (27 per-project unit tests plus 23 cross-system tests) pass, ruff and mypy
are clean, the Desk Chat build works, and the demo smoke passes 10/10 with every mock running from its
own project environment and database and the admin token enforced.

**Fixed**

1. **`MOCK_ADMIN_TOKEN` didn't protect most `/_admin` routes.** Service-specific routes
   (`credentials` with every API key, bot token, webhook secret and FIX password; `injected-breaks`
   and `labels` with the answer keys; `config`, `flags`, `limits`, `loadgen`) were registered outside
   the guarded router. The check now runs in the middleware for every `/_admin` path (HTTP and
   WebSocket), with a regression test.
2. **Shared clock row.** Every mock read `control.clock` from one database, which forced a shared
   database. The clock is now pushed and stored per mock (see above).
3. **One codebase.** The mocks shared one `pyproject`, lockfile and image. Each is now its own project.
   The split surfaced one hidden coupling: a test built its ISO 20022 message with the custodian's
   server code. It now sends its own XML, as a client would.
4. `harness/smoke.py` sends `X-Admin-Token` and takes `--url-template` for the hosted systems.
5. Integration tests now **fail** instead of skipping when `MOCK_TEST_DATABASE_ADMIN_URL` is set but
   Postgres is unreachable, so a misconfigured CI can't pass with nothing tested.

**Hosting constraints (handled)**

6. Single-instance only, so one task per system with stop-then-start deploys. A restart drops
   pending timers (already documented).
7. `/health` is exempt from simulated outages and errors, so chaos drills don't make a load balancer
   kill the task. Keep it that way.
8. First boot seeds the universe before `/health` answers, so the grace period is 180 s.

**Open items**

9. mock-control's own `/reset`, `/clock`, `/faults` and `/scenarios` routes aren't under `/_admin`,
   so anyone in `allowed_cidrs` can reset the simulation. Consider requiring the admin token there.
   That changes the local workflow, so it's left for a decision.
10. Desk Chat human API: no auth (`X-User`, `?as=`). Fine behind an allowlist. For wider access, put
   ALB + Cognito authentication on `chat` and `ib`.
11. FIX is plaintext TCP with the password in tag 554. An NLB TLS listener is a small follow-up if the
   client can speak TLS.
12. LLM `replay` mode has nothing to replay in the cloud: `mocks/ib/cassettes/` isn't in the repo or the
    image, and `record` writes to the task's ephemeral disk. Bake a cassette into the image (or use S3)
    before relying on LLM personas there.
13. The spec puts mock-ngt on DynamoDB. It now has its own Postgres, like every system. Swapping in
    DynamoDB is possible per system later and no longer affects the others.

## Not yet built (from the spec's cloud section)

The second "firm" account (its NAT IPs go in `client_cidrs`), PrivateLink as an alternative to
public allowlisted endpoints, API Gateway with mTLS and usage plans for counterparties, WAF, Cognito
roles, MSK, SFTP (Transfer Family) for locate and EOD files, OTel to X-Ray, and GitHub Actions
deploys via OIDC (running `deploy.sh`). Most of these belong with the platform, not the mocks.
