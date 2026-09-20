"""Off-Label Radar pipeline: RxNorm → DailyMed → CT.gov → pair → Jev gate → PubMed → Jev judge → rank."""
import asyncio
import json
import os
import re
import time
import xml.etree.ElementTree as ET

import httpx

import db
import jev
import net

THRESHOLD = float(os.environ.get("OFF_LABEL_THRESHOLD", "0.6"))
MAX_TRIALS = int(os.environ.get("MAX_TRIALS_PER_DRUG", "40"))
TTL = {"labels": 30 * 86400, "trials": 7 * 86400, "pubmed": 30 * 86400}
ACTIVE = "RECRUITING,NOT_YET_RECRUITING,ACTIVE_NOT_RECRUITING,ENROLLING_BY_INVITATION"
NS = {"v": "urn:hl7-org:v3"}


# ---- S1 RxNorm --------------------------------------------------------------

async def rxnorm(client, name, st):
    r = await net.fetch(client, "rxnorm", "GET", "https://rxnav.nlm.nih.gov/REST/rxcui.json", st,
                        params={"name": name, "search": "1"})
    ids = (r.json().get("idGroup") or {}).get("rxnormId") or []
    if not ids:
        return f"name:{name.lower()}", [name]
    rxcui = ids[0]
    syn = {name}
    try:
        r2 = await net.fetch(client, "rxnorm", "GET", f"https://rxnav.nlm.nih.gov/REST/rxcui/{rxcui}/allrelated.json", st)
        for g in (r2.json().get("allRelatedGroup") or {}).get("conceptGroup", []) or []:
            if g.get("tty") in ("IN", "BN", "PIN"):
                for c in g.get("conceptProperties") or []:
                    syn.add(c["name"])
    except Exception:
        pass
    return rxcui, sorted(syn, key=str.lower)[:12]


# ---- S2 DailyMed ------------------------------------------------------------

def extract_indications(xml_text):
    root = ET.fromstring(xml_text)
    for sec in root.iter("{urn:hl7-org:v3}section"):
        code = sec.find("v:code", NS)
        if code is not None and code.get("code") == "34067-9":
            txt = " ".join(t.strip() for t in sec.itertext() if t.strip())
            return re.sub(r"\s+", " ", txt)
    return ""


def _effective_time(xml_text):
    m = re.search(r'<effectiveTime value="(\d{8})', xml_text)
    if m:
        v = m.group(1)
        return f"{v[:4]}-{v[4:6]}-{v[6:]}"
    return None


async def dailymed(client, name, rxcui, st):
    cached = db.get_label(rxcui)
    if cached and cached["fetched_at"] > time.time() - TTL["labels"] and cached["indications_text"]:
        return cached
    r = await net.fetch(client, "dailymed", "GET", "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json", st,
                        params={"drug_name": name, "pagesize": "25"})
    data = r.json().get("data", []) or []
    up = name.upper()

    def key(d):
        t = (d.get("title") or "").upper()
        return (0 if t.startswith(up) else 1, 1 if (" AND " in t or "KIT" in t) else 0)

    data.sort(key=key)
    for d in data[:4]:
        setid = d.get("setid")
        rx = await net.fetch(client, "dailymed", "GET", f"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/{setid}.xml", st)
        if rx.status_code != 200:
            continue
        ind = extract_indications(rx.text)
        if len(ind) > 40:
            db.save_label(setid, rxcui, d.get("title"), _effective_time(rx.text), ind, len(ind) > 4800)
            return db.get_label(rxcui)
    st.note(f"{name}: no DailyMed Indications section found")
    db.save_label(f"none:{rxcui}", rxcui, f"{name} (no label found)", None, "", False)
    return db.get_label(rxcui)


# ---- S3 ClinicalTrials.gov --------------------------------------------------

