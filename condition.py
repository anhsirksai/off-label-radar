"""Condition mode: plain-text condition → CT.gov trials → drug interventions → RxNorm ingredient → DailyMed label
→ same Jev gate/judge as drug mode → two buckets: label already covers it vs being tested off-label."""
import asyncio
import os
import re
import time
from collections import Counter, defaultdict

import httpx

import db
import jev
import net
import pipeline

MAX_DRUGS = int(os.environ.get("MAX_DRUGS_PER_CONDITION", "30"))
KEEP_TYPES = {"DRUG", "BIOLOGICAL"}
SKIP = {"placebo", "saline", "sham", "standard of care", "usual care", "vehicle", "no intervention", "control",
        "normal saline", "best supportive care", "matching placebo", "placebo tablet", "treatment as usual",
        "sodium chloride", "water for injection", "dextrose", "glucose"}


def clean_name(raw):
    s = re.sub(r"\([^)]*\)", " ", raw or "")
    s = re.split(r"[,;/]|\s\+\s", s, 1)[0]
    s = re.sub(r"\b\d+(\.\d+)?\s*(mg|mcg|µg|ug|g|ml|mL|%|iu|units?)(?!\w).*$", " ", s, flags=re.I)
    s = re.sub(r"\b(oral|tablets?|capsules?|injections?|infusions?|solution|iv|intravenous|subcutaneous|topical|cream|gel|patch|"
               r"extended release|er|xr|sr|low dose|high dose|dose|daily|once|twice)\b", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" -.:")


def slug(t):
    return re.sub(r"[^a-z0-9]+", "_", (t or "").lower()).strip("_")[:40] or "term"


async def resolve_ingredient(client, raw, st):
    """Intervention name as typed by a trial sponsor → single RxNorm ingredient (rxcui, name), or None."""
    name = clean_name(raw)
    low = name.lower()
    if len(name) < 3 or low in SKIP or "placebo" in low or low.startswith("sham"):
        return None
    cached = db.get_intervention_map(low)
    if cached is not None:
        return cached if cached.get("rxcui") else None
    result = None
    try:
        # 1) normalized exact-ish lookup, 2) approximate match (rank-1 candidate whose name actually overlaps the input)
        r = await net.fetch(client, "rxnorm", "GET", "https://rxnav.nlm.nih.gov/REST/rxcui.json", st,
                            params={"name": name, "search": "2"})
        hit = ((r.json().get("idGroup") or {}).get("rxnormId") or [None])[0]
        if not hit:
            r = await net.fetch(client, "rxnorm", "GET", "https://rxnav.nlm.nih.gov/REST/approximateTerm.json", st,
                                params={"term": name, "maxEntries": "5"})
            first = [w for w in re.findall(r"[a-z0-9]+", low) if len(w) > 3]
            for c in (r.json().get("approximateGroup") or {}).get("candidate") or []:
                cn = (c.get("name") or "").lower()
                if c.get("rank") == "1" and c.get("rxcui") and cn and (cn in low or low in cn or (first and first[0] in cn)):
                    hit = c["rxcui"]
                    break
        ins = []
        if hit:
            r1 = await net.fetch(client, "rxnorm", "GET", f"https://rxnav.nlm.nih.gov/REST/rxcui/{hit}/properties.json", st)
            props = (r1.json().get("properties") or {})
            if props.get("tty") in ("IN", "PIN"):
                ins = [(hit, props.get("name") or name)]
            else:
                r2 = await net.fetch(client, "rxnorm", "GET", f"https://rxnav.nlm.nih.gov/REST/rxcui/{hit}/related.json",
                                     st, params={"tty": "IN"})
                ins = [(c["rxcui"], c["name"]) for g in ((r2.json().get("relatedGroup") or {}).get("conceptGroup") or [])
                       for c in (g.get("conceptProperties") or [])]
        if hit:
            if len(ins) == 1:
                result = {"rxcui": ins[0][0], "ingredient": ins[0][1].lower(), "multi": 0}
            elif len(ins) > 1:  # fixed-dose combination product: skip, we judge single ingredients against single labels
                result = {"rxcui": None, "ingredient": " + ".join(n for _, n in ins), "multi": 1}
    except Exception as e:
        st.note(f"rxnorm resolve '{name[:40]}': {type(e).__name__}")
        return None  # transient: don't cache the miss
    if result and (result.get("ingredient") or "").lower() in SKIP:  # resolved to a vehicle/placebo ingredient
        result = {"rxcui": None, "ingredient": result["ingredient"], "multi": 0}
    db.save_intervention_map(low, raw, result)
    return result if result and result.get("rxcui") else None


