# LoanDesk AI: AWS hosting

Terraform for the spec's **market account**. Each mock is its own code project
(`mocks/<name>`, with its own lockfile, Dockerfile and image) and is hosted the way the real
external system would be: as a separate system with its own image registry, network, database,
compute, endpoint and Terraform state. The systems share nothing at runtime. They meet only over public HTTPS (and
FIX), with IP allowlists, the way a desk connects to its vendors.

Systems run **only during a session**: `infra/up.sh` starts the ones you name, `infra/down.sh` stops
them, and an auto-stop inside AWS stops everything when the session's time is up or the month's
spend crosses a guard. Each system saves its database as it stops and picks up where it left off
on the next start. Standby costs about $1 a month.

```
 operators (allowed_cidrs)          LoanDesk platform, later (client_cidrs)
        │                                         │
        ▼                                         ▼
┌──────────────────────┐   HTTPS from a fixed   ┌─────────────────────────────────────────────┐
│ mock-control         │   egress IP (NAT+EIP)  │ ngt.<domain> (Venue A)    + fix.<domain>:9878│
│  control.<domain>    │ ─────────────────────► │   own VPC · ALB + NLB · task (mock + pg)     │
│  own VPC · ALB · task│   clock pushes, ticks, ├─────────────────────────────────────────────┤
│  (mock + pg: clock)  │   resets, faults,      │ ib.<domain> + chat.<domain> (Desk Chat)      │
└──────────────────────┘   scenario verbs       │   own VPC · ALB · 2 tasks (mock + pg, chat)  │
                                                ├─────────────────────────────────────────────┤
                                                │ gateway · wematch · sfp · custodian          │
                                                │   each: own VPC · ALB · task (mock + pg)     │
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
| `terraform/shared` | The simulator's admin token and API-key secret, mock-control's Elastic IP, an optional LLM key, the snapshot bucket, the Postgres image registry, and the auto-stop (CodeBuild job, Scheduler role, credit-guard budget). Nothing on a request path. | workspace `<env>`, e.g. `demo` |
| `terraform/vendor` | One external system. The workspace picks which one. | workspace `<env>-<system>`, e.g. `demo-ngt` |
| `terraform/modules/vendor` | The system: its own ECR repos, VPC (2 AZs), ECS cluster + Fargate service (the mock plus its own Postgres 16), HTTPS ALB with its own ACM cert and DNS name, an NLB for raw TCP (FIX), IAM, logs. | — |
| `postgres/` | The Postgres image every task runs: the official image plus the snapshot save/restore hooks. | — |
| `up.sh`, `down.sh`, `status.sh` | Start systems for a session (and arm the auto-stop), stop them, show what's on. | — |
| `destroy.sh` | Remove systems entirely, base included. Rarely needed: `down.sh` already takes a system to ~$0. | — |
| `terraform/common/variables.tf` | Variables for both roots (symlinked), so one `<env>.tfvars` drives everything. | — |

Per-system design points:

* **Exactly one task, stop-then-start deploys** (`min 0% / max 100%`). Each mock keeps its scheduler,
  WebSocket hub, FIX sessions and faults in memory, so two copies would double-fire fills, persona
  replies and settlements. A deploy means about a minute of downtime for that one system.
* **Own Postgres, inside its task.** Each mock's task runs a `postgres:16` container next to it (0.25
  vCPU / 1 GB for the pair). The mock waits for it to be healthy and reaches it over the task's
  loopback; port 5432 is never opened. No RDS to create (~10 min) or pay for.
* **Graceful stop, resumed sessions.** When a task stops, ECS stops the mock first, then gives
  Postgres up to 120 s. It `pg_dump`s to `s3://loandesk-<env>-snapshots-<account>/<system>.sql.gz`
  (seconds for a session's ~1000 trades) and shuts down. The next task restores that dump before it
  accepts connections, and the mock sees its seed marker and carries on instead of reseeding.
  `up.sh --fresh` discards a system's snapshot and starts it from the seed. The bucket is versioned,
  so an older dump stays recoverable for 30 days. If a restore fails, the task fails rather than
  starting empty and overwriting the snapshot at the next stop.
* **On or off** (`running`). The base (VPC, security groups, certificate, ECR, logs, IAM) is free
  and always there. Everything billed by the hour (ALB, NLB, NAT, task, public IPs, DNS records)
  exists only while running, so a start is a ~5 minute apply and a stop leaves nothing billing.
* **Ingress**:
  * mock-control: `allowed_cidrs` only (it's the operators' console).
  * Every other system: `allowed_cidrs` + `client_cidrs` + mock-control's Elastic IP.
  * `0.0.0.0/0` is rejected.
* **Egress**:
  * mock-control runs in private subnets behind a NAT on the shared Elastic IP, an address vendors
    can allowlist. `down.sh <env>` releases it once every system is stopped, and the next `up.sh`
    allocates a new one. Each system allowlists the current address when it starts.
  * Others run in public subnets with public IPs, with inbound traffic only from their own load
    balancers. That covers LLM calls and webhooks to external callbacks.
* mock-gateway calls its own simulated counterparties at `127.0.0.1`. Every other URL is public.
* **Own image, own version.** Each system has its own ECR repos (`loandesk-<env>/<name>`, plus
  `ib-desk-chat`), built from `mocks/<name>/Dockerfile`. The image tag is the last commit
  touching that project or the libs, so redeploying one system never rebuilds or restarts another.

## Sessions

Prereqs: Terraform ≥ 1.11, Docker, zip, AWS CLI with credentials for the market account, a
versioned S3 bucket for state, and a Route 53 public hosted zone.

```bash
cd infra/terraform
cp backend.hcl.example backend.hcl     # bucket, region
cp example.tfvars demo.tfvars          # zone, domain, allowed_cidrs, state_bucket
cd ../..
export AWS_PROFILE=...                 # aws login --profile ... first; credentials last 12 h

infra/up.sh demo                       # every system, auto-stop in 3 h
infra/up.sh demo ngt ib control        # only the systems this session needs
infra/up.sh demo --for 90m             # again later: moves the stop time (and starts any named systems)
infra/up.sh demo --fresh sfp           # start sfp from the seed instead of its saved session
infra/status.sh demo                   # what's on, its URLs, when it stops
infra/down.sh demo ngt                 # stop one system
infra/down.sh demo                     # stop everything, release the egress IP, cancel the auto-stop
```

`up.sh` applies the shared stack, arms the auto-stop, builds the Postgres image if it changed, then
starts the systems in parallel (logs in `infra/out/`). Docker runs with its own temporary config, so the ECR login never touches the macOS
keychain. Per system it builds and pushes the image only if that version isn't in ECR yet, then
applies with `running=true`. The first start of a system takes ~10 minutes (image build, certificate);
later ones ~5. mock-control tolerates systems that are off: it reports them as unreachable and its
5-second clock push catches each one up when it starts.

**Auto-stop** runs inside AWS, so it fires with the laptop shut. Both triggers start the same
CodeBuild job, which runs `infra/down.sh <env>` against the copy of `infra/` that `up.sh` uploads
next to the state:

* **Session limit:** `up.sh --for <15m–12h>` (default 3h) sets a one-off EventBridge Scheduler
  schedule. Running `up.sh` again moves it; `down.sh <env>` cancels it.
* **Credit guard:** an AWS Budget on the month's spend *before credits* (on the free plan, net spend
  stays $0 until the credits are gone). Past `credit_guard_usd` (default $40) it publishes to SNS, and
  a small Lambda starts the stop job. Set `budget_email` to also get emails at 80% and 100%. Budgets
  refresh a few times a day, so this is a backstop for a forgotten session, not a meter.

