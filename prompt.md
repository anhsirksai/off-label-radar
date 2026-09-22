Build prompt — Off-Label Radar (CT.gov × DailyMed × PubMed → Jev)

·

Bot
Build prompt — "Off-Label Radar": live drug-repurposing candidate scanner
You are building a small, self-contained web app that finds live clinical trials testing an approved drug outside its FDA-labeled indications, has Jev (TypeSafe AI) judge each candidate, and shows a ranked, evidence-backed table. The core signal — CT.gov trial conditions vs. DailyMed labeled indications — is not published anywhere today.

Optimize for: working end-to-end fast, polite/legal data access (no bans), cheap models, and every decision traceable to its evidence.

0. Non-negotiables
APIs only, never HTML scraping. CT.gov v2, DailyMed web services, NCBI E-utilities, RxNorm all have public JSON/XML APIs. Do not fetch or parse HTML pages from any of them.
One global rate-limiter per host, with jitter, exponential backoff, and honoring Retry-After. Limits below are hard ceilings; run at ~70% of them.
Cache everything on disk (SQLite) keyed by ID + fetched_at. Re-running the pipeline must not re-fetch unchanged records. Default cache TTL: labels 30 days, trials 7 days, PubMed 30 days, Jev answers forever (keyed by content hash + model version).
Identify yourself: User-Agent: OffLabelRadar/0.1 (contact: <email>) on every request; NCBI additionally requires tool= and email= query params.
No generative LLM in the core path. Jev is the only model; it returns probabilities, not prose. Any optional summarization step must be off by default and, if enabled, use a small/cheap model.
Pin the Jev version. Call GET https://api.typesafe.ai/v1/models once at startup, pick the newest jev-x.y.z (not the moving jev-latest alias), store it in the run record, and use it for the whole run.
This is a research/CI tool. Prominent disclaimer: not medical advice; approvals and indications must be verified against the current label. No patient data anywhere.
1. Data sources and exact access rules
Source	Endpoint	Auth	Ceiling	Notes
ClinicalTrials.gov API v2	https://clinicaltrials.gov/api/v2/studies	none	≤50 req/min (documented) → run at 30/min	query.intr=<drug>, filter.overallStatus=RECRUITING,NOT_YET_RECRUITING,ENROLLING_BY_INVITATION,ACTIVE_NOT_RECRUITING, pageSize=200 (max 1000), pageToken for paging, fields= to fetch only what you need (see §3). Single study: /api/v2/studies/{nctId}.
DailyMed web services v2	https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json?drug_name=<name>&pagesize=100 then .../spls/{setid}.xml	none	no published quota → ≤1 req/s, cache by setid	Prefer the most recent human-prescription label per ingredient. In the SPL XML, the Indications & Usage section is <section> with <code code="34067-9" codeSystem="2.16.840.1.113883.6.1">. Extract its text verbatim (this is the ground truth for "approved indications").
RxNorm (RxNav)	https://rxnav.nlm.nih.gov/REST/rxcui.json?name=<name> and /REST/rxcui/{rxcui}/allrelated.json	none	≤20 req/s → run at 5/s	Normalize drug names so Sirolimus, rapamycin, RAPAMUNE collapse to one ingredient. Use ingredient (IN) rxcui as the drug key.
PubMed E-utilities	https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi + efetch.fcgi	optional NCBI_API_KEY	3 req/s without key, 10 req/s with key; always send tool=OffLabelRadar&email=<email>	esearch: db=pubmed&term=("<drug>"[tiab] AND "<condition>"[tiab])&retmax=5&sort=relevance. Batch efetch with comma-joined PMIDs (retmode=xml), ≤200 IDs/request. Schedule bulk runs off-peak US Eastern (21:00–05:00) when doing >1k requests.
Jev (TypeSafe AI)	POST https://api.typesafe.ai/v1/systemone	TYPESAFE_API_KEY	~1,200 req/min, ~250k tok/s (dynamic); on 429 honor retry-after, on 529 back off	Billed on input tokens only (~$0.042 / Mtok). Keep state under ~2k tokens per call.
Inside a PromptQL VM, route Jev through the connected integration instead of a raw key: POST $PROMPTQL_PLATFORM_API_URL/v1/integration/jev/api.typesafe.ai/v1/systemone with Authorization: Bearer $PROMPTQL_USER_JWT (use __typesafe-api as the provider id if that is the one provisioned). Elsewhere, the official SDK is pip install typesafe-sdk.

