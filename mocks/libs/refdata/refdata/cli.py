"""``refdata`` command: export the synthetic universe and counterparties for other teams.

    uv run refdata export --out out/refdata [--seed 20261027]
    uv run refdata resolve "QZNT US"
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from mockcore import rng

from . import identifiers
from .counterparties import OUR_FIRM, counterparties
from .universe import resolve, universe


def _export(out: Path, seed: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    insts = [i.to_json() for i in universe(seed)]
    (out / "instruments.json").write_text(json.dumps(insts, indent=1))
    with (out / "instruments.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(insts[0]))
        w.writeheader()
        for row in insts:
            w.writerow({**row, "tags": "|".join(row["tags"])})
    cps = [c.to_json(include_secrets=True) for c in counterparties(seed)]
    (out / "counterparties.json").write_text(json.dumps({"ourFirm": OUR_FIRM, "counterparties": cps}, indent=1))
    tiers: dict[str, int] = {}
    for i in insts:
        tiers[i["tier"]] = tiers.get(i["tier"], 0) + 1
    print(f"wrote {len(insts)} instruments {tiers} and {len(cps)} counterparties to {out}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="refdata")
    p.add_argument("--seed", type=int, default=20261027)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--out", type=Path, default=Path("out/refdata"))
    r = sub.add_parser("resolve")
    r.add_argument("query")
    args = p.parse_args(argv)
    rng.set_seed(args.seed)
    if args.cmd == "export":
        _export(args.out, args.seed)
    else:
        inst = resolve(args.query)
        if inst is None:
            hints = identifiers.suggest_fix(args.query)
            print(json.dumps({"error": "not found", "didYouMean": hints}))
            return 1
        print(json.dumps(inst.to_json(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
