"""Write or check a system's committed OpenAPI snapshot (its ``openapi.json``).

    uv run python -m mockcore.openapi mock_ngt.app            # rewrite ./openapi.json
    uv run python -m mockcore.openapi mock_ngt.app --check    # exit 1 if it is stale
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path


def render(module: str) -> str:
    app = importlib.import_module(module).app
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("module", help="e.g. mock_ngt.app")
    ap.add_argument("--out", default="openapi.json")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    text, out = render(a.module), Path(a.out)
    if a.check:
        if not out.exists() or out.read_text() != text:
            print(f"{out} is stale: run python -m mockcore.openapi {a.module}", file=sys.stderr)
            return 1
        return 0
    out.write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