2. Pipeline (stages are idempotent, resumable, and individually re-runnable)
seed drugs (config)
  → S1 normalize     RxNorm → ingredient rxcui + synonyms
  → S2 label         DailyMed → Indications & Usage text (+ setid, effective date)
  → S3 trials        CT.gov v2 → active trials mentioning the drug
  → S4 pair          one row per (drug, trial); cheap heuristic pre-tag
  → S5 jev gate      Jev noul: "tests drug outside approved indications?"
  → S6 evidence      PubMed top-5 abstracts for (drug, trial condition)   [only pairs passing S5]
  → S7 jev judge     Jev score (mechanistic plausibility) + choice (category)  [one call]
  → S8 rank          composite score → ranked_candidates view
  → UI
S4 heuristic pre-tag (no model, just to save tokens and give the UI a "why"): mark on_label_hint = true when any trial condition or its MeSH term appears (case-insensitive, stemmed) inside the label's Indications text. Still send to Jev — the hint is a UI badge and a sanity check on Jev, not a filter.

Gate threshold: proceed to S6/S7 only when P(off-label) ≥ 0.6. Store everything either way so the threshold can be tuned later without re-fetching.

3. Fields to keep
CT.gov (fields= param): protocolSection.identificationModule.nctId,briefTitle,officialTitle; statusModule.overallStatus,startDateStruct,lastUpdatePostDateStruct; designModule.phases,studyType,designInfo.allocationInfo; conditionsModule.conditions,keywords; armsInterventionsModule.interventions(name,type,description),armGroups(label,type,interventionNames); descriptionModule.briefSummary; sponsorCollaboratorsModule.leadSponsor(name,class); derivedSection.conditionBrowseModule.meshes,ancestors; derivedSection.interventionBrowseModule.meshes.

DailyMed: setid, title, effective_time, indications_text (verbatim), product_type, ingredient_names.

PubMed: pmid, title, abstract, pub_year, journal, pub_types.

4. Jev contract — copy these shapes exactly
S5 — gate (one question per (drug, trial) pair)
{
  "model": "<pinned jev version>",
  "state": {
    "drug": {"ingredient": "sirolimus", "synonyms": ["rapamycin", "Rapamune"]},
    "approved_indications_verbatim": "<Indications & Usage section text, trimmed to ~1200 tokens>",
    "label_effective_date": "2024-03-11",
    "trial": {
      "nct_id": "NCT0…",
      "title": "…",
      "conditions": ["…"],
      "condition_mesh": ["…"],
      "phases": ["PHASE2"],
      "interventions": [{"name": "…", "type": "DRUG", "description": "…"}],
      "arms": [{"label": "…", "type": "EXPERIMENTAL", "interventions": ["…"]}],
      "brief_summary": "<trimmed to ~400 tokens>",
      "sponsor": {"name": "…", "class": "INDUSTRY|OTHER|NIH|…"}
    },
    "heuristic_on_label_hint": false
  },
  "questions": {
    "off_label": {
      "type": "noul",
      "instructions": "Is this trial testing the drug for a condition or population that is NOT covered by its approved Indications & Usage text? Judge from the label text only; do not rely on outside knowledge of approvals. A different disease, a different stage/severity not mentioned, or a population the label excludes counts as outside. The same disease in a new formulation, dose, or comparator arm does NOT count.",
      "criteria": {
        "true": "The trial's target condition/population is not covered by the approved indications text.",
        "false": "The trial's target condition/population is within the approved indications text (including new dosing, formulation, or comparator studies of the same indication)."
      }
    },
    "is_active_arm": {
      "type": "noul",
      "instructions": "Is the drug an intervention under study in an experimental arm, rather than only a comparator, background/standard-of-care therapy, or rescue medication?",
      "criteria": {"true": "Drug is the (or a) studied intervention.", "false": "Drug is only comparator, background, or rescue."}
    }
  }
}
Read back answers.off_label.noul and answers.is_active_arm.noul (each a probability in [0,1]). Store both plus usage.input_tokens and model.

S7 — judge (one call, two questions; state = S5 state + PubMed evidence)
Add to state: "pubmed_evidence": [{"pmid": "…", "year": 2023, "title": "…", "abstract": "<trimmed to ~250 tokens>"}] (top 5).