Drive it like the local stack:

```bash
export MOCK_ADMIN_TOKEN=$(aws ssm get-parameter --with-decryption --query Parameter.Value --output text \
  --name "$(terraform -chdir=infra/terraform/shared output -raw admin_token_parameter)")
# Each system's API keys: GET /_admin/credentials (admin token). Here, control's operator key.
KEY=$(curl -s https://control.mocks.example.com/_admin/credentials -H "X-Admin-Token: $MOCK_ADMIN_TOKEN" | jq -r .operatorKey)
curl -XPOST https://control.mocks.example.com/scenarios/demo/run -H "authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{"mode":"live","speed":10}'
cd mocks/harness && uv run python smoke.py --url-template 'https://{name}.mocks.example.com' --host fix.mocks.example.com
```

Desk Chat asks for its access key once: open `https://chat.mocks.example.com/?key=<deskKey>` with the
`deskKey` from ib's `/_admin/credentials`.

**API keys** (see "Access" in `mocks/README.md`): the bootstrap keys, webhook secrets and FIX password
are derived from `MOCK_KEY_SECRET`, an SSM SecureString that `terraform/shared` generates
(`/<prefix>/key-secret`), so they survive stops, restarts and snapshot restores. Keys issued with
`POST /_admin/keys` and revocations live in each system's database, so the snapshot carries them into
the next session. To change every bootstrap key at once, replace the secret and restart:
`terraform -chdir=infra/terraform/shared apply -replace=random_password.key_secret` (workspace `demo`),
then `infra/up.sh demo`.

The smoke script calls the systems directly, so run it from an address in `allowed_cidrs`. If your
IP changes, update `allowed_cidrs` and run `up.sh` again.

* **Offline checks** (no AWS needed), in each of `shared`, `vendor` and `modules/vendor`:
  `terraform init -backend=false && terraform validate && terraform test` (in the `default` workspace).

## Cost (us-east-2, approximate)

Per hour **while running**:

| System | Pieces | $/hour |
| --- | --- | --- |
| gateway, wematch, sfp, custodian | each: ALB ~0.025, its 2 public IPv4 0.01, Fargate 0.25 vCPU / 1 GB ~0.015, task IP 0.005 | each ~0.055 |
| ib | the above + the Desk Chat task and its IP | ~0.075 |
| ngt | the above + NLB and its 2 IPs | ~0.09 |
| control | the above + NAT gateway, minus the task IP | ~0.095 |
| **All seven** | | **~0.50** (a 3-hour session ≈ $1.50) |

**While stopped:** ~$0.50–1/month. That's the hosted zone ($0.50) plus cents for ECR images, logs and
snapshots. The admin token and key secret are SSM SecureStrings (free), and mock-control's Elastic IP exists only
during a session. The auto-stop's CodeBuild runs, Lambda and Scheduler cost fractions of a cent per
session. $100 of credits covers roughly 190 hours with every system on, or far more with a
few.

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

9. ~~mock-control's own routes were open~~ (fixed Oct 28): `/reset`, `/clock`, `/faults` and
   `/scenarios` need an operator key (`sim:operate`); an observer key can only read.
10. Desk Chat's human API now needs the desk key, but who you act as is still the `X-User` switcher:
   one key plays every persona, which the demo wants. For per-person logins, put ALB + Cognito in front
   of `chat` and `ib`.
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
deploys via OIDC (running `up.sh`). Most of these belong with the platform, not the mocks.
