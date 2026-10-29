# System-one models for locate review: evaluation (Oct 29, 2026)

**Outcome: not adopted. The locate review stays on Muse (Spark, escalating to Wonder).** This write-up keeps what
was tried and what came out of it; the integration was removed. The cases and raw results are in `data/`.

## The idea

A locate review on Muse costs about $0.03 (Spark alone) to $0.17 (Spark, then Wonder). Most of that is
Wonder escalations. A *system-one* model is a decision model: given a state and fixed questions, it returns a
calibrated probability for every option in one forward pass, with no generated text, for a fraction of a cent.
The plan was:

```
gather ─► system one ─(confident)─► check ─► propose ─► trader
              └─(unsure)─► Spark ─► Wonder (as today)
```

and to let it decide alone only when it is confident, keeping Muse for everything else.

## Models tried

All through OpenRouter's System One endpoint (Baseten direct returned 403 on inference for our key).

| Model | Maker | Size | Context | Price (input, per 1M tokens; output free) | Cost per locate case |
|---|---|---|---|---|---|
| `baseten/mammoth-1` | Base Labs (Baseten), on Qwen3.5-27B | 27B | 16k | $0.10 | ~$0.00014 |
| `baseten/elephant-1` | Base Labs (Baseten), on Qwen3.5-9B | 9B | 16k | ~$0.035 | ~$0.00005 |
| `typesafe/jev-1.14` | TypeSafe | n/a | 262k | $0.042 | ~$0.00006 |

API (`POST https://openrouter.ai/api/v1/systemone`, `Authorization: Bearer <key>`):

```json
{"model": "baseten/mammoth-1",
 "state": {"case": "..."},
 "questions": {"size": {"type": "score", "instructions": "...", "criteria": ["none", "a quarter", "half", "..."]},
               "fee":  {"type": "score", "instructions": "...", "criteria": ["at the floor", "5% above", "..."]},
               "senior": {"type": "noul", "instructions": "..."}}}
```

Question types: `noul` (yes/no, answer `noul` = P(yes)), `choice` (criteria `{name: description}`, answer
`choice`, `probabilities`, `confidence`), `score` (ordered criteria, lowest first; answer `score` = expected
level, `probabilities`, `confidence`). Baseten direct is `https://inference.baseten.co/v1/systemone` with
`Authorization: Api-Key <key>` and model `decision-models/baseten-mammoth-27b-latest`.

## What was built and tested (removed, not merged)

* **Gateway:** `ModelGateway.systemone()` on the same recordings, budget and spend meter as Muse.
* **Graph:** a `system1` step after `gather`, plus a `handoff` back to Spark when its answer fails any check.
* **Modes:** `LOANDESK_SYSTEM1_MODE=off | shadow | decide`, with `LOANDESK_SYSTEM1_MODEL` and `LOANDESK_SYSTEM1_CONFIDENT` (0.9).
  * `shadow`: it answers every review alongside Muse, and the run records where the two agree.
  * `decide`: a confident answer becomes the proposal, unless the case is Hot or over counterparty limits.
* **Numbers come from the desk.** The model picks levels and the desk turns them into a quantity (a share of
  `fillableQty`, the least of request, counterparty room and availability) and a fee (the floor plus a markup).
  The floor and the counterparty limits hold by construction. The hard checks and the trader gate are unchanged.
* **Final input:** a desk brief (what an SBL desk does and the risks it manages) plus an 8-line summary of the
  case. Counterparty-supplied text is left out entirely.
* **Tests:** 4 tests (shadow, confident decide, unsure hand-off, answers to desk numbers); 44 passed with it in.

## Results

The cases are the 8 locates Muse reviewed live that day, on the mocks running on Postgres (`data/cases.json`).
Each has the full market picture, the counterparty room, the pricing and Muse's final decision. Muse's
decision is used as the label. **It is not ground truth.** Muse was itself 0.62 to 0.85 confident on these
cases.

| # | Case | Request | Fillable | Muse |
|---|---|---|---|---|
| 0 | WARM, deep supply (ACT) | 200k | 200k | approve 200k @ 23.6 (floor) |
| 1 | WARM, 81% util, 4 lenders (AD) | 300k | 300k | partial 150k @ 295 (Wonder) |
| 2 | WARM, 1 lender (AA) | 500k | 500k | approve 500k @ 101.5 |
| 3 | per-name cap (AFRM) | 4M | 1M | partial 1M @ 270 (Wonder) |
| 4 | HOT, 1 lender (ACAD) | 300k | 300k | partial 150k @ 435.7 (Wonder) |
| 5 | WARM, fee up 31% (FICO) | 200k | 200k | partial 100k @ 275 (Wonder) |
| 6 | locate limit caps it (SNDK) | 200k | 156,817 | partial 156,817 @ 85 (Wonder) |
| 7 | crosses our own inventory (SLV) | 10k | 10k | approve 10k @ 42 |

### 1. First design: raw JSON, thin framing (mammoth-1, live shadow run on cases 0–4)