"questions": {
  "plausibility": {
    "type": "score",
    "instructions": "Rate the mechanistic and clinical plausibility that this drug could work for the trial's condition, using ONLY the supplied abstracts and trial description as evidence. Higher levels require stronger, more direct evidence.",
    "criteria": [
      "No mechanistic rationale in the evidence; the pairing looks incidental or the drug is not the focus.",
      "Only an indirect or speculative rationale (shared pathway named but no data in this condition).",
      "Preclinical or observational evidence in this condition, or a clear shared mechanism with supporting data.",
      "Prior human data in this condition (case series, phase 1/2, retrospective cohorts) with a positive signal.",
      "Randomized or multiple independent clinical studies in this condition with consistent positive results."
    ]
  },
  "category": {
    "type": "choice",
    "instructions": "Classify how this trial relates to the drug's approved indications.",
    "criteria": {
      "new_indication": "A different disease or therapeutic area from anything in the approved indications.",
      "label_extension": "Same disease area as an approved indication, but a new population, stage, severity, line of therapy, or route/formulation not covered by the label.",
      "combination": "The drug is studied primarily as a combination partner and the novelty is the combination, not the indication.",
      "unclear": "Not enough information in the state to decide."
    }
  }
}
Read back answers.plausibility.score (float, 1–5 probability-weighted), .confidence, .probabilities; answers.category.choice, .probabilities, .confidence.

5. Ranking
rank_score = P(off_label) * P(is_active_arm) * (plausibility_score / 5) * phase_weight * recency_weight
phase_weight:   PHASE3=1.0, PHASE2=0.9, PHASE1/2=0.8, PHASE1=0.6, NA=0.5
recency_weight: 1.0 if last_update ≤ 12 mo, 0.85 if ≤ 24 mo, else 0.7
Default view: category ∈ {new_indication, label_extension}, P(off_label) ≥ 0.6, sorted by rank_score. Combinations and unclear are visible via filter, never deleted. Also surface a "disagreement" flag when heuristic_on_label_hint and P(off_label) conflict — those are the rows worth a human look.

6. Storage (SQLite, single file radar.db)
Tables: drugs(rxcui PK, name, synonyms_json), labels(setid PK, rxcui, title, effective_time, indications_text, fetched_at), trials(nct_id PK, json, fetched_at), pairs(id PK, rxcui, nct_id, on_label_hint, UNIQUE(rxcui,nct_id)), pubmed(pmid PK, json, fetched_at), pair_evidence(pair_id, pmid), jev_calls(id PK, pair_id, stage, model, state_hash, request_json, response_json, input_tokens, created_at), runs(id PK, started_at, finished_at, jev_model, seed_drugs_json, counts_json). Create a ranked_candidates view implementing §5.

7. UI (single page, one component library, works with zero context)
Header (two sentences): what this is, how to read it. Disclaimer link.
Ranked table: Drug · Trial (NCT link) · Condition · Category badge · P(off-label) · Plausibility (1–5 with confidence) · Phase · Sponsor class · Last updated · flags (disagreement, on-label hint).
Filters: drug, category, phase, sponsor class, min P(off-label), min plausibility, show combinations.
Row → evidence drawer: side-by-side label Indications text (with matched terms highlighted) vs trial conditions/summary; the 5 abstracts with PMID links; all three Jev answers with their probability distributions; the exact state JSON sent (collapsible).
Run panel: seed drug list (editable), "Refresh" button, progress by stage, request counts per host vs ceilings, Jev token spend for the run.
Ready endpoint GET /readyz → 200 once the DB is initialized.
8. Config / env
TYPESAFE_API_KEY=            # or route via PromptQL integration
NCBI_API_KEY=                # optional, raises PubMed limit to 10 rps
CONTACT_EMAIL=               # required; used in User-Agent and NCBI email=
SEED_DRUGS=metformin,sirolimus,semaglutide,tirzepatide,sildenafil,ketamine,colchicine,propranolol,naltrexone,minoxidil,thalidomide,dexamethasone,disulfiram,itraconazole,losartan,atorvastatin,doxycycline,valproate,aspirin,rapamycin
OFF_LABEL_THRESHOLD=0.6
MAX_TRIALS_PER_DRUG=60
DRY_RUN=false                # true → use recorded fixtures, no network
9. Definition of done (v1)
make run on a fresh machine builds the DB for the 20 seed drugs in under ~25 minutes at the ceilings above, with zero 429s from CT.gov/NCBI and all Jev 429/529s retried successfully.
Re-running immediately makes no CT.gov/DailyMed/PubMed requests (cache hit) and no Jev calls (answer cache hit).
DRY_RUN=true renders the full UI from fixtures with no network.
Tests: SPL Indications-section extraction on 3 recorded labels; rate-limiter respects ceilings under concurrency; ranking view matches §5 on a hand-built fixture; Jev request bodies validate against §4 shapes.
Every row in the UI can be traced to: label setid + effective date, NCT id + last update, PMIDs, Jev model version + request hash.
10. Known gaps to state in the UI, not hide
DailyMed covers US labels only (EMA differences not captured); CT.gov omits some non-US registries (WHO ICTRP is a v2 add); "outside approved indications" ≠ "unapproved use is unsafe"; Jev judges only what it is shown — label text truncation is a real error source, so log when truncation happened.

