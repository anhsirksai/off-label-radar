import asyncio
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import condition
import db
import jev
import net
import pipeline

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
DEFAULT_SEEDS = [s.strip() for s in os.environ.get(
    "SEED_DRUGS", "metformin,sirolimus,semaglutide,ketamine,colchicine,propranolol,naltrexone,minoxidil,disulfiram,losartan").split(",") if s.strip()]

app = FastAPI(title="Off-Label Radar")
READY = False
RUN = net.RunState()
TASK = None


@app.on_event("startup")
async def _startup():
    global READY
    db.init()
    READY = True


@app.get("/readyz")
def readyz():
    return Response(status_code=204 if READY else 503)


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/api/status")
def status(request: Request):
    return {"run": RUN.snapshot(), "stats": db.stats(), "default_seeds": DEFAULT_SEEDS,
            "threshold": pipeline.THRESHOLD, "max_trials": pipeline.MAX_TRIALS,
            "jev_provider": os.environ.get("JEV_PROVIDER", "__typesafe-api"),
            "signed_in": bool(request.headers.get("x-promptql-visitor-token") or jev.DIRECT_KEY)}


@app.get("/api/candidates")
def candidates(condition: int | None = None):
    return db.list_condition_candidates(condition) if condition else db.list_candidates()


@app.get("/api/conditions")
def conditions():
    return db.list_conditions()


@app.get("/api/candidates/{pair_id}")
def candidate(pair_id: int):
    p = db.get_pair(pair_id)
    if not p:
        raise HTTPException(404)
    trial = db.get_trial(p["nct_id"])
    label = db.get_label(p["rxcui"]) or {}
    drug = db.get_drug(p["rxcui"]) or {}
    return {"pair": p, "trial": trial["json"] if trial else None, "label": label, "drug": drug,
            "evidence": db.get_evidence(pair_id), "jev": db.jev_calls_for_pair(pair_id)}


@app.post("/api/run")
async def start_run(request: Request):
    global TASK
    token = request.headers.get("x-promptql-visitor-token") or jev.DIRECT_KEY
    if not token:
        raise HTTPException(401, "Open this app from inside PromptQL (Jev calls run as you), or set TYPESAFE_API_KEY in .env for a local build.")
    if RUN.running:
        raise HTTPException(409, "A run is already in progress.")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    drugs = [d.strip() for d in (body.get("drugs") or DEFAULT_SEEDS) if d.strip()][:40]
    threshold = float(body.get("threshold") or pipeline.THRESHOLD)
    max_trials = int(body.get("max_trials") or pipeline.MAX_TRIALS)
    force = bool(body.get("force"))
    TASK = asyncio.create_task(pipeline.run(drugs, token, RUN, threshold, max_trials, force))
    return {"started": True, "drugs": drugs, "force": force}


@app.post("/api/run_condition")
async def start_condition_run(request: Request):
    global TASK
    token = request.headers.get("x-promptql-visitor-token") or jev.DIRECT_KEY
    if not token:
        raise HTTPException(401, "Open this app from inside PromptQL (Jev calls run as you), or set TYPESAFE_API_KEY in .env for a local build.")
    if RUN.running:
        raise HTTPException(409, "A run is already in progress.")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    q = (body.get("condition") or "").strip()[:120]
    if not q:
        raise HTTPException(400, "Type a medical condition first.")
    threshold = float(body.get("threshold") or pipeline.THRESHOLD)
    max_trials = int(body.get("max_trials") or pipeline.MAX_TRIALS)
    force = bool(body.get("force"))
    TASK = asyncio.create_task(condition.run_condition(q, token, RUN, threshold, max_trials, force))
    return {"started": True, "condition": q, "force": force}


@app.get("/api/export.csv")
def export_csv():
    import csv
    import io
    rows = db.list_candidates()
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else ["id"])
    w.writeheader()
    w.writerows(rows)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=off_label_radar_candidates.csv"})