async def _ctgov_page(client, st, params):
    for drop in (None, "filter.advanced", "sort"):
        if drop:
            params.pop(drop, None)
        r = await net.fetch(client, "ctgov", "GET", "https://clinicaltrials.gov/api/v2/studies", st, params=params)
        if r.status_code != 400:
            r.raise_for_status()
            return r.json()
    r.raise_for_status()


async def ctgov_by_condition(client, condition, st, max_trials):
    trials, token = [], None
    while len(trials) < max_trials:
        params = {"query.cond": condition, "filter.overallStatus": pipeline.ACTIVE,
                  "filter.advanced": "AREA[InterventionType](DRUG OR BIOLOGICAL)",
                  "pageSize": str(min(100, max_trials - len(trials))), "format": "json", "sort": "LastUpdatePostDate:desc"}
        if token:
            params["pageToken"] = token
        j = await _ctgov_page(client, st, params)
        for s in j.get("studies", []):
            t = pipeline.compact_study(s)
            if t["nct_id"] and any((iv.get("type") or "").upper() in KEEP_TYPES for iv in t.get("interventions") or []):
                db.save_trial(t)
                trials.append(t)
        token = j.get("nextPageToken")
        if not token or not j.get("studies"):
            break
    return trials[:max_trials]


def mesh_question(terms):
    crit = {slug(t): f"MeSH condition term '{t}'" for t in terms}
    crit["none_of_these"] = "None of the listed terms means the same thing as what the user typed."
    return {"condition_match": {
        "type": "choice",
        "instructions": ("Which MeSH condition term best matches the free-text medical condition the user typed? "
                         "Prefer the most specific term that means the same condition; a broader parent term is acceptable "
                         "only if nothing more specific matches. Pick none_of_these if the listed terms are about something else."),
        "criteria": crit,
    }}


async def decide(client, token, pair_ids, st, threshold):
    """S5–S8 shared with drug mode: Jev gate → PubMed evidence → Jev judge → rank."""
    st.stage, st.pairs_total, st.pairs_done = "S5 Jev gate", len(pair_ids), 0
    sem = asyncio.Semaphore(6)

    async def gate(pid):
        async with sem:
            try:
                await pipeline._gate(client, token, pid, st)
            except Exception as e:
                st.note(f"gate pair {pid}: {str(e)[:140]}")
            st.pairs_done += 1
    await asyncio.gather(*[gate(p) for p in pair_ids])

    passing = [p for p in pair_ids if (db.get_pair(p)["p_off_label"] or 0) >= threshold]
    st.note(f"gate: {len(passing)} off-label / {len(pair_ids) - len(passing)} label-covers-it (threshold {threshold})")

    st.stage, st.pairs_total, st.pairs_done = "S6 PubMed evidence", len(passing), 0
    all_pmids = []
    for pid in passing:
        p = db.get_pair(pid)
        if not db.has_evidence(pid):
            pmids = await pipeline.pubmed_search(client, p["drug_name"], p["condition"] or p["trial_title"], st)
            db.set_evidence(pid, pmids)
            all_pmids += pmids
        st.pairs_done += 1
    await pipeline.pubmed_fetch(client, sorted(set(all_pmids)), st)

    st.stage, st.pairs_total, st.pairs_done = "S7 Jev judge", len(passing), 0

    async def judge(pid):
        async with sem:
            try:
                await pipeline._judge(client, token, pid, st)
            except Exception as e:
                st.note(f"judge pair {pid}: {str(e)[:140]}")
            st.pairs_done += 1
    await asyncio.gather(*[judge(p) for p in passing])

    st.stage = "S8 rank"
    pipeline.rank_all()
    return passing