Playground

725.26 OLUs remaining

Invite people, get 500 OLUs

Create your own project

Mentions

All


bot


Browse more

Bots

Create a voting bot for the demos of...

Demo Jev Bot

·
Working
does Jev work with video?

Talk to this bot and start working on...




Jev Video Capability Inquiry

Today

krishna1256

12:48 PM

@bot
 instead of drug names that I submit as comma seperated values. can I submit my medical condition in simple text. and then it can give me a list of medicines that are applicable to this condition based on the studies? Can the app do that?

bot

12:48 PM

Claude Fable 5.1


Details

Yes — and it's a natural inversion of what's already there, not a rewrite. CT.gov v2 has query.cond= search, so the pipeline flips:

condition text → CT.gov trials for that condition → pull every DRUG intervention → RxNorm → DailyMed label → same Jev gate/judge (is this drug off-label for this condition? is it the active arm? evidence strength? category) → ranked list of medicines.

Two things change in the output, and they make it a better demo:

Two buckets, not one. Drugs whose label already covers the condition (approved standard of care) vs. drugs being tested off-label for it. The second bucket is the "existing drug, new use" list you want; the first is the sanity check judges can verify against what they know.
Fuzzy condition input is fine — CT.gov's condition search handles "sleep problems" vs "insomnia" reasonably, and I'd add a choice step where Jev maps the free text to the MeSH condition CT.gov actually returned, so the user sees "you asked about X, I searched Y".
Caveats to design in: a hot condition (e.g. "depression") has 5k+ trials — cap at the ~200 most recent so it's ~30 drugs, not 400. And the app must say clearly this is a research signal, not medical advice — the bucket labels handle most of that.

Build is small: one new pipeline entry point, one endpoint, a mode toggle in the run panel (drugs ↔ condition). Same DB, same Jev questions, same drawer. ~20 min. Want it?

krishna1256

12:59 PM

@bot
 Can you add this to the existing

bot

1:09 PM

Claude Fable 5.1


Details

On it — adding a "condition" mode alongside the drug list. Reading the current pipeline first so it reuses the same DB, gate, and drawer.

bot was stopped.

krishna1256

1:09 PM

@bot
 Can this be a create a visualization for the program flow / decision tree

bot

1:12 PM


Details

bot was stopped.

krishna1256

1:12 PM

@bot
 Can you push this source code to github for me to reuse this repo on my local build later. Add a runbook.md to use it locally. have a .env file with the spaces for keys to be loaded / added. check the code to https://github.com/anhsirksai/ as a new repo. or give me a package to dowload so that I can commit locally

bot

1:21 PM

Claude Fable 5.1


Details

Got all three: finishing the condition mode (was mid-test), then a flow/decision-tree diagram, then packaging to GitHub with a runbook + .env.example. Checking where the condition test landed.

Push failed: GitHub isn't connected on your account here, so git push (and repo creation) got a 502. Connect it and say "push" — the repo is committed and ready (main, 2 commits, 21 files, .env/radar.db/.venv git-ignored); I'll create anhsirksai/off-label-radar and push.

GitHub

Meanwhile, here's the package — git archive of HEAD, so unzip && git init && git add -A && git commit is all it needs:

off-label-radar.zip (source + git history)

What's in it:

RUNBOOK.md — local setup (uv sync, uv run --env-file .env uvicorn main:app), CLI usage, rate-limit table, config table, PromptQL VM deploy, troubleshooting.
.env.example — blanks for TYPESAFE_API_KEY (required locally), CONTACT_EMAIL, optional NCBI_API_KEY, pipeline knobs; PromptQL-only vars left blank. Code now falls back to a direct TypeSafe call with your key when it's not running inside PromptQL.
README.md — pitch, the Jev question table, mermaid flowchart, file layout.
docs/flow.svg + gen_flow.py — the program flow / decision tree, also embedded at the top of the app:
Off-Label Radar: program flow / decision tree

Also finished and committed: condition mode is live in the app (By condition tab). "alopecia areata" → 40 trials → 15 ingredients → 25 pairs: 19 off-label (cyclosporine pl 4.0, oral minoxidil 4.1, dupilumab, upadacitinib, tirzepatide, topical metformin/simvastatin…) vs 6 label-covers-it (ritlecitinib, deuruxolitinib, minoxidil). Fixed on the way: saline/vehicle "ingredients" and dose-strip regex (0.9% was eating "Sodium chloride").

krishna1256

1:23 PM

@bot
 ✅ Connected GitHub — please continue.

bot

1:23 PM

