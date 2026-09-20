import json
import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("RADAR_DB", os.path.join(HERE, "radar.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS drugs (
  rxcui TEXT PRIMARY KEY, name TEXT, synonyms_json TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS labels (
  setid TEXT PRIMARY KEY, rxcui TEXT, title TEXT, effective_time TEXT,
  indications_text TEXT, truncated INTEGER, fetched_at REAL);
CREATE TABLE IF NOT EXISTS trials (
  nct_id TEXT PRIMARY KEY, json TEXT, fetched_at REAL);
CREATE TABLE IF NOT EXISTS drug_trials (
  rxcui TEXT, nct_id TEXT, fetched_at REAL, PRIMARY KEY (rxcui, nct_id));
CREATE TABLE IF NOT EXISTS pairs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rxcui TEXT, nct_id TEXT, drug_name TEXT, label_setid TEXT,
  trial_title TEXT, condition TEXT, phases TEXT, sponsor_class TEXT, sponsor_name TEXT, last_update TEXT,
  on_label_hint INTEGER,
  p_off_label REAL, p_active REAL,
  plausibility REAL, plaus_conf REAL, category TEXT, cat_conf REAL, cat_probs TEXT,
  rank_score REAL, stage TEXT, updated_at REAL,
  UNIQUE (rxcui, nct_id));
CREATE TABLE IF NOT EXISTS pubmed (
  pmid TEXT PRIMARY KEY, json TEXT, fetched_at REAL);
CREATE TABLE IF NOT EXISTS pair_evidence (
  pair_id INTEGER, pmid TEXT, rank INTEGER, PRIMARY KEY (pair_id, pmid));
CREATE TABLE IF NOT EXISTS jev_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, pair_id INTEGER, stage TEXT, model TEXT,
  state_hash TEXT, request_json TEXT, response_json TEXT, input_tokens INTEGER, created_at REAL);
CREATE INDEX IF NOT EXISTS jev_hash ON jev_calls(state_hash);
CREATE TABLE IF NOT EXISTS intervention_map (
  key TEXT PRIMARY KEY, raw TEXT, rxcui TEXT, ingredient TEXT, multi INTEGER, resolved_at REAL);
CREATE TABLE IF NOT EXISTS conditions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT UNIQUE, mesh_match TEXT, mesh_probs TEXT,
  n_trials INTEGER, drugs_json TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS condition_pairs (
  condition_id INTEGER, pair_id INTEGER, PRIMARY KEY (condition_id, pair_id));
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL, finished_at REAL, jev_model TEXT,
  seed_drugs_json TEXT, counts_json TEXT, status TEXT, error TEXT);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init():
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def _exec(sql, params=(), fetch=None):
    conn = connect()
    try:
        cur = conn.execute(sql, params)
        if fetch == "one":
            r = cur.fetchone()
            return dict(r) if r else None
        if fetch == "all":
            return [dict(r) for r in cur.fetchall()]
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ---- drugs / labels / trials -------------------------------------------------

def upsert_drug(rxcui, name, synonyms):
    _exec("INSERT INTO drugs(rxcui,name,synonyms_json,updated_at) VALUES(?,?,?,?) "
          "ON CONFLICT(rxcui) DO UPDATE SET name=excluded.name, synonyms_json=excluded.synonyms_json, updated_at=excluded.updated_at",
          (rxcui, name, json.dumps(synonyms), time.time()))


def get_drug(rxcui):
    return _exec("SELECT * FROM drugs WHERE rxcui=?", (rxcui,), "one")


def get_label(rxcui):
    return _exec("SELECT * FROM labels WHERE rxcui=? ORDER BY fetched_at DESC LIMIT 1", (rxcui,), "one")