async def run_condition(query, token, st: net.RunState, threshold=pipeline.THRESHOLD, max_trials=pipeline.MAX_TRIALS, force=False):
    st.reset()
    st.running, st.started_at, st.force = True, time.time(), bool(force)
    st.mode, st.condition = "condition", query
    run_id = db.start_run([f"condition:{query}"])
    status, err = "done", None
    try:
        async with httpx.AsyncClient(timeout=90, headers={"User-Agent": net.UA}, follow_redirects=True) as client:
            st.stage = "C1 CT.gov trials for condition"
            st.note(f'"{query}": CT.gov active drug trials by condition (newest {max_trials})')
            trials = await ctgov_by_condition(client, query, st, max_trials)
            st.note(f"{len(trials)} active drug/biologic trials found")

            # C2 — Jev choice: which MeSH term did the user actually mean?
            mesh = Counter(m for t in trials for m in (t.get("condition_mesh") or []) if m)
            top = [m for m, _ in mesh.most_common(8)]
            mesh_match, mesh_probs = None, {}
            if top:
                st.stage = "C2 Jev: map condition text to MeSH"
                try:
                    slugs = {slug(t): t for t in top}
                    state = {"user_condition_text": query,
                             "candidate_mesh_terms": [{"term": t, "n_trials_indexed": mesh[t]} for t in top]}
                    ans = await jev.ask(client, token, None, "condition_match", state, mesh_question(top), st,
                                        f"Off-Label Radar: map condition text to MeSH term: {query}")
                    ch = ans["condition_match"]
                    mesh_match = slugs.get(ch["choice"])
                    mesh_probs = {slugs.get(k, k): v for k, v in (ch.get("probabilities") or {}).items()}
                    st.mesh_match = mesh_match
                    st.note(f'Jev: "{query}" -> {mesh_match or "none of the top MeSH terms"} (conf {float(ch.get("confidence") or 0):.2f})')
                except Exception as e:
                    st.note(f"condition mapping skipped: {str(e)[:120]}")

            # C3 — every drug intervention → RxNorm ingredient
            st.stage = "C3 resolve drug interventions (RxNorm)"
            by_drug, names, cache = defaultdict(set), {}, {}
            for t in trials:
                for iv in t.get("interventions") or []:
                    raw = iv.get("name")
                    if not raw or (iv.get("type") or "").upper() not in KEEP_TYPES:
                        continue
                    if raw not in cache:
                        cache[raw] = await resolve_ingredient(client, raw, st)
                    res = cache[raw]
                    if res:
                        by_drug[res["rxcui"]].add(t["nct_id"])
                        names[res["rxcui"]] = res["ingredient"]
            ranked = sorted(by_drug, key=lambda k: -len(by_drug[k]))[:MAX_DRUGS]
            skipped = sum(1 for v in cache.values() if v is None)
            st.note(f"{len(cache)} intervention names -> {len(by_drug)} ingredients ({len(ranked)} kept, {skipped} placebo/unresolved/combos)")
            st.drugs = [names[k] for k in ranked]
            cond_id = db.upsert_condition(query, mesh_match, mesh_probs, len(trials), st.drugs)

            # C4 — labels + pairs
            st.stage = "C4 DailyMed labels"
            pair_ids = []
            for rx in ranked:
                name = names[rx]
                try:
                    st.note(f"{name}: DailyMed label")
                    _, syn = await pipeline.rxnorm(client, name, st)
                    db.upsert_drug(rx, name, syn)
                    label = await pipeline.dailymed(client, name, rx, st)
                    for nct in sorted(by_drug[rx]):
                        t = db.get_trial(nct)["json"]
                        pair_ids.append(db.upsert_pair(rx, name, label["setid"], t, pipeline.on_label_hint(label["indications_text"], t)))
                except Exception as e:
                    st.note(f"{name}: skipped ({type(e).__name__}: {str(e)[:100]})")
            db.set_condition_pairs(cond_id, pair_ids)
            st.note(f"{len(pair_ids)} (drug, trial) pairs for \"{query}\"")

            passing = await decide(client, token, pair_ids, st, threshold)
            off_drugs = sorted({db.get_pair(p)["drug_name"] for p in passing})
            st.note(f"done: {len(off_drugs)} drugs being tested off-label for \"{query}\": {', '.join(off_drugs)[:300]} | "
                    f"{st.jev_calls} Jev calls ({st.jev_cached} cached, {st.jev_reused} reused{' — FORCE' if st.force else ''})")
    except Exception as e:
        status, err = "error", f"{type(e).__name__}: {e}"
        st.error = err
        st.note(f"ERROR {err}")
    finally:
        st.running, st.finished_at, st.stage = False, time.time(), ("error" if err else "done")
        db.finish_run(run_id, st.jev_model, st.snapshot()["counts"], status, err)