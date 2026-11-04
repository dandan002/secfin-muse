# LoanDesk AI: AWS hosting

Terraform for the desk (the LoanDesk platform) and the spec's **market account** of mocks. Each system
(`platform/api`, and each `mocks/<name>` with its own lockfile, Dockerfile and image) is hosted the way
a real one would be: its own image registry, network, database, compute and Terraform state. The
systems share nothing at runtime. They meet only over public HTTPS, the way a desk connects to its
vendors.

There are no load balancers, certificates or Route 53 zones. Everything public goes through
**Cloudflare** (free plan) on `loandesk.dev`: each task runs a `cloudflared` container that dials out
to its own Cloudflare Tunnel, and Cloudflare terminates HTTPS for the system's hostname and sends
requests down the tunnel. No task opens an inbound port, and starting or stopping a system needs
no Cloudflare credentials.

Systems run **only during a session**: `infra/up.sh` starts the ones you name, `infra/down.sh` stops
them, and an auto-stop inside AWS stops everything when the session's time is up or the month's
spend crosses a guard. Each system saves its database as it stops and picks up where it left off
on the next start. Standby costs cents a month; the Cloudflare side is free.

```
 browser ──► desk.loandesk.dev, api.loandesk.dev ──┐
 clients ──► mock-<name>.loandesk.dev ─────────────┤   Cloudflare (HTTPS, DNS)
                                                   │
                         one tunnel per ECS service, dialled out from the task
                                                   ▼
 ┌────────────────────────────────┐   ┌──────────────────────────────────────────────┐
 │ desk (the platform)            │   │ control · ib (+ Desk Chat) · gateway · ngt ·  │
 │  own VPC                       │   │ wematch · sfp · custodian · analytics          │
 │  task: api + pg + cloudflared  │   │  each: own VPC · task: mock + pg + cloudflared │
 │  task: web (nginx) + cloudflared│  │                                                │
 └────────────────────────────────┘   └──────────────────────────────────────────────┘
```

| Hostname | System |
| --- | --- |
| `desk.loandesk.dev` | the desk's web app (`platform/web`, nginx) |
| `api.loandesk.dev` | the desk's API (`platform/api`), behind Clerk sign-in |
| `mock-control`, `mock-ib`, `mock-chat`, `mock-gateway`, `mock-ngt`, `mock-wematch`, `mock-sfp`, `mock-custodian`, `mock-analytics` `.loandesk.dev` | the mocks (Desk Chat is `mock-chat`) |

Hostnames sit one level under the domain because Cloudflare's free certificate covers `*.loandesk.dev`
only. The catalog is `terraform/common/catalog.tf`.

Also in the zone, outside Terraform: Clerk production's records (`clerk`, `accounts`, `clkmail` and two
DKIM CNAMEs, DNS-only), and the apex `loandesk.dev`, a proxied placeholder whose redirect rule sends
every path to `desk.loandesk.dev` (Clerk's paths, such as `/sign-in`, resolve against the apex).

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
| `terraform/edge` | The public side, on Cloudflare: a tunnel and proxied DNS name per ECS service, and each tunnel's token in SSM for its task. Free; apply once with `infra/edge.sh`. (The `demo` environment's were made through the Cloudflare API with the same names and SSM paths; import them before managing them here.) | workspace `<env>` |
| `terraform/shared` | The simulator's admin token and API-key secret, an optional LLM key, the snapshot bucket, the Postgres image registry, and the auto-stop (CodeBuild job, Scheduler role, credit-guard budget). Nothing on a request path. | workspace `<env>`, e.g. `demo` |
| `terraform/vendor` | One system, the desk or a mock. The workspace picks which one. | workspace `<env>-<system>`, e.g. `demo-desk`, `demo-ngt` |
| `terraform/modules/vendor` | The system: its own ECR repos, VPC (2 AZs, public subnets), ECS cluster + Fargate service (the app, its own Postgres 16 and `cloudflared`), a second service for its static UI if it has one, IAM, logs. | — |
| `terraform/common` | Variables and the catalog of systems for every root (symlinked), so one `<env>.tfvars` drives everything. | — |
| `postgres/` | The Postgres image every task runs: the official image plus the snapshot save/restore hooks. | — |
| `edge.sh`, `up.sh`, `down.sh`, `status.sh` | Set up the Cloudflare side, start systems for a session (and arm the auto-stop), stop them, show what's on. | — |
| `destroy.sh` | Remove systems entirely, base included. Rarely needed: `down.sh` already takes a system to ~$0. | — |

