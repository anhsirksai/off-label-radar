"""Polite networking: per-host rate limiters, backoff, Retry-After, shared run state."""
import asyncio
import os
import random
import time

import httpx

CONTACT = os.environ.get("CONTACT_EMAIL", "krishna1256@gmail.com")
UA = f"OffLabelRadar/0.1 (contact: {CONTACT})"
NCBI_KEY = os.environ.get("NCBI_API_KEY") or None


class RateLimiter:
    """Serializes calls per host at ~70% of the documented ceiling, with jitter."""

    def __init__(self, per_sec: float):
        self.interval = 1.0 / per_sec
        self.lock = asyncio.Lock()
        self.next_at = 0.0

    async def wait(self):
        async with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval * random.uniform(1.0, 1.3)
        if delay > 0:
            await asyncio.sleep(delay)


# documented ceilings → run rate
LIMITS = {
    "ctgov": RateLimiter(30 / 60),                 # ceiling 50/min
    "dailymed": RateLimiter(1.0),                  # no published quota; be gentle
    "rxnorm": RateLimiter(5.0),                    # ceiling 20/s
    "pubmed": RateLimiter(7.0 if NCBI_KEY else 2.5),  # ceiling 10/s with key, 3/s without
    "jev": RateLimiter(15.0),                      # ceiling ~1200/min
}
CEILINGS = {
    "ctgov": "≤50/min", "dailymed": "~1/s", "rxnorm": "≤20/s",
    "pubmed": "≤10/s (key)" if NCBI_KEY else "≤3/s (no key)", "jev": "≤1200/min",
}


class RunState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.running = False
        self.stage = "idle"
        self.message = ""
        self.counts = {k: 0 for k in LIMITS}
        self.backoffs = 0
        self.jev_calls = 0
        self.jev_cached = 0
        self.jev_reused = 0
        self.force = False
        self.jev_tokens = 0
        self.jev_model = None
        self.pairs_total = 0
        self.pairs_done = 0
        self.started_at = None
        self.finished_at = None
        self.log = []
        self.error = None
        self.drugs = []
        self.mode = "drugs"
        self.condition = None
        self.mesh_match = None

    def note(self, msg):
        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")
        self.log = self.log[-60:]
        self.message = msg

    def snapshot(self):
        end = self.finished_at or time.time()
        elapsed = (end - self.started_at) if self.started_at else 0.0
        return {
            "running": self.running, "stage": self.stage, "message": self.message,
            "counts": self.counts, "ceilings": CEILINGS, "backoffs": self.backoffs,
            "jev_calls": self.jev_calls, "jev_cached": self.jev_cached, "jev_reused": self.jev_reused,
            "force": self.force, "jev_tokens": self.jev_tokens,
            "jev_cost_cents": round(self.jev_tokens / 1_000_000 * 4.2, 3), "jev_model": self.jev_model,
            "pairs_total": self.pairs_total, "pairs_done": self.pairs_done,
            "elapsed_s": round(elapsed, 1), "log": self.log, "error": self.error, "drugs": self.drugs,
            "mode": self.mode, "condition": self.condition, "mesh_match": self.mesh_match,
        }


async def fetch(client: httpx.AsyncClient, host: str, method: str, url: str, st: RunState, **kw):
    """Rate-limited request with exponential backoff; honors Retry-After on 429/503/529."""
    last = None
    for attempt in range(6):
        await LIMITS[host].wait()
        st.counts[host] += 1
        try:
            r = await client.request(method, url, **kw)
        except httpx.HTTPError as e:
            last = e
            st.backoffs += 1
            st.note(f"{host}: {type(e).__name__}, retrying")
            await asyncio.sleep(1.5 * 2 ** attempt + random.random())
            continue
        if r.status_code in (429, 500, 502, 503, 529):
            ra = r.headers.get("retry-after")
            try:
                delay = float(ra) if ra else 1.5 * 2 ** attempt
            except ValueError:
                delay = 1.5 * 2 ** attempt
            st.backoffs += 1
            st.note(f"{host}: HTTP {r.status_code}, backing off {delay:.1f}s")
            await asyncio.sleep(delay + random.random())
            last = r
            continue
        return r
    raise RuntimeError(f"{host}: gave up after retries ({last})")