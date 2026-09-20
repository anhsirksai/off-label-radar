# RUNBOOK — Off-Label Radar

## 0. What you need

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- A **TypeSafe AI (Jev) API key** — https://typesafe.ai · docs: https://docs.typesafe.ai/api
- Optional: a free **NCBI API key** (PubMed 3 → 10 req/s) — https://www.ncbi.nlm.nih.gov/account/settings/
- Outbound HTTPS to: `clinicaltrials.gov`, `dailymed.nlm.nih.gov`, `rxnav.nlm.nih.gov`, `eutils.ncbi.nlm.nih.gov`, `api.typesafe.ai`

## 1. Local build (5 minutes)

```bash
git clone https://github.com/anhsirksai/off-label-radar.git
cd off-label-radar

cp .env.example .env            # then open .env and set:
#   TYPESAFE_API_KEY=...        (required)
#   CONTACT_EMAIL=you@...       (required by NCBI; also goes in the User-Agent)
#   NCBI_API_KEY=...            (optional, faster PubMed)

uv sync                          # creates .venv with FastAPI, uvicorn, httpx …

# start the app
uv run --env-file .env uvicorn main:app --host 127.0.0.1 --port 8080 --reload
# → open http://127.0.0.1:8080
```

The SQLite DB (`radar.db`) is created on first start next to the code. Delete it to start clean.

### Headless / CLI (no browser)

```bash
# by drug — comma or space separated ingredient names (generic names; RxNorm resolves brand names too)
uv run --env-file .env python cli.py colchicine ketamine semaglutide

# by condition — plain text
uv run --env-file .env python cli.py --condition "alopecia areata"

# ignore the Jev cache and re-decide every pair (counters/cost actually move)
uv run --env-file .env python cli.py colchicine --force
```

Progress prints every 5 s: `[stage] done/total jev=calls tok=tokens message`.

## 2. Using the app

**By drug** tab → seed drugs → *Run pipeline*. **By condition** tab → e.g. `treatment-resistant depression` → *Find drugs for condition*. Results:

- Condition mode shows two buckets: 🧪 **being tested off-label** (the repurposing signal) and ✅ **label already covers it** (approved use / standard of care). Chips at the top summarise drugs; click one to filter.
- Click any row → drawer with the **label text vs. trial**, PubMed abstracts, and Jev's full probability distributions plus the exact `state` JSON Jev saw.
- Flags: `comparator?` = Jev thinks the drug is background/control therapy (P(active) < 0.5); `disagree` = the heuristic and Jev conflict (your human-review / calibration queue); `on-label hint` = a trial condition appears verbatim in the label.
- ⬇ CSV exports every pair with all four Jev answers.
- ☐ **re-judge with Jev (ignore cache)** — fresh Jev calls for every pair; public-API fetches stay cached. Use this for a live demo.

### Demo tips

- Rehearse with a throwaway drug (e.g. `atorvastatin`) — anything you run gets cached, so keep your on-stage "new" drugs unseen.
- Stage run = cached seed drugs + 1–2 new drugs + *re-judge* ticked: fetch counters tick for the new drugs, the Jev counter and cost ticker run on all of them.
- Nice "existing drug, new use" rows from the seed set: colchicine → coronary artery disease, ketamine → treatment-resistant depression, semaglutide → alcohol use disorder, propranolol → PTSD.

## 3. Politeness / rate limits (don't get banned)

| Host | Documented ceiling | We run at | Notes |
|---|---|---|---|
| clinicaltrials.gov API v2 | ≤ 50 req/min | 30/min | JSON API, never HTML |
| DailyMed SPL services | ~1 req/s | 0.7/s | cached 30 d by setid |
| RxNorm (rxnav) | ≤ 20 req/s | 14/s | |
| PubMed E-utilities | 3 req/s (10 with key) | 70 % of that | sends `tool=` + `email=` as NCBI requires |
| TypeSafe / Jev | ~1200 req/min | 15/s | |

All hosts: identified `User-Agent`, exponential backoff, `Retry-After` honoured, 6 attempts, then the drug is skipped (never kills the run).

## 4. Configuration

Everything is read from the environment (see `.env.example`):

| Var | Default | Meaning |
|---|---|---|
| `TYPESAFE_API_KEY` | — | Jev key. If unset **and** `PROMPTQL_PLATFORM_API_URL` is set, Jev is reached via the PromptQL proxy instead. |
| `JEV_MODEL` | `jev-latest` | pin e.g. `jev-1.13.0` for reproducibility |
| `CONTACT_EMAIL` | — | identification for NCBI / User-Agent |
| `NCBI_API_KEY` | — | optional PubMed key |
| `OFF_LABEL_THRESHOLD` | `0.6` | gate on P(off-label) |
| `MAX_TRIALS_PER_DRUG` | `40` | newest N active trials per drug / condition |
| `MAX_DRUGS_PER_CONDITION` | `30` | condition mode: keep the N most-tested ingredients |
| `RADAR_DB` | `radar.db` | SQLite path |

## 5. Deploying inside a PromptQL VM (how the hosted copy runs)

- `off-label-radar.service` — systemd unit that runs uvicorn on `:8080` under the `promptql` user and loads `.env`.
  `sudo cp off-label-radar.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now off-label-radar`
- `publish_app.sh` — writes the PromptQL **App Artifact** (version 2, readiness `/readyz`) so the app opens inside PromptQL. There, Jev calls run as the visitor through the integration proxy (`__typesafe-api`) — no key in the VM.
- Health: `curl -i http://127.0.0.1:8080/readyz` → 204.

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Run button says *Open this app from inside PromptQL, or set TYPESAFE_API_KEY* | Local build without a key. Add `TYPESAFE_API_KEY` to `.env` and restart. |
| `Jev HTTP 401/403` | Bad or expired key. |
| `<drug>: no DailyMed Indications section found` | Not an FDA-approved US product (or a very odd label). No label → no diff → skipped. |
| Wrong label picked (e.g. Rybelsus vs Wegovy for semaglutide) | DailyMed is one label **per product**; we take the first clean match for the ingredient. Known gap — fix is to union the Indications sections across all labels. |
| Jev spend shows 0¢ | Cache hit: every pair already judged. Tick *re-judge (ignore cache)* or use `--force`. |
| Condition mode finds 0 drugs | Condition too rare/misspelt on CT.gov, or all interventions are devices/procedures. Try the MeSH wording. |
| PubMed `HTTP 429` backoffs | Add `NCBI_API_KEY`; lower `MAX_TRIALS_PER_DRUG`. |
| Want to start over | `rm radar.db*` and restart. |

## 7. Data provenance

- Label ground truth = the SPL **Indications & Usage** section (LOINC `34067-9`), sent to Jev verbatim; Jev is instructed to judge from that text only.
- Trials = CT.gov v2 studies with status RECRUITING / NOT_YET_RECRUITING / ACTIVE_NOT_RECRUITING / ENROLLING_BY_INVITATION.
- Every Jev request and response is stored in `jev_calls` — the drawer shows the exact state JSON per pair, so every decision is auditable.