# Simulation Control: operator API

Not a stand-in for any external system: this is the simulator's own console. It owns the **sim
clock**, **resets** every system to a seed, injects **faults**, and runs **scenarios**. It is the
only component that talks to every system, and only over their public `/_admin` APIs with the
admin token, so the systems stay independent.

| | |
| --- | --- |
| Base | `https://control.<domain>` (local `http://localhost:8000`); OpenAPI at `/docs`, snapshot in [`openapi.json`](openapi.json) |
| Auth | `Authorization: Bearer <key>`: `operatorKey` (`sim:operate`) or `observerKey` (`sim:read`, read-only) from `GET /_admin/credentials`; operators only (IP allowlist). It calls the systems with `X-Admin-Token` (`MOCK_ADMIN_TOKEN`) |
| Connector | [`connector/`](connector): `SimControl` |

## Connector

```python
from control_connector import SimControl

async with SimControl("https://control.mocks.example.com", api_key=operator_key) as sim:
    await sim.reset(seed=20261027, mode="stepped", epoch="2026-10-27T08:55:00-04:00")
    run = await sim.run_scenario("demo", mode="stepped", wait=True)     # replays event for event
    await sim.set_fault("ngt", outage=True)                             # venue outage drill
    await sim.clear_faults()
    await sim.set_clock(mode="live", speed=10, paused=False)             # 10x real time
```

## Clock

| Method | Path | |
| --- | --- | --- |
| GET | `/clock` | `now`, `now_local` (New York), `mode`, `speed`, `paused`, `seed`, `sim_epoch`, `wall_anchor` |
| PUT | `/clock` | `{speed?, paused?, mode?}` |
| POST | `/clock/start` · `/clock/pause` | |
| POST | `/clock/jump` | `{to}`: set the time without running jobs |
| POST | `/clock/advance` | Stepped mode: `{seconds}` or `{to}`; moves time and tells every system to run its due jobs, in a fixed order |

**live**: sim time runs at `speed` × wall time. **stepped**: time moves only on `advance`, which
makes a seeded run reproducible event for event.

Control keeps the clock in its own database and **pushes** it to every system
(`PUT <system>/_admin/clock` with `{state, rev}`) on every change and every 5 s
(`MOCK_CLOCK_PUSH_S`). Each system stores the last clock it received in its own database and
ignores pushes older than one it already has (`rev`), so a restarted system resumes at once and
catches up on the next heartbeat. Requests may carry `X-Sim-Time` to pin "now" for that request.

## Reset and services

| Method | Path | |
| --- | --- | --- |
| POST | `/reset` | `{seed?, epoch?, mode, speed, paused}`: truncate every system and reseed. IDs, keys and passwords all derive from the seed |
| GET | `/services` | Each system's URL, `up`, label and disclaimer |

## Faults

| Method | Path | |
| --- | --- | --- |
| GET | `/faults` | Every system's fault config |
| PUT | `/faults/{system}` | `latency_ms`, `jitter_ms`, `error_pct` (HTTP 500s), `outage` (503s; FIX refuses logon), `drop_pct`, `dup_pct`, `reorder_pct` (outbound WebSocket events, FIX order/trade messages, webhooks) |
| DELETE | `/faults` | Clear all |

Fault decisions come from the seeded RNG, so a chaos drill replays. `/health` and `/_admin` stay
up during an outage.

## Scenarios

| Method | Path | |
| --- | --- | --- |
| GET | `/scenarios` · `/scenarios/{name}` | Summaries; full step list |
| POST | `/scenarios/{name}/run?wait=` | `{mode, speed?, seed?, reset}` → a run (`run-0001`) |
| GET | `/runs` · `/runs/{id}` | Status, current step and per-step log |
| POST | `/runs/{id}/stop` | |

Scenario files live in [`scenarios/`](scenarios): timed steps `{at, do: <system>.<verb>, args}`
or raw `http` calls. `demo.yaml` is the five-minute demo; `chaos.yaml` is the resilience drill.
`GET <system>/_admin/scenario` lists each system's verbs.

## Common `/_admin` API on every system

Every system serves `/health`, `/` (identity and disclaimer) and, with `X-Admin-Token`:
`GET|PUT /_admin/faults`, `POST /_admin/reset`, `POST /_admin/tick?until=`, `GET /_admin/jobs`,
`GET /_admin/events?after=&limit=` (its append-only event log), `GET|PUT /_admin/clock`,
`GET /_admin/scenario`, `POST /_admin/scenario/{verb}`, and `GET /_admin/credentials`.
