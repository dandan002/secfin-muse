"""Run every mock system as its own process, from its own project environment, with its own database.

    cd mocks/harness
    uv run python run_all.py                  # ports 8000-8060, FIX 9878, dbs loandesk_<service>
    uv run python run_all.py --base-port 18000 --db 'postgresql+asyncpg://postgres@localhost/test_{service}'

Each system is a separate project (``mocks/systems/<name>``); its environment is synced with
``uv sync --frozen`` before it starts, so nothing here imports a system's code.

``{service}`` in the database URL is replaced per mock; missing databases are created.

Also importable: ``start_stack(...)`` is what the integration tests use.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]  # mocks/
SYSTEMS = ROOT / "systems"
OUT = Path(__file__).resolve().parent / "out"
DEFAULT_DB = "postgresql+asyncpg://postgres@localhost:5432/loandesk_{service}"
SERVICES = (  # name, module, port offset
    ("control", "mock_control.app:app", 0),
    ("ib", "mock_ib.app:app", 10),
    ("gateway", "mock_gateway.app:app", 20),
    ("ngt", "mock_ngt.app:app", 30),
    ("wematch", "mock_wematch.app:app", 40),
    ("sfp", "mock_sfp.app:app", 50),
    ("custodian", "mock_custodian.app:app", 60),
)


@dataclass
class Stack:
    urls: dict[str, str]
    fix_port: int
    procs: list[subprocess.Popen] = field(default_factory=list)
    logs: dict[str, Path] = field(default_factory=dict)

    def stop(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        deadline = time.time() + 10
        for p in self.procs:
            try:
                p.wait(max(deadline - time.time(), 0.1))
            except subprocess.TimeoutExpired:
                p.kill()


def db_urls(template: str) -> dict[str, str]:
    """Per-service database URLs from a template containing ``{service}``."""
    return {name: template.replace("{service}", name) for name, _, _ in SERVICES}


def ensure_databases(template: str) -> None:
    """Create each service's database if it doesn't exist (connects to the server's ``postgres`` db)."""
    import asyncpg

    async def go() -> None:
        for url in set(db_urls(template).values()):
            server, name = url.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)
            conn = await asyncpg.connect(f"{server}/postgres")
            try:
                if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
                    await conn.execute(f'CREATE DATABASE "{name}"')
            finally:
                await conn.close()

    asyncio.run(go())


def sync_systems() -> dict[str, Path]:
    """Make sure each system's own environment exists; return its interpreter."""
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}  # don't point uv at the harness venv
    pythons = {}
    for name, _, _ in SERVICES:
        project = SYSTEMS / name
        subprocess.run(["uv", "sync", "--frozen", "--no-dev", "--quiet"], cwd=project, env=env, check=True)
        pythons[name] = project / ".venv" / "bin" / "python"
    return pythons


def start_stack(base_port: int = 8000, db_url: str | None = None, fix_port: int = 9878, log_dir: Path | None = None,
                extra_env: dict[str, str] | None = None, host: str = "127.0.0.1") -> Stack:
    urls = {name: f"http://{host}:{base_port + off}" for name, _, off in SERVICES}
    env = {**os.environ, **(extra_env or {})}
    dbs = db_urls(db_url or env.get("MOCK_DATABASE_URL") or DEFAULT_DB)
    for name, url in urls.items():
        env[f"MOCK_{name.upper()}_URL"] = url
    env["MOCK_NGT_FIX_PORT"] = str(fix_port)
    env["MOCK_NGT_FIX_HOST"] = host
    log_dir = log_dir or OUT / "logs"
    pythons = sync_systems()
    env.pop("VIRTUAL_ENV", None)
    log_dir.mkdir(parents=True, exist_ok=True)
    stack = Stack(urls=urls, fix_port=fix_port)
    for name, module, off in SERVICES:
        log = log_dir / f"{name}.log"
        stack.logs[name] = log
        stack.procs.append(subprocess.Popen(
            [str(pythons[name]), "-m", "uvicorn", module, "--host", host, "--port", str(base_port + off),
             "--log-level", "warning"],
            cwd=SYSTEMS / name, env={**env, "MOCK_DATABASE_URL": dbs[name]},
            stdout=log.open("w"), stderr=subprocess.STDOUT,
        ))
    deadline = time.time() + 90
    pending = dict(urls)
    while pending and time.time() < deadline:
        for name, url in list(pending.items()):
            try:
                if httpx.get(url + "/health", timeout=1).status_code == 200:
                    del pending[name]
            except httpx.HTTPError:
                pass
        for p, (name, _, _) in zip(stack.procs, SERVICES, strict=True):
            if p.poll() is not None:
                stack.stop()
                tail = stack.logs[name].read_text()[-3000:]
                raise RuntimeError(f"{name} exited early; see {stack.logs[name]}:\n{tail}")
        time.sleep(0.3)
    if pending:
        stack.stop()
        raise RuntimeError(f"services did not become healthy: {sorted(pending)}")
    return stack


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-port", type=int, default=8000)
    ap.add_argument("--fix-port", type=int, default=9878)
    ap.add_argument("--db", default=None, help="SQLAlchemy URL; {service} is replaced per mock "
                    f"(default: MOCK_DATABASE_URL or {DEFAULT_DB})")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    ensure_databases(args.db or os.environ.get("MOCK_DATABASE_URL") or DEFAULT_DB)
    stack = start_stack(args.base_port, args.db, args.fix_port, host=args.host)
    for name, url in stack.urls.items():
        print(f"{name:10s} {url}/docs")
    print(f"{'fix':10s} tcp://{args.host}:{args.fix_port}")
    print("logs in", OUT / "logs", "- Ctrl+C to stop")
    try:
        while all(p.poll() is None for p in stack.procs):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stack.stop()


if __name__ == "__main__":
    main()