def compact_study(study):
    p = study.get("protocolSection", {}) or {}
    d = study.get("derivedSection", {}) or {}
    idm, sm, dm = p.get("identificationModule", {}), p.get("statusModule", {}), p.get("designModule", {})
    cm, ai, ds = p.get("conditionsModule", {}), p.get("armsInterventionsModule", {}), p.get("descriptionModule", {})
    sp = (p.get("sponsorCollaboratorsModule", {}) or {}).get("leadSponsor", {}) or {}
    return {
        "nct_id": idm.get("nctId"), "title": idm.get("briefTitle"), "official_title": idm.get("officialTitle"),
        "status": sm.get("overallStatus"), "start_date": (sm.get("startDateStruct") or {}).get("date"),
        "last_update": (sm.get("lastUpdatePostDateStruct") or {}).get("date"),
        "phases": dm.get("phases") or [], "study_type": dm.get("studyType"),
        "conditions": cm.get("conditions") or [], "keywords": (cm.get("keywords") or [])[:15],
        "condition_mesh": [m.get("term") for m in (d.get("conditionBrowseModule") or {}).get("meshes", []) or []],
        "intervention_mesh": [m.get("term") for m in (d.get("interventionBrowseModule") or {}).get("meshes", []) or []],
        "interventions": [{"name": i.get("name"), "type": i.get("type"), "description": (i.get("description") or "")[:500],
                           "other_names": (i.get("otherNames") or [])[:6]} for i in ai.get("interventions") or []],
        "arms": [{"label": a.get("label"), "type": a.get("type"), "interventions": a.get("interventionNames") or []}
                 for a in ai.get("armGroups") or []],
        "brief_summary": (ds.get("briefSummary") or "")[:1600],
        "sponsor": {"name": sp.get("name"), "class": sp.get("class")},
    }


async def ctgov(client, name, rxcui, st, max_trials):
    fresh = db.drug_trials_fresh(rxcui, TTL["trials"])
    if fresh is not None:
        trials = [db.get_trial(n)["json"] for n in fresh]
        return [t for t in trials if t]
    params = {"query.intr": name, "filter.overallStatus": ACTIVE, "pageSize": str(min(max_trials, 100)),
              "format": "json", "sort": "LastUpdatePostDate:desc"}
    r = await net.fetch(client, "ctgov", "GET", "https://clinicaltrials.gov/api/v2/studies", st, params=params)
    if r.status_code == 400:  # sort not accepted → retry without it
        params.pop("sort")
        r = await net.fetch(client, "ctgov", "GET", "https://clinicaltrials.gov/api/v2/studies", st, params=params)
    r.raise_for_status()
    studies = r.json().get("studies", [])[:max_trials]
    trials = []
    for s in studies:
        t = compact_study(s)
        if t["nct_id"]:
            db.save_trial(t)
            trials.append(t)
    db.save_drug_trials(rxcui, [t["nct_id"] for t in trials])
    return trials


# ---- S4 heuristic ----------------------------------------------------------

STOP = {"disease", "diseases", "disorder", "disorders", "syndrome", "chronic", "acute", "type", "with", "patients",
        "adult", "adults", "other", "unspecified", "severe", "moderate", "mild", "advanced", "stage", "cancer",
        "carcinoma", "infection", "injury", "treatment", "therapy"}


def on_label_hint(indications, trial):
    text = (indications or "").lower()
    if not text:
        return None
    for t in (trial.get("conditions") or []) + (trial.get("condition_mesh") or []):
        tl = (t or "").lower().strip()
        if not tl:
            continue
        if tl in text or tl.rstrip("s") in text:
            return True
        words = [w for w in re.findall(r"[a-z0-9]+", tl) if len(w) > 4 and w not in STOP]
        if words and all(w.rstrip("s") in text for w in words):
            return True
    return False


# ---- S6 PubMed --------------------------------------------------------------

def _ncbi_params(**kw):
    p = {"tool": "OffLabelRadar", "email": net.CONTACT}
    if net.NCBI_KEY:
        p["api_key"] = net.NCBI_KEY
    p.update(kw)
    return p


async def pubmed_search(client, drug, condition, st):
    cond = re.sub(r"[^\w\s\-]", " ", condition or "").strip()
    words = [w for w in cond.split() if w.lower() not in STOP or len(cond.split()) == 1]
    # strict phrase first, then loose word-AND (handles "Parkinson Disease" vs "Parkinson's disease")
    terms = [f'{drug}[tiab] AND "{cond}"[tiab]',
             f'{drug}[tiab] AND ({" AND ".join(w + "[tiab]" for w in words) or cond + "[tiab]"})',
             f'{drug}[tiab] AND {cond}[MeSH Terms]']
    for term in terms:
        r = await net.fetch(client, "pubmed", "GET", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", st,
                            params=_ncbi_params(db="pubmed", term=term, retmax="5", sort="relevance", retmode="json"))
        try:
            ids = r.json().get("esearchresult", {}).get("idlist", []) or []
        except Exception:
            ids = []
        if ids:
            return ids
    return []