Muse cost $0.46 for the five reviews, against $0.0013 for mammoth-1. Mammoth-1's top decision probability was
0.65 to 0.77, so it was **never confident**. Its "partial" contradicted its own size answer ("all of it") in 4
of 5 cases.

### 2. Question variants (mammoth-1, cases 0–4)

* **Single 0–100% size scale:** the probabilities sat around "half" for every case (top p 0.2 to 0.3), whatever Muse did.
* **Compact text summary in place of raw JSON:** the same answers, at about 0.7k tokens in place of 2.4k.
* **Yes/no "is the full request safe?":** 0.12 to 0.20 on WARM, 0.02 on HOT and limit-capped. It **separates
  risky from safe** but not "fill in full" from "cut".

### 3. Three models, with and without the desk brief (8 cases)

Size is scored against `fillableQty`, in levels from decline to all of fillable. An over-approval means the
model sized above Muse.

| Model | Framing | Within one level of Muse | Over-approved | Under-approved | Decides alone at p ≥ 0.75 (n, matching Muse) |
|---|---|---|---|---|---|
| mammoth-1 | thin | 4/8 | 1 | 5 | 0 |
| mammoth-1 | brief | 5/8 | **0** | 5 | 0 |
| elephant-1 | thin | 4/8 | 2 | 2 | 0 |
| elephant-1 | brief | 4/8 | **0** | 4 | **3, 3** |
| Jev 1.14 | thin | 5/8 | 3 | 3 | 1, 1 |
| Jev 1.14 | brief | **8/8** | 2 | 3 | 1, 1 |

**The brief helps.** It raised confidence for all three models. It removed over-approvals for mammoth-1 and
elephant-1, and it widened the gap in "full is safe?" between the cases Muse filled and the ones it cut:

| Model | P(full is safe), thin: Muse filled the whole request / Muse cut | With the brief: filled / cut |
|---|---|---|
| mammoth-1 | 0.27 / 0.11 | 0.33 / 0.03 |
| elephant-1 | 0.53 / 0.29 | 0.49 / 0.05 |

**The results are sensitive to wording.** Re-running the same design through the graph code, which words the
size options slightly differently, dropped elephant-1's confident cases at 0.75 from 3 to 1 (only case 7).
Jev asked for a senior reviewer on every case (p 0.71 to 0.87), possibly because the brief opens with "You
are the senior trader".

### 4. Two calls: decide first, then size only if cutting (8 cases)

The first call asks approve, partial or reject. The second is told the desk is cutting and picks a quarter,
half or three quarters of fillable.

| Model | Decision p | Agrees with Muse | Decides alone at joint p ≥ 0.75 |
|---|---|---|---|
| mammoth-1 | 0.67–0.91 | 3/8 | 0 |
| elephant-1 | 0.75–0.98 | 4/8 | 1 (case 7) |
| Jev 1.14 | 0.82–0.97 | 4/8 | 1 (case 7) |

* **Contradictions gone:** this design no longer contradicts itself, and the decision call is much more confident.
* **The models always cut:** all three said **partial on every case except case 7**, including the four Muse
  filled in full (0, 2, 3, 6), at up to 0.93 confidence.
* **Sizing tells us little:** on the cases Muse cut, all three picked "half", which matches Muse, but they pick half almost every time.

## Conclusions

1. **Safe but conservative.** In every design and model, the errors were almost all **under-approvals**:
   - With the brief, mammoth-1 and elephant-1 never approved more than Muse; Jev did twice.
   - The cost of an under-approval is lost fee and client friction, not fails or Reg SHO exposure.
2. **No cost saving here.** The rules engine already auto-approves the easy cases (ETB, GC). The ones that
   reach the agent are borderline by construction, and that is where these models stay unsure or cut by
   default. At a confidence bar that avoids bad calls, about 1 review in 8 would have skipped Muse.
3. **Not an equivalent of Muse's output.** The models pick from fixed levels (5 sizes, 4 fees), so the answers are coarse:
   - On SNDK, Muse proposed 156,817 at 85 bps, the counterparty's exact room; elephant-1 proposed 78,000 at 89.2.
   - They give no reasoning, so the card carries numbers and probabilities, not "two lenders, fee up 31%".

## If this is revisited

* **Collect real labels:** run `shadow` for a few hundred reviews, and keep the **trader's final decision** on
  each card, not Muse's proposal. Eight cases is not enough to decide.
* **Calibrate on those labels:** worked desk examples in the brief, or a fine-tune (elephant-1 and mammoth-1
  are open weights under MIT).
* **Try a narrower job:**
  - a router: send risky cases straight to Wonder, skipping Spark;
  - a check on Muse's cuts, rather than a replacement for Muse.
* **A cheaper saving that needs no new model:** a one-shot Muse review. Today each model re-fetches market, fee
  history, counterparty and trade history that `gather` already put in the prompt, and Wonder repeats all of it
  after an escalation.

## Files

* **`data/cases.json`:** the 8 cases (synthetic mock data) with Muse's decisions.
* **`data/compare-results.json`:** raw answers for 3 models × 2 framings × 8 cases (section 3).
* **`data/two-stage-results.json`:** raw answers for the decide-then-size design (section 4).