Claude Fable 5.1


Details

Creating anhsirksai/off-label-radar and pushing now.

GitHub is connected (I see you as anhsirksai), but the PromptQL GitHub App can't create repos — GitHub returned "Resource not accessible by integration". Push works once the repo exists.

Two quick steps on your side:

Create an empty repo: https://github.com/new → name off-label-radar, no README/.gitignore/license (I have those).
Make sure the PromptQL GitHub App can see it — for a public repo it already can; if you make it private: GitHub integration card → Settings → Manage installations → add off-label-radar.
Then say "push" and I'll push main (2 commits, 21 files) within seconds and verify the commits landed.

The zip from earlier is the same content if you'd rather git push from your laptop.

krishna1256

1:24 PM

@bot
 push

bot

1:25 PM

Claude Fable 5.1


Details

Pushing.

Not there yet — GitHub still returns 404 for anhsirksai/off-label-radar, and your newest repo is TrueAPIBuild (Aug 20). The repo needs to exist before I can push.

https://github.com/new → Repository name off-label-radar → leave README / .gitignore / license unchecked → Create. Public is simplest (no app install needed).

Then "push" again and it'll go through.

krishna1256

1:26 PM

@bot
 download the repo

bot

1:27 PM

Claude Fable 5.1


Details

Here's the repo as a zip — includes the .git history (2 commits on main), so no re-init needed:

off-label-radar.zip (source + git history)

unzip off-label-radar.zip && cd off-label-radar
git remote add origin https://github.com/anhsirksai/off-label-radar.git   # after creating the empty repo
git push -u origin main
cp .env.example .env   # add TYPESAFE_API_KEY, CONTACT_EMAIL; then see RUNBOOK.md
Excluded: .venv, radar.db, .env, logs.

krishna1256

1:33 PM

@bot
 make a linked post for me. showcaseing the Jev usage. showcasing where it helped. and a screenshot / video for this

bot

1:39 PM

Claude Fable 5.1


Details

On it — drafting the LinkedIn post and grabbing screenshots + a short screen recording of the app.

Post is drafted — LinkedIn-length (≈3,200 chars), leads with the problem, then the four Jev questions and where each one earned its keep (the comparator-arm noul killing 150 false pairs is the best "where it helped" line), then real numbers from the DB (620 pairs, 2,410 decisions ≈ 24¢, colchicine→CHD 0.98 / plausibility 4.9, semaglutide→T2DM 0.09 sanity check).

LinkedIn post: Off-Label Radar x Jev

Media — 26-second screen recording (overview → filter → colchicine evidence drawer with Jev answers → condition mode), plus stills:

Off-Label Radar: 26s demo (mp4)

Off-Label Radar: overview

Off-Label Radar: evidence drawer (label vs trial)

Off-Label Radar: Jev answers per pair

Off-Label Radar: condition mode

Two things to check before posting: the code link points at github.com/anhsirksai/off-label-radar (create the repo + push first, or drop the line), and swap "Sunday hackathon" for the actual event name if you want to tag it.

krishna1256

1:42 PM

@bot
 are you not able to checkin to github?

bot

1:43 PM

Claude Fable 5.1


Details

Half yes: I can push commits to any repo you own, but I can't create one — the PromptQL GitHub App's token has no repo-creation permission (GitHub returns 403 "Resource not accessible by integration"; just re-confirmed). And anhsirksai/off-label-radar still doesn't exist (404), so there's nothing to push into yet.

One 10-second step on your side: https://github.com/new → name off-label-radar → public → leave README/.gitignore/license unchecked → Create repository.

Say "push" right after and the 2 commits land in seconds — I'll verify them on GitHub and confirm.

(Or skip me entirely: unzip the earlier off-label-radar.zip, it already has the .git history — git remote add origin … && git push -u origin main.)

krishna1256

1:47 PM

@bot
 https://github.com/anhsirksai/off-label-radar push

bot

1:48 PM

Claude Fable 5.1


Details

Repo's there (public, empty) but the push got 403 — and the API confirms why: the PromptQL GitHub App has 0 installations on your account. Your OAuth login gives me read on public repos; write needs the app installed on the repo, public or not (I was wrong earlier that public alone was enough).

Fix: open the GitHub integration card here → Manage installations → install the PromptQL GitHub App on anhsirksai → select only off-label-radar (or all repos) → Save. Then say "push" and it'll land.

If that's a hassle, the zip route needs zero permissions from me:

unzip off-label-radar.zip && cd off-label-radar
git remote add origin https://github.com/anhsirksai/off-label-radar.git
git push -u origin main


Artifacts
(13)

/

Build prompt — Off-Label Radar (CT.gov × DailyMed × PubMed → Jev)