Per-system design points:

* **Exactly one task, stop-then-start deploys** (`min 0% / max 100%`). Each system keeps its scheduler,
  WebSocket hub and faults in memory, so two copies would double-fire fills, persona replies and
  settlements. A deploy means about a minute of downtime for that one system.
* **Own Postgres, inside its task.** Each task runs a `postgres:16` container next to the app. The app
  waits for it to be healthy and reaches it over the task's loopback. No RDS to create or pay for.
* **Graceful stop, resumed sessions.** When a task stops, ECS stops the app first, then gives Postgres
  up to 120 s. It `pg_dump`s to `s3://loandesk-<env>-snapshots-<account>/<system>.sql.gz` and shuts
  down. The next task restores that dump before it accepts connections. `up.sh --fresh` discards a
  system's snapshot and starts it from the seed. The bucket is versioned, so an older dump stays
  recoverable for 30 days. If a restore fails, the task fails rather than starting empty and
  overwriting the snapshot at the next stop.
* **On or off** (`running`). The base (VPC, security group, ECR, logs, IAM) is free and always there.
  The task and its service exist only while running, so a start is a quick apply and a stop leaves
  nothing billing but storage.
* **In through the tunnel, nothing else.** The task's security group has no inbound rule. `cloudflared`
  starts once the app's own health check (`/health`, run by ECS) passes, and carries the system's
  hostname to `127.0.0.1:<port>`. While a system is stopped, its hostname answers with Cloudflare's 530.
* **Access is by key, not by address.** There are no IP allowlists: every mock's business API needs
  its API key and `/_admin` the admin token (see "Access" in `mocks/README.md`), and the desk's API
  needs a Clerk session token (`clerk_issuer`; the desk won't start without one).
* **Out through a public IP.** Tasks run in public subnets (no NAT to pay for) and reach Cloudflare,
  ECR, S3 and each other's public hostnames directly. mock-gateway calls its own simulated
  counterparties at `127.0.0.1`.
* **FIX isn't hosted.** A tunnel carries HTTP; mock-ngt's FIX port stays inside its task. Use FIX
  against the local stack.
* **Own image, own version.** Each system has its own ECR repos (`loandesk-<env>/<name>`, plus
  `desk-web` and `ib-chat` for the UIs), built from its project's Dockerfiles (the desk's API from
  `platform/api/Dockerfile` with the repo root as context; its web app from `platform/web`, with the
  API's URL and Clerk's publishable key baked in). The image tag is the last commit touching that
  project or the libs, so redeploying one system never rebuilds or restarts another.

## Sessions

Prereqs: Terraform ≥ 1.11, Docker, zip, the AWS CLI with credentials for the account, a versioned
S3 bucket for state, and a Cloudflare zone for the domain. Setting up the edge needs a Cloudflare API
token with **Account: Cloudflare Tunnel Edit** and **Zone (the domain): DNS Edit**; sessions don't.

```bash
cd infra/terraform
cp backend.hcl.example backend.hcl     # bucket, region
cp example.tfvars demo.tfvars          # domain, state_bucket, Clerk issuer and publishable key
cd ../..
export AWS_PROFILE=...                 # aws login --profile ... first; credentials last 12 h
export CLOUDFLARE_API_TOKEN=...        # only for edge.sh

infra/edge.sh demo                     # once: tunnels, DNS names, tunnel tokens (free, stays put)
infra/up.sh demo desk                  # the platform: API + web app, auto-stop in 3 h
infra/up.sh demo                       # every system
infra/up.sh demo ngt ib control        # only the mocks this session needs
infra/up.sh demo --for 90m             # again later: moves the stop time (and starts any named systems)
infra/up.sh demo --fresh sfp           # start sfp from the seed instead of its saved session
infra/status.sh demo                   # what's on, its URLs, when it stops
infra/down.sh demo ngt                 # stop one system
infra/down.sh demo                     # stop everything and cancel the auto-stop
```