def save_label(setid, rxcui, title, effective_time, indications_text, truncated):
    _exec("INSERT INTO labels(setid,rxcui,title,effective_time,indications_text,truncated,fetched_at) VALUES(?,?,?,?,?,?,?) "
          "ON CONFLICT(setid) DO UPDATE SET rxcui=excluded.rxcui, title=excluded.title, effective_time=excluded.effective_time, "
          "indications_text=excluded.indications_text, truncated=excluded.truncated, fetched_at=excluded.fetched_at",
          (setid, rxcui, title, effective_time, indications_text, int(truncated), time.time()))


def get_trial(nct_id):
    r = _exec("SELECT * FROM trials WHERE nct_id=?", (nct_id,), "one")
    if r:
        r["json"] = json.loads(r["json"])
    return r


def save_trial(trial):
    _exec("INSERT INTO trials(nct_id,json,fetched_at) VALUES(?,?,?) "
          "ON CONFLICT(nct_id) DO UPDATE SET json=excluded.json, fetched_at=excluded.fetched_at",
          (trial["nct_id"], json.dumps(trial), time.time()))


def drug_trials_fresh(rxcui, ttl):
    rows = _exec("SELECT nct_id, fetched_at FROM drug_trials WHERE rxcui=?", (rxcui,), "all")
    if not rows:
        return None
    if min(r["fetched_at"] for r in rows) < time.time() - ttl:
        return None
    return [r["nct_id"] for r in rows]


def save_drug_trials(rxcui, nct_ids):
    conn = connect()
    now = time.time()
    conn.execute("DELETE FROM drug_trials WHERE rxcui=?", (rxcui,))
    conn.executemany("INSERT OR REPLACE INTO drug_trials(rxcui,nct_id,fetched_at) VALUES(?,?,?)",
                     [(rxcui, n, now) for n in nct_ids])
    conn.commit()
    conn.close()


# ---- pairs -------------------------------------------------------------------

def upsert_pair(rxcui, drug_name, label_setid, trial, on_label_hint):
    conds = trial.get("conditions") or trial.get("condition_mesh") or []
    row = _exec("SELECT id FROM pairs WHERE rxcui=? AND nct_id=?", (rxcui, trial["nct_id"]), "one")
    params = (drug_name, label_setid, trial.get("title"), conds[0] if conds else None,
              ",".join(trial.get("phases") or []), (trial.get("sponsor") or {}).get("class"),
              (trial.get("sponsor") or {}).get("name"), trial.get("last_update"),
              None if on_label_hint is None else int(on_label_hint), time.time())
    if row:
        _exec("UPDATE pairs SET drug_name=?, label_setid=?, trial_title=?, condition=?, phases=?, sponsor_class=?, sponsor_name=?, "
              "last_update=?, on_label_hint=?, updated_at=? WHERE id=?", params + (row["id"],))
        return row["id"]
    return _exec("INSERT INTO pairs(rxcui,nct_id,drug_name,label_setid,trial_title,condition,phases,sponsor_class,sponsor_name,last_update,"
                 "on_label_hint,updated_at,stage) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'paired')",
                 (rxcui, trial["nct_id"]) + params)


def get_pair(pair_id):
    return _exec("SELECT * FROM pairs WHERE id=?", (pair_id,), "one")


def update_pair_gate(pair_id, p_off, p_active):
    _exec("UPDATE pairs SET p_off_label=?, p_active=?, stage='gated', updated_at=? WHERE id=?",
          (p_off, p_active, time.time(), pair_id))


def update_pair_judge(pair_id, plaus, plaus_conf, category, cat_conf, cat_probs):
    _exec("UPDATE pairs SET plausibility=?, plaus_conf=?, category=?, cat_conf=?, cat_probs=?, stage='judged', updated_at=? WHERE id=?",
          (plaus, plaus_conf, category, cat_conf, json.dumps(cat_probs), time.time(), pair_id))


def set_rank(pair_id, score):
    _exec("UPDATE pairs SET rank_score=? WHERE id=?", (score, pair_id))


def all_pairs():
    return _exec("SELECT * FROM pairs", (), "all")