Sep 20, 2026, 12:05 PM

Build prompt — "Off-Label Radar": live drug-repurposing candidate scanner
You are building a small, self-contained web app that finds live clinical trials testing an approved drug outside its FDA-labeled indications, has Jev (TypeSafe AI) judge each candidate, and shows a ranked, evidence-backed table. The core signal — CT.gov trial conditions vs. DailyMed labeled indications — is not published anywhere today.

Optimize for: working end-to-end fast, polite/legal data access (no bans), cheap models, and every decision traceable to its evidence.

0. Non-negotiables
APIs only, never HTML scraping. CT.gov v2, DailyMed web services, NCBI E-utilities, RxNorm all have public JSON/XML APIs. Do not fetch or parse HTML pages from any of them.
One global rate-limiter per host, with jitter, exponential backoff, and honoring Retry-After. Limits below are hard ceilings; run at ~70% of them.
Cache everything on disk (SQLite) keyed by ID + fetched_at. Re-running the pipeline must not re-fetch unchanged records. Default cache TTL: labels 30 days, trials 7 days, PubMed 30 days, Jev answers forever (keyed by content hash + model version).
Identify yourself: User-Agent: OffLabelRadar/0.1 (contact: <email>) on every request; NCBI additionally requires tool= and email= query params.
No generative LLM in the core path. Jev is the only model; it returns probabilities, not prose. Any optional summarization step must be off by default and, if enabled, use a small/cheap model.
Pin the Jev version. Call GET https://api.typesafe.ai/v1/models once at startup, pick the newest jev-x.y.z (not the moving jev-latest alias), store it in the run record, and use it for the whole run.
This is a research/CI tool. Prominent disclaimer: not medical advice; approvals and indications must be verified against the current label. No patient data anywhere.
1. Data sources and exact access rules
Source	Endpoint	Auth	Ceiling	Notes
ClinicalTrials.gov API v2	https://clinicaltrials.gov/api/v2/studies	none	≤50 req/min (documented) → run at 30/min	query.intr=<drug>, filter.overallStatus=RECRUITING,NOT_YET_RECRUITING,ENROLLING_BY_INVITATION,ACTIVE_NOT_RECRUITING, pageSize=200 (max 1000), pageToken for paging, fields= to fetch only what you need (see §3). Single study: /api/v2/studies/{nctId}.
DailyMed web services v2	https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json?drug_name=<name>&pagesize=100 then .../spls/{setid}.xml	none	no published quota → ≤1 req/s, cache by setid	Prefer the most recent human-prescription label per ingredient. In the SPL XML, the Indications & Usage section is <section> with <code code="34067-9" codeSystem="2.16.840.1.113883.6.1">. Extract its text verbatim (this is the ground truth for "approved indications").
RxNorm (RxNav)	https://rxnav.nlm.nih.gov/REST/rxcui.json?name=<name> and /REST/rxcui/{rxcui}/allrelated.json	none	≤20 req/s → run at 5/s	Normalize drug names so Sirolimus, rapamycin, RAPAMUNE collapse to one ingredient. Use ingredient (IN) rxcui as the drug key.
PubMed E-utilities	https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi + efetch.fcgi	optional NCBI_API_KEY	3 req/s without key, 10 req/s with key; always send tool=OffLabelRadar&email=<email>	esearch: db=pubmed&term=("<drug>"[tiab] AND "<condition>"[tiab])&retmax=5&sort=relevance. Batch efetch with comma-joined PMIDs (retmode=xml), ≤200 IDs/request. Schedule bulk runs off-peak US Eastern (21:00–05:00) when doing >1k requests.
Jev (TypeSafe AI)	POST https://api.typesafe.ai/v1/systemone	TYPESAFE_API_KEY	~1,200 req/min, ~250k tok/s (dynamic); on 429 honor retry-after, on 529 back off	Billed on input tokens only (~$0.042 / Mtok). Keep state under ~2k tokens per call.
Inside a PromptQL VM, route Jev through the connected integration instead of a raw key: POST $PROMPTQL_PLATFORM_API_URL/v1/integration/jev/api.typesafe.ai/v1/systemone with Authorization: Bearer $PROMPTQL_USER_JWT (use __typesafe-api as the provider id if that is the one provisioned). Elsewhere, the official SDK is pip install typesafe-sdk.

