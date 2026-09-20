"""Jev (TypeSafe AI System One) via the PromptQL integration proxy. Typed decisions only, no prose."""
import hashlib
import json
import os

import db
import net

PLATFORM = os.environ.get("PROMPTQL_PLATFORM_API_URL", "").rstrip("/")
PROVIDER = os.environ.get("JEV_PROVIDER", "__typesafe-api")
MODEL = os.environ.get("JEV_MODEL", "jev-latest")
PRICE_PER_MTOK_CENTS = 4.2


DIRECT_KEY = os.environ.get("TYPESAFE_API_KEY") or None  # local builds: call TypeSafe directly with your own key


def url():
    if DIRECT_KEY or not PLATFORM:
        return "https://api.typesafe.ai/v1/systemone"
    return f"{PLATFORM}/v1/integration/{PROVIDER}/api.typesafe.ai/v1/systemone"


GATE_QUESTIONS = {
    "off_label": {
        "type": "noul",
        "instructions": (
            "Is this trial testing the drug for a condition or population that is NOT covered by its approved "
            "Indications & Usage text? Judge from the label text only; do not rely on outside knowledge of approvals. "
            "A different disease, a different stage/severity not mentioned, or a population the label excludes counts as outside. "
            "The same disease in a new formulation, dose, or comparator arm does NOT count."
        ),
        "criteria": {
            "true": "The trial's target condition/population is not covered by the approved indications text.",
            "false": "The trial's target condition/population is within the approved indications text (including new dosing, formulation, or comparator studies of the same indication).",
        },
    },
    "is_active_arm": {
        "type": "noul",
        "instructions": (
            "Is the drug an intervention under study in an experimental arm, rather than only a comparator, "
            "background/standard-of-care therapy, or rescue medication?"
        ),
        "criteria": {
            "true": "Drug is the (or a) studied intervention.",
            "false": "Drug is only comparator, background, or rescue.",
        },
    },
}

JUDGE_QUESTIONS = {
    "plausibility": {
        "type": "score",
        "instructions": (
            "Rate the mechanistic and clinical plausibility that this drug could work for the trial's condition, "
            "using ONLY the supplied abstracts and trial description as evidence. Higher levels require stronger, more direct evidence."
        ),
        "criteria": [
            "No mechanistic rationale in the evidence; the pairing looks incidental or the drug is not the focus.",
            "Only an indirect or speculative rationale (shared pathway named but no data in this condition).",
            "Preclinical or observational evidence in this condition, or a clear shared mechanism with supporting data.",
            "Prior human data in this condition (case series, phase 1/2, retrospective cohorts) with a positive signal.",
            "Randomized or multiple independent clinical studies in this condition with consistent positive results.",
        ],
    },
    "category": {
        "type": "choice",
        "instructions": "Classify how this trial relates to the drug's approved indications.",
        "criteria": {
            "new_indication": "A different disease or therapeutic area from anything in the approved indications.",
            "label_extension": "Same disease area as an approved indication, but a new population, stage, severity, line of therapy, or route/formulation not covered by the label.",
            "combination": "The drug is studied primarily as a combination partner and the novelty is the combination, not the indication.",
            "unclear": "Not enough information in the state to decide.",
        },
    },
}


def _trim(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n] + " …[truncated]"


def gate_state(drug, synonyms, label, trial, hint):
    return {
        "drug": {"ingredient": drug, "synonyms": synonyms[:12]},
        "approved_indications_verbatim": _trim(label.get("indications_text"), 4800),
        "label_effective_date": label.get("effective_time"),
        "trial": {
            "nct_id": trial["nct_id"], "title": trial.get("title"),
            "conditions": trial.get("conditions"), "condition_mesh": trial.get("condition_mesh"),
            "phases": trial.get("phases"), "interventions": trial.get("interventions"), "arms": trial.get("arms"),
            "brief_summary": _trim(trial.get("brief_summary"), 1600), "sponsor": trial.get("sponsor"),
        },
        "heuristic_on_label_hint": hint,
    }


def judge_state(gstate, evidence):
    st = dict(gstate)
    st["pubmed_evidence"] = [
        {"pmid": e["pmid"], "year": e.get("year"), "title": e.get("title"), "abstract": _trim(e.get("abstract"), 1000)}
        for e in evidence[:5]
    ]
    return st


def state_hash(state, questions, model):
    raw = json.dumps({"s": state, "q": questions, "m": model}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


async def ask(client, token, pair_id, stage, state, questions, st: net.RunState, description):
    """Cached, rate-limited Jev call. Returns the parsed answers dict."""
    h = state_hash(state, questions, MODEL)
    cached = None if getattr(st, "force", False) else db.get_jev_cached(h)
    if cached:
        st.jev_cached += 1
        st.jev_model = st.jev_model or cached.get("model")
        return cached["answers"]
    body = {"model": MODEL, "state": state, "questions": questions}
    headers = {"Authorization": f"Bearer {DIRECT_KEY or token}", "Content-Type": "application/json"}
    if not DIRECT_KEY and PLATFORM:  # inside PromptQL: the proxy injects the TypeSafe key, description shows in the approval modal
        headers["X-PromptQL-Description"] = description.encode("ascii", "replace").decode().replace("\n", " ")
    r = await net.fetch(client, "jev", "POST", url(), st, json=body, headers=headers)
    if r.status_code >= 400:
        raise RuntimeError(f"Jev HTTP {r.status_code}: {r.text[:300]}")
    j = r.json()
    tokens = (j.get("usage") or {}).get("input_tokens", 0) or 0
    st.jev_calls += 1
    st.jev_tokens += tokens
    st.jev_model = j.get("model") or MODEL
    db.save_jev_call(pair_id, stage, j.get("model") or MODEL, h, body, j, tokens)
    return j["answers"]