`up.sh` applies the shared stack, arms the auto-stop, builds the Postgres image if it changed, then
starts the systems in parallel (logs in `infra/out/`). Docker runs with its own temporary config, so
the ECR login never touches the macOS keychain. Per system it builds and pushes the image only if that
version isn't in ECR yet, then applies with `running=true`. For the desk it also builds the web app's
image (`VITE_API_URL=https://api.<domain>`, the Clerk publishable key from the tfvars). mock-control
tolerates systems that are off: it reports them as unreachable and
its 5-second clock push catches each one up when it starts. Set `desk_sync = true` once the mocks
run, so the desk polls them.

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
KEY=$(curl -s https://mock-control.loandesk.dev/_admin/credentials -H "X-Admin-Token: $MOCK_ADMIN_TOKEN" | jq -r .operatorKey)
curl -XPOST https://mock-control.loandesk.dev/scenarios/demo/run -H "authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{"mode":"live","speed":10}'
cd mocks/harness && uv run python smoke.py --url-template 'https://mock-{name}.loandesk.dev'
```

Desk Chat asks for its access key once: open `https://mock-chat.loandesk.dev/?key=<deskKey>` with the
`deskKey` from ib's `/_admin/credentials`.

**API keys** (see "Access" in `mocks/README.md`): the bootstrap keys, webhook secrets and FIX password
are derived from `MOCK_KEY_SECRET`, an SSM SecureString that `terraform/shared` generates
(`/<prefix>/key-secret`), so they survive stops, restarts and snapshot restores. Keys issued with
`POST /_admin/keys` and revocations live in each system's database, so the snapshot carries them into
the next session. To change every bootstrap key at once, replace the secret and restart:
`terraform -chdir=infra/terraform/shared apply -replace=random_password.key_secret` (workspace `demo`),
then `infra/up.sh demo`.

* **Offline checks** (no AWS or Cloudflare needed), in each of `shared`, `edge`, `vendor` and
  `modules/vendor`: `terraform init -backend=false && TF_WORKSPACE=default terraform test`.

## Cost (us-east-2, approximate)

Per hour **while running**: Fargate (0.25 vCPU / 1 GB, ~$0.012) plus the task's public IPv4
($0.005), so **~$0.017 per mock**, **~$0.03 for ib** (two tasks) and **~$0.04 for the desk** (0.5
vCPU API plus its web task). Every system on is about **$0.20/hour** (a 3-hour session ≈ $0.60).
Cloudflare (tunnels, DNS, HTTPS) is free.

**While stopped:** cents a month for ECR images, logs and snapshots. The admin token, key secret and
tunnel tokens are SSM SecureStrings (free). The auto-stop's CodeBuild runs, Lambda and Scheduler cost
fractions of a cent per session.

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
   one key plays every persona, which the demo wants. For per-person logins, put Cloudflare Access in
   front of `mock-chat` and `mock-ib`.
11. FIX is plaintext TCP with the password in tag 554, and isn't hosted (a tunnel carries HTTP only).
12. LLM `replay` mode has nothing to replay in the cloud: `mocks/ib/cassettes/` isn't in the repo or the
    image, and `record` writes to the task's ephemeral disk. Bake a cassette into the image (or use S3)
    before relying on LLM personas there.
13. The spec puts mock-ngt on DynamoDB. It now has its own Postgres, like every system. Swapping in
    DynamoDB is possible per system later and no longer affects the others.

## Not yet built (from the spec's cloud section)

The second "firm" account, PrivateLink as an alternative to public endpoints, API Gateway with mTLS
and usage plans for counterparties, MSK, SFTP (Transfer Family) for locate and EOD files, OTel to
X-Ray, FIX over the internet (a tunnel carries HTTP only), and GitHub Actions deploys via OIDC
(running `up.sh`).
