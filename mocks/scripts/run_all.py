"""Run every mock as its own uvicorn process (no Docker needed).

    uv run python scripts/run_all.py                  # ports 8000-8060, FIX 9878
    uv run python scripts/run_all.py --base-port 18000 --db postgresql+asyncpg://postgres@localhost/mocks_test

Also importable: ``start_stack(...)`` is what the integration tests use.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
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


def start_stack(base_port: int = 8000, db_url: str | None = None, fix_port: int = 9878, log_dir: Path | None = None,
                extra_env: dict[str, str] | None = None, host: str = "127.0.0.1") -> Stack:
    urls = {name: f"http://{host}:{base_port + off}" for name, _, off in SERVICES}
    env = {**os.environ, **(extra_env or {})}
    if db_url:
        env["MOCK_DATABASE_URL"] = db_url
    for name, url in urls.items():
        env[f"MOCK_{name.upper()}_URL"] = url
    env["MOCK_NGT_FIX_PORT"] = str(fix_port)
    env["MOCK_NGT_FIX_HOST"] = host
    log_dir = log_dir or ROOT / "out" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stack = Stack(urls=urls, fix_port=fix_port)
    for name, module, off in SERVICES:
        log = log_dir / f"{name}.log"
        stack.logs[name] = log
        stack.procs.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", module, "--host", host, "--port", str(base_port + off),
             "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
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
                raise RuntimeError(f"{name} exited early; see {stack.logs[name]}:\n{stack.logs[name].read_text()[-3000:]}")
        time.sleep(0.3)
    if pending:
        stack.stop()
        raise RuntimeError(f"services did not become healthy: {sorted(pending)}")
    return stack


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-port", type=int, default=8000)
    ap.add_argument("--fix-port", type=int, default=9878)
    ap.add_argument("--db", default=None, help="SQLAlchemy URL (default: MOCK_DATABASE_URL or local postgres)")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    stack = start_stack(args.base_port, args.db, args.fix_port, host=args.host)
    for name, url in stack.urls.items():
        print(f"{name:10s} {url}/docs")
    print(f"{'fix':10s} tcp://{args.host}:{args.fix_port}")
    print("logs in", ROOT / "out" / "logs", "- Ctrl+C to stop")
    try:
        while all(p.poll() is None for p in stack.procs):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stack.stop()


if __name__ == "__main__":
    main()