def parse_pubmed(xml_text):
    out = []
    root = ET.fromstring(xml_text)
    for art in root.iter("PubmedArticle"):
        mc = art.find("MedlineCitation")
        a = mc.find("Article") if mc is not None else None
        if a is None:
            continue
        title_el = a.find("ArticleTitle")
        abst = " ".join("".join(x.itertext()) for x in a.findall("Abstract/AbstractText"))
        year = a.findtext("Journal/JournalIssue/PubDate/Year") or (a.findtext("Journal/JournalIssue/PubDate/MedlineDate") or "")[:4]
        out.append({
            "pmid": mc.findtext("PMID"),
            "title": "".join(title_el.itertext()) if title_el is not None else "",
            "abstract": abst, "year": year, "journal": a.findtext("Journal/Title"),
            "pub_types": [t.text for t in a.findall("PublicationTypeList/PublicationType")],
        })
    return out


async def pubmed_fetch(client, pmids, st):
    need = [p for p in pmids if not db.get_pubmed(p)]
    for i in range(0, len(need), 150):
        chunk = need[i:i + 150]
        r = await net.fetch(client, "pubmed", "POST", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi", st,
                            data=_ncbi_params(db="pubmed", id=",".join(chunk), retmode="xml"))
        if r.status_code != 200:
            st.note(f"pubmed efetch HTTP {r.status_code}")
            continue
        try:
            for art in parse_pubmed(r.text):
                if art["pmid"]:
                    db.save_pubmed(art)
        except ET.ParseError:
            st.note("pubmed efetch: unparseable XML")


# ---- S8 rank -----------------------------------------------------------------

def phase_weight(phases):
    p = set(phases or [])
    if "PHASE3" in p or "PHASE4" in p:
        return 1.0
    if "PHASE2" in p and "PHASE1" in p:
        return 0.8
    if "PHASE2" in p:
        return 0.9
    if "PHASE1" in p:
        return 0.6
    return 0.5


def recency_weight(last_update):
    if not last_update:
        return 0.7
    try:
        parts = last_update.split("-")
        y, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 6
        months = (time.gmtime().tm_year - y) * 12 + (time.gmtime().tm_mon - m)
    except Exception:
        return 0.7
    return 1.0 if months <= 12 else 0.85 if months <= 24 else 0.7


def rank_all():
    for p in db.all_pairs():
        if p["p_off_label"] is None or p["plausibility"] is None:
            db.set_rank(p["id"], None)
            continue
        score = (p["p_off_label"] * (p["p_active"] or 1.0) * (p["plausibility"] / 5.0)
                 * phase_weight((p["phases"] or "").split(",")) * recency_weight(p["last_update"]))
        db.set_rank(p["id"], round(score, 4))


# ---- orchestration ----------------------------------------------------------

def _drug_row(p):
    return db.get_drug(p["rxcui"]) or {"name": p["drug_name"], "synonyms_json": json.dumps([p["drug_name"]])}


async def _gate(client, token, pair_id, st):
    p = db.get_pair(pair_id)
    if p["p_off_label"] is not None and not st.force:
        st.jev_reused += 1
        return
    drug = _drug_row(p)
    label = db.get_label(p["rxcui"]) or {}
    trial = db.get_trial(p["nct_id"])["json"]
    state = jev.gate_state(drug["name"], json.loads(drug["synonyms_json"] or "[]"), label, trial,
                           None if p["on_label_hint"] is None else bool(p["on_label_hint"]))
    ans = await jev.ask(client, token, pair_id, "gate", state, jev.GATE_QUESTIONS, st,
                        f"Off-Label Radar gate: {drug['name']} x {p['nct_id']}")
    db.update_pair_gate(pair_id, float(ans["off_label"]["noul"]), float(ans["is_active_arm"]["noul"]))


