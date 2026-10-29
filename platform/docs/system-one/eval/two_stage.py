"""Two calls: (1) approve / partial / reject; (2) only for partial, the size given that the desk is cutting."""
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
SP = Path(__file__).resolve().parent.parent / "data"
COMPARE = Path(__file__).with_name("compare.py")
g = {"__file__": str(COMPARE)}; exec(COMPARE.read_text().split("CONDS = ")[0], g)
cases, ask, BRIEF, summary = g["cases"], g["ask"], g["BRIEF"], g["summary"]
MODELS = ["baseten/mammoth-1", "baseten/elephant-1", "typesafe/jev-1.14"]
DECIDE = {"decision": {"type": "choice", "instructions": "What should the desk do with this locate request?",
          "criteria": {"approve": "Approve all of fillableQty: the supply behind it is sound.",
                       "partial": "Approve only part of fillableQty: there is a supply, utilization or fee issue.",
                       "reject": "Decline: too little realistically gettable supply."}}}
SIZE = {"size": {"type": "score", "instructions": "The desk has decided to approve only part of fillableQty. "
                 "How much of fillableQty should it approve?",
                 "criteria": ["about a quarter", "about half", "about three quarters"]}}
def fill(c):
    f = c["facts"]; room = f["counterparty"] or {}
    return max(min(f["locate"]["qtyRequested"], room.get("maxQty", 0), f["market"]["available"]), 0)
def muse(c):
    q, fl = c["muse"]["quantity"] or 0, fill(c)
    return ("reject", None) if q == 0 else ("approve", None) if q >= fl else ("partial", round(q / fl * 4) - 1)
def run(job):
    m, i = job; f = cases[i]["facts"]; base = {"desk_brief": BRIEF, "case": summary(f)}
    d = ask(m, base, DECIDE)
    s = ask(m, {**base, "desk_decision": "approve only part of fillableQty"}, SIZE)
    return job, (d, s)
with ThreadPoolExecutor(8) as ex:
    res = dict(ex.map(run, [(m, i) for m in MODELS for i in range(len(cases))]))
json.dump({f"{m}|{i}": v for (m, i), v in res.items()}, open(SP / "two-stage-results.json", "w"))
SZ = ["1/4", "1/2", "3/4"]
for m in MODELS:
    print(m); cost = 0; agree = 0; conf = {0.6: [], 0.75: [], 0.9: []}
    for i, c in enumerate(cases):
        (d, s) = res[(m, i)]; cost += d["usage"]["cost"] + s["usage"]["cost"]
        dp = d["answers"]["decision"]["probabilities"]; k = max(dp, key=dp.get)
        sp = s["answers"]["size"]["probabilities"]; sk = int(max(sp, key=sp.get))
        mk, ms = muse(c)
        ok = k == mk and (k != "partial" or sk == ms); agree += ok
        p = dp[k] * (sp[str(sk)] if k == "partial" else 1)  # joint confidence: decision, then size if cutting
        for t in conf:
            if p >= t: conf[t].append(ok)
        lbl = k + (f" {SZ[sk]}" if k == "partial" else "")
        mlbl = mk + (f" {SZ[ms]}" if mk == "partial" else "")
        print(f"   {c['label']:28} muse {mlbl:13} | s1 {lbl:13} decision p {dp[k]:.2f}"
              + (f", size p {sp[str(sk)]:.2f}" if k == "partial" else "") + f" -> joint {p:.2f} {'✓' if ok else '✗'}")
    print(f"   agree {agree}/8 | would decide (n, right): " + "  ".join(f"@{t}: {len(v)}, {sum(v)}" for t, v in conf.items()) + f" | ${cost:.4f}\n")