2. Pipeline (stages are idempotent, resumable, and individually re-runnable)
seed drugs (config)
  → S1 normalize     RxNorm → ingredient rxcui + synonyms
  → S2 label         DailyMed → Indications & Usage text (+ setid, effective date)
  → S3 trials        CT.gov v2 → active trials mentioning the drug
  → S4 pair          one row per (drug, trial); cheap heuristic pre-tag
  → S5 jev gate      Jev noul: "tests drug outside approved indications?"
  → S6 evidence      PubMed top-5 abstracts for (drug, trial condition)   [only pairs passing S5]
  → S7 jev judge     Jev score (mechanistic plausibility) + choice (category)  [one call]
  → S8 rank          composite score → ranked_candidates view
  → UI
S4 heuristic pre-tag (no model, just to save tokens and give the UI a "why"): mark on_label_hint = true when any trial condition or its MeSH term appears (case-insensitive, stemmed) inside the label's Indications text. Still send to Jev — the hint is a UI badge and a sanity check on Jev, not a filter.

Gate threshold: proceed to S6/S7 only when P(off-label) ≥ 0.6. Store everything either way so the threshold can be tuned later without re-fetching.

3. Fields to keep
CT.gov (fields= param): protocolSection.identificationModule.nctId,briefTitle,officialTitle; statusModule.overallStatus,startDateStruct,lastUpdatePostDateStruct; designModule.phases,studyType,designInfo.allocationInfo; conditionsModule.conditions,keywords; armsInterventionsModule.interventions(name,type,description),armGroups(label,type,interventionNames); descriptionModule.briefSummary; sponsorCollaboratorsModule.leadSponsor(name,class); derivedSection.conditionBrowseModule.meshes,ancestors; derivedSection.interventionBrowseModule.meshes.

DailyMed: setid, title, effective_time, indications_text (verbatim), product_type, ingredient_names.

PubMed: pmid, title, abstract, pub_year, journal, pub_types.

4. Jev contract — copy these shapes exactly
S5 — gate (one question per (drug, trial) pair)
{
  "model": "<pinned jev version>",
  "state": {
    "drug": {"ingredient": "sirolimus", "synonyms": ["rapamycin", "Rapamune"]},
    "approved_indications_verbatim": "<Indications & Usage section text, trimmed to ~1200 tokens>",
    "label_effective_date": "2024-03-11",
    "trial": {
      "nct_id": "NCT0…",
      "title": "…",
      "conditions": ["…"],
      "condition_mesh": ["…"],
      "phases": ["PHASE2"],
      "interventions": [{"name": "…", "type": "DRUG", "description": "…"}],
      "arms": [{"label": "…", "type": "EXPERIMENTAL", "interventions": ["…"]}],
      "brief_summary": "<trimmed to ~400 tokens>",
      "sponsor": {"name": "…", "class": "INDUSTRY|OTHER|NIH|…"}
    },
    "heuristic_on_label_hint": false
  },
  "questions": {
    "off_label": {
      "type": "noul",
      "instructions": "Is this trial testing the drug for a condition or population that is NOT covered by its approved Indications & Usage text? Judge from the label text only; do not rely on outside knowledge of approvals. A different disease, a different stage/severity not mentioned, or a population the label excludes counts as outside. The same disease in a new formulation, dose, or comparator arm does NOT count.",
      "criteria": {
        "true": "The trial's target condition/population is not covered by the approved indications text.",
        "false": "The trial's target condition/population is within the approved indications text (including new dosing, formulation, or comparator studies of the same indication)."
      }
    },
    "is_active_arm": {
      "type": "noul",
      "instructions": "Is the drug an intervention under study in an experimental arm, rather than only a comparator, background/standard-of-care therapy, or rescue medication?",
      "criteria": {"true": "Drug is the (or a) studied intervention.", "false": "Drug is only comparator, background, or rescue."}
    }
  }
}
Read back answers.off_label.noul and answers.is_active_arm.noul (each a probability in [0,1]). Store both plus usage.input_tokens and model.

S7 — judge (one call, two questions; state = S5 state + PubMed evidence)
Add to state: "pubmed_evidence": [{"pmid": "…", "year": 2023, "title": "…", "abstract": "<trimmed to ~250 tokens>"}] (top 5).