async def _judge(client, token, pair_id, st):
    p = db.get_pair(pair_id)
    if p["plausibility"] is not None and not st.force:
        st.jev_reused += 1
        return
    drug = _drug_row(p)
    label = db.get_label(p["rxcui"]) or {}
    trial = db.get_trial(p["nct_id"])["json"]
    gstate = jev.gate_state(drug["name"], json.loads(drug["synonyms_json"] or "[]"), label, trial,
                            None if p["on_label_hint"] is None else bool(p["on_label_hint"]))
    state = jev.judge_state(gstate, db.get_evidence(pair_id))
    ans = await jev.ask(client, token, pair_id, "judge", state, jev.JUDGE_QUESTIONS, st,
                        f"Off-Label Radar judge: {drug['name']} x {p['nct_id']}")
    pl, cat = ans["plausibility"], ans["category"]
    # Jev score levels are 0-indexed (legend keys 0..4); present as 1..5
    levels = [int(k) for k in (pl.get("legend") or {}).keys()] or [0]
    plaus = float(pl["score"]) - min(levels) + 1
    db.update_pair_judge(pair_id, round(plaus, 2), float(pl.get("confidence") or 0), cat["choice"],
                         float(cat.get("confidence") or 0), cat.get("probabilities") or {})


async def run(drugs, token, st: net.RunState, threshold=THRESHOLD, max_trials=MAX_TRIALS, force=False):
    st.reset()
    st.running, st.started_at, st.drugs, st.force = True, time.time(), drugs, bool(force)
    run_id = db.start_run(drugs)
    status, err = "done", None
    try:
        async with httpx.AsyncClient(timeout=90, headers={"User-Agent": net.UA}, follow_redirects=True) as client:
            pair_ids = []
            st.stage = "S1–S3 fetch"
            for name in drugs:
                try:
                    st.note(f"{name}: RxNorm normalize")
                    rxcui, syn = await rxnorm(client, name, st)
                    db.upsert_drug(rxcui, name, syn)
                    st.note(f"{name}: DailyMed label")
                    label = await dailymed(client, name, rxcui, st)
                    st.note(f"{name}: CT.gov active trials")
                    trials = await ctgov(client, name, rxcui, st, max_trials)
                    for t in trials:
                        hint = on_label_hint(label["indications_text"], t)
                        pair_ids.append(db.upsert_pair(rxcui, name, label["setid"], t, hint))
                    st.note(f"{name}: {len(trials)} active trials paired")
                except Exception as e:  # one bad drug must not kill the run
                    st.note(f"{name}: skipped ({type(e).__name__}: {str(e)[:120]})")

            st.stage, st.pairs_total, st.pairs_done = "S5 Jev gate", len(pair_ids), 0
            sem = asyncio.Semaphore(6)

            async def gate(pid):
                async with sem:
                    try:
                        await _gate(client, token, pid, st)
                    except Exception as e:
                        st.note(f"gate pair {pid}: {str(e)[:140]}")
                    st.pairs_done += 1
            await asyncio.gather(*[gate(p) for p in pair_ids])

            passing = [p for p in pair_ids if (db.get_pair(p)["p_off_label"] or 0) >= threshold]
            st.note(f"gate: {len(passing)}/{len(pair_ids)} pairs ≥ {threshold}")

            st.stage, st.pairs_total, st.pairs_done = "S6 PubMed evidence", len(passing), 0
            all_pmids = []
            for pid in passing:
                p = db.get_pair(pid)
                if not db.has_evidence(pid):
                    pmids = await pubmed_search(client, p["drug_name"], p["condition"] or p["trial_title"], st)
                    db.set_evidence(pid, pmids)
                    all_pmids += pmids
                st.pairs_done += 1
            await pubmed_fetch(client, sorted(set(all_pmids)), st)

            st.stage, st.pairs_total, st.pairs_done = "S7 Jev judge", len(passing), 0

            async def judge(pid):
                async with sem:
                    try:
                        await _judge(client, token, pid, st)
                    except Exception as e:
                        st.note(f"judge pair {pid}: {str(e)[:140]}")
                    st.pairs_done += 1
            await asyncio.gather(*[judge(p) for p in passing])

            st.stage = "S8 rank"
            rank_all()
            st.note(f"done: {len(pair_ids)} pairs, {len(passing)} candidates, {st.jev_calls} Jev calls "
                    f"({st.jev_cached} cached, {st.jev_reused} reused from DB{' — FORCE' if st.force else ''})")
    except Exception as e:
        status, err = "error", f"{type(e).__name__}: {e}"
        st.error = err
        st.note(f"ERROR {err}")
    finally:
        st.running, st.finished_at, st.stage = False, time.time(), ("error" if err else "done")
        db.finish_run(run_id, st.jev_model, st.snapshot()["counts"], status, err)