def list_candidates():
    return _exec("SELECT id, rxcui, nct_id, drug_name, trial_title, condition, phases, sponsor_class, sponsor_name, last_update, "
                 "on_label_hint, p_off_label, p_active, plausibility, plaus_conf, category, cat_conf, rank_score, stage "
                 "FROM pairs ORDER BY rank_score DESC NULLS LAST, p_off_label DESC NULLS LAST", (), "all")


# ---- pubmed / evidence -------------------------------------------------------

def save_pubmed(article):
    _exec("INSERT INTO pubmed(pmid,json,fetched_at) VALUES(?,?,?) ON CONFLICT(pmid) DO UPDATE SET json=excluded.json, fetched_at=excluded.fetched_at",
          (article["pmid"], json.dumps(article), time.time()))


def get_pubmed(pmid):
    r = _exec("SELECT json FROM pubmed WHERE pmid=?", (pmid,), "one")
    return json.loads(r["json"]) if r else None


def set_evidence(pair_id, pmids):
    conn = connect()
    conn.execute("DELETE FROM pair_evidence WHERE pair_id=?", (pair_id,))
    conn.executemany("INSERT OR REPLACE INTO pair_evidence(pair_id,pmid,rank) VALUES(?,?,?)",
                     [(pair_id, p, i) for i, p in enumerate(pmids)] or [(pair_id, "__none__", -1)])
    conn.commit()
    conn.close()


def get_evidence(pair_id):
    rows = _exec("SELECT e.pmid, p.json FROM pair_evidence e LEFT JOIN pubmed p ON p.pmid=e.pmid WHERE e.pair_id=? ORDER BY e.rank",
                 (pair_id,), "all")
    return [json.loads(r["json"]) for r in rows if r["json"]]


def has_evidence(pair_id):
    return _exec("SELECT COUNT(1) AS c FROM pair_evidence WHERE pair_id=?", (pair_id,), "one")["c"] > 0


# ---- jev ---------------------------------------------------------------------

def get_jev_cached(state_hash):
    r = _exec("SELECT response_json FROM jev_calls WHERE state_hash=? ORDER BY id DESC LIMIT 1", (state_hash,), "one")
    return json.loads(r["response_json"]) if r else None


def save_jev_call(pair_id, stage, model, state_hash, request, response, input_tokens):
    _exec("INSERT INTO jev_calls(pair_id,stage,model,state_hash,request_json,response_json,input_tokens,created_at) VALUES(?,?,?,?,?,?,?,?)",
          (pair_id, stage, model, state_hash, json.dumps(request), json.dumps(response), input_tokens, time.time()))


def jev_calls_for_pair(pair_id):
    rows = _exec("SELECT stage, model, state_hash, request_json, response_json, input_tokens, created_at FROM jev_calls WHERE pair_id=? ORDER BY id",
                 (pair_id,), "all")
    out = {}
    for r in rows:  # last call per stage wins
        out[r["stage"]] = {"model": r["model"], "state_hash": r["state_hash"], "request": json.loads(r["request_json"]),
                           "response": json.loads(r["response_json"]), "input_tokens": r["input_tokens"], "created_at": r["created_at"]}
    return out


# ---- runs / stats ------------------------------------------------------------

def start_run(drugs):
    return _exec("INSERT INTO runs(started_at,seed_drugs_json,status) VALUES(?,?,'running')", (time.time(), json.dumps(drugs)))


def finish_run(run_id, model, counts, status, error=None):
    _exec("UPDATE runs SET finished_at=?, jev_model=?, counts_json=?, status=?, error=? WHERE id=?",
          (time.time(), model, json.dumps(counts), status, error, run_id))


# ---- condition mode ----------------------------------------------------------

def get_intervention_map(key):
    r = _exec("SELECT rxcui, ingredient, multi FROM intervention_map WHERE key=?", (key,), "one")
    return r  # None = never resolved; dict with rxcui None = resolved to nothing