"questions": {
  "plausibility": {
    "type": "score",
    "instructions": "Rate the mechanistic and clinical plausibility that this drug could work for the trial's condition, using ONLY the supplied abstracts and trial description as evidence. Higher levels require stronger, more direct evidence.",
    "criteria": [
      "No mechanistic rationale in the evidence; the pairing looks incidental or the drug is not the focus.",
      "Only an indirect or speculative rationale (shared pathway named but no data in this condition).",
      "Preclinical or observational evidence in this condition, or a clear shared mechanism with supporting data.",
      "Prior human data in this condition (case series, phase 1/2, retrospective cohorts) with a positive signal.",
      "Randomized or multiple independent clinical studies in this condition with consistent positive results."
    ]
  },
  "category": {
    "type": "choice",
    "instructions": "Classify how this trial relates to the drug's approved indications.",
    "criteria": {
      "new_indication": "A different disease or therapeutic area from anything in the approved indications.",
      "label_extension": "Same disease area as an approved indication, but a new population, stage, severity, line of therapy, or route/formulation not covered by the label.",
      "combination": "The drug is studied primarily as a combination partner and the novelty is the combination, not the indication.",
      "unclear": "Not enough information in the state to decide."
    }
  }
}
Read back answers.plausibility.score (float, 1–5 probability-weighted), .confidence, .probabilities; answers.category.choice, .probabilities, .confidence.

5. Ranking
rank_score = P(off_label) * P(is_active_arm) * (plausibility_score / 5) * phase_weight * recency_weight
phase_weight:   PHASE3=1.0, PHASE2=0.9, PHASE1/2=0.8, PHASE1=0.6, NA=0.5
recency_weight: 1.0 if last_update ≤ 12 mo, 0.85 if ≤ 24 mo, else 0.7
Default view: category ∈ {new_indication, label_extension}, P(off_label) ≥ 0.6, sorted by rank_score. Combinations and unclear are visible via filter, never deleted. Also surface a "disagreement" flag when heuristic_on_label_hint and P(off_label) conflict — those are the rows worth a human look.

6. Storage (SQLite, single file radar.db)
Tables: drugs(rxcui PK, name, synonyms_json), labels(setid PK, rxcui, title, effective_time, indications_text, fetched_at), trials(nct_id PK, json, fetched_at), pairs(id PK, rxcui, nct_id, on_label_hint, UNIQUE(rxcui,nct_id)), pubmed(pmid PK, json, fetched_at), pair_evidence(pair_id, pmid), jev_calls(id PK, pair_id, stage, model, state_hash, request_json, response_json, input_tokens, created_at), runs(id PK, started_at, finished_at, jev_model, seed_drugs_json, counts_json). Create a ranked_candidates view implementing §5.

7. UI (single page, one component library, works with zero context)
Header (two sentences): what this is, how to read it. Disclaimer link.
Ranked table: Drug · Trial (NCT link) · Condition · Category badge · P(off-label) · Plausibility (1–5 with confidence) · Phase · Sponsor class · Last updated · flags (disagreement, on-label hint).
Filters: drug, category, phase, sponsor class, min P(off-label), min plausibility, show combinations.
Row → evidence drawer: side-by-side label Indications text (with matched terms highlighted) vs trial conditions/summary; the 5 abstracts with PMID links; all three Jev answers with their probability distributions; the exact state JSON sent (collapsible).
Run panel: seed drug list (editable), "Refresh" button, progress by stage, request counts per host vs ceilings, Jev token spend for the run.
Ready endpoint GET /readyz → 200 once the DB is initialized.
8. Config / env
TYPESAFE_API_KEY=            # or route via PromptQL integration
NCBI_API_KEY=                # optional, raises PubMed limit to 10 rps
CONTACT_EMAIL=               # required; used in User-Agent and NCBI email=
SEED_DRUGS=metformin,sirolimus,semaglutide,tirzepatide,sildenafil,ketamine,colchicine,propranolol,naltrexone,minoxidil,thalidomide,dexamethasone,disulfiram,itraconazole,losartan,atorvastatin,doxycycline,valproate,aspirin,rapamycin
OFF_LABEL_THRESHOLD=0.6
MAX_TRIALS_PER_DRUG=60
DRY_RUN=false                # true → use recorded fixtures, no network
9. Definition of done (v1)
make run on a fresh machine builds the DB for the 20 seed drugs in under ~25 minutes at the ceilings above, with zero 429s from CT.gov/NCBI and all Jev 429/529s retried successfully.
Re-running immediately makes no CT.gov/DailyMed/PubMed requests (cache hit) and no Jev calls (answer cache hit).
DRY_RUN=true renders the full UI from fixtures with no network.
Tests: SPL Indications-section extraction on 3 recorded labels; rate-limiter respects ceilings under concurrency; ranking view matches §5 on a hand-built fixture; Jev request bodies validate against §4 shapes.
Every row in the UI can be traced to: label setid + effective date, NCT id + last update, PMIDs, Jev model version + request hash.
10. Known gaps to state in the UI, not hide
DailyMed covers US labels only (EMA differences not captured); CT.gov omits some non-US registries (WHO ICTRP is a v2 add); "outside approved indications" ≠ "unapproved use is unsafe"; Jev judges only what it is shown — label text truncation is a real error source, so log when truncation happened.