def save_intervention_map(key, raw, result):
    _exec("INSERT INTO intervention_map(key,raw,rxcui,ingredient,multi,resolved_at) VALUES(?,?,?,?,?,?) "
          "ON CONFLICT(key) DO UPDATE SET raw=excluded.raw, rxcui=excluded.rxcui, ingredient=excluded.ingredient, multi=excluded.multi, resolved_at=excluded.resolved_at",
          (key, raw, (result or {}).get("rxcui"), (result or {}).get("ingredient"), int((result or {}).get("multi") or 0), time.time()))


def upsert_condition(query, mesh_match, mesh_probs, n_trials, drugs):
    q = query.lower().strip()
    _exec("INSERT INTO conditions(query,mesh_match,mesh_probs,n_trials,drugs_json,updated_at) VALUES(?,?,?,?,?,?) "
          "ON CONFLICT(query) DO UPDATE SET mesh_match=excluded.mesh_match, mesh_probs=excluded.mesh_probs, n_trials=excluded.n_trials, "
          "drugs_json=excluded.drugs_json, updated_at=excluded.updated_at",
          (q, mesh_match, json.dumps(mesh_probs or {}), n_trials, json.dumps(drugs), time.time()))
    return _exec("SELECT id FROM conditions WHERE query=?", (q,), "one")["id"]


def set_condition_pairs(condition_id, pair_ids):
    conn = connect()
    conn.execute("DELETE FROM condition_pairs WHERE condition_id=?", (condition_id,))
    conn.executemany("INSERT OR REPLACE INTO condition_pairs(condition_id,pair_id) VALUES(?,?)", [(condition_id, p) for p in pair_ids])
    conn.commit()
    conn.close()


def list_conditions():
    rows = _exec("SELECT c.*, (SELECT COUNT(1) FROM condition_pairs cp WHERE cp.condition_id=c.id) AS n_pairs "
                 "FROM conditions c ORDER BY c.updated_at DESC", (), "all")
    for r in rows:
        r["drugs"] = json.loads(r.pop("drugs_json") or "[]")
        r["mesh_probs"] = json.loads(r["mesh_probs"] or "{}")
    return rows


def list_condition_candidates(condition_id):
    return _exec("SELECT p.id, p.rxcui, p.nct_id, p.drug_name, p.trial_title, p.condition, p.phases, p.sponsor_class, p.sponsor_name, p.last_update, "
                 "p.on_label_hint, p.p_off_label, p.p_active, p.plausibility, p.plaus_conf, p.category, p.cat_conf, p.rank_score, p.stage "
                 "FROM pairs p JOIN condition_pairs cp ON cp.pair_id=p.id WHERE cp.condition_id=? "
                 "ORDER BY p.rank_score DESC NULLS LAST, p.p_off_label DESC NULLS LAST", (condition_id,), "all")


def stats():
    return {
        "drugs": _exec("SELECT COUNT(1) c FROM drugs", (), "one")["c"],
        "labels": _exec("SELECT COUNT(1) c FROM labels", (), "one")["c"],
        "trials": _exec("SELECT COUNT(1) c FROM trials", (), "one")["c"],
        "pairs": _exec("SELECT COUNT(1) c FROM pairs", (), "one")["c"],
        "gated": _exec("SELECT COUNT(1) c FROM pairs WHERE p_off_label IS NOT NULL", (), "one")["c"],
        "judged": _exec("SELECT COUNT(1) c FROM pairs WHERE plausibility IS NOT NULL", (), "one")["c"],
        "jev_calls": _exec("SELECT COUNT(1) c FROM jev_calls", (), "one")["c"],
        "jev_tokens": _exec("SELECT COALESCE(SUM(input_tokens),0) c FROM jev_calls", (), "one")["c"],
        "last_run": _exec("SELECT * FROM runs ORDER BY id DESC LIMIT 1", (), "one"),
    }