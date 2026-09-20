"""Generates docs/flow.svg + static/flow.svg — the program flow / decision tree as a static diagram."""
import html
import os

HERE = os.path.dirname(os.path.abspath(__file__))
W, H = 1180, 1010
COL = {
    "io": ("#22313f", "#7aa7d6"), "step": ("#1d2732", "#5b7fa6"), "jev": ("#3b2d13", "#e6a83c"),
    "on": ("#153524", "#4fc27a"), "out": ("#3a2211", "#f0a35a"), "note": ("#1a1f26", "#5a6472"), "dec": ("#2a1f3a", "#b489e6"),
}
nodes, edges = {}, []


def n(i, x, y, w, h, title, lines=(), kind="step"):
    nodes[i] = dict(x=x, y=y, w=w, h=h, title=title, lines=list(lines), kind=kind)


def e(a, b, label="", pts=None, dashed=False):
    edges.append((a, b, label, pts, dashed))


# ---- drug mode (left lane) ----
n("in_d", 60, 60, 280, 52, "Input: drug names", ["comma-separated · e.g. colchicine, ketamine"], "io")
n("rx", 60, 146, 280, 60, "RxNorm (rxnav.nlm.nih.gov)", ["name → rxcui + synonyms (IN / BN / PIN)"])
n("dm", 60, 238, 280, 60, "DailyMed SPL", ["Indications & Usage section (LOINC 34067-9), verbatim · cached 30 d"])
n("ct", 60, 330, 280, 60, "ClinicalTrials.gov API v2 — by intervention", ["active statuses · newest N per drug · cached 7 d"])
# ---- condition mode (right lane) ----
n("in_c", 760, 60, 300, 52, "Input: medical condition (plain text)", ["e.g. alopecia areata, treatment-resistant depression"], "io")
n("cc", 760, 146, 300, 60, "ClinicalTrials.gov API v2 — by condition", ["DRUG / BIOLOGICAL trials · active · newest N"])
n("mesh", 760, 238, 300, 60, "Jev · choice: condition_match", ["which MeSH term did the user mean? → “you asked X, we searched Y”"], "jev")
n("ri", 760, 330, 300, 60, "Interventions → RxNorm ingredient", ["exact then approximate match · skip placebo / saline · skip fixed-dose combos"])
n("dm2", 760, 422, 300, 52, "DailyMed SPL — one label per ingredient", [], "step")
# ---- shared ----
n("pairs", 400, 520, 380, 60, "(drug, trial) pairs", ["+ heuristic hint: does a trial condition appear verbatim in the label text?"])
n("gate", 330, 616, 520, 78, "Jev · gate (2 × noul)", [
    "off_label — is the trial outside the approved Indications & Usage?  → P(off-label)",
    "is_active_arm — is the drug the studied intervention, not comparator?  → P(active) < 0.5 ⇒ “comparator?” flag"], "jev")
n("dec", 440, 730, 300, 70, "P(off-label) ≥ threshold (0.6)?", [], "dec")
n("on", 60, 730, 260, 70, "✅ Label already covers it", ["approved use / standard of care", "no evidence pulled, no further Jev calls"], "on")
n("pm", 860, 730, 260, 60, "PubMed E-utilities", ["top-5 abstracts: drug AND condition", "phrase → word-AND → MeSH fallback"])
n("judge", 860, 822, 260, 78, "Jev · judge (score + choice)", [
    "plausibility — 1–5 rubric: incidental → RCT evidence", "category — new_indication / label_extension /", "combination / unclear"], "jev")
n("rank", 860, 932, 260, 56, "Rank", ["P(off) × P(active) × plaus/5 × phase × recency"])
n("out", 330, 932, 460, 56, "🧪 Ranked off-label candidates", [
    "table · drawer (label vs trial, abstracts, Jev distributions) · CSV export · flags: comparator? · disagree"], "out")
n("cache", 60, 850, 260, 110, "SQLite cache (radar.db)", [
    "labels 30 d · trials 7 d · PubMed 30 d", "Jev answers keyed by hash(state, questions, model)",
    "☐ re-judge (ignore cache) forces fresh Jev calls", "per-host rate limiters at ~70% of API ceilings"], "note")

e("in_d", "rx"); e("rx", "dm"); e("dm", "ct"); e("ct", "pairs")
e("in_c", "cc"); e("cc", "mesh"); e("mesh", "ri"); e("ri", "dm2"); e("dm2", "pairs")
e("pairs", "gate"); e("gate", "dec")
e("dec", "on", "no", pts=[(440, 765), (320, 765)])
e("dec", "pm", "yes", pts=[(740, 765), (860, 765)])
e("pm", "judge"); e("judge", "rank")
e("rank", "out", "", pts=[(860, 960), (790, 960)])
e("cache", "gate", "", pts=[(190, 850), (190, 655), (330, 655)], dashed=True)


def esc(s):
    return html.escape(s, quote=True)


def text(x, y, s, size=13, bold=False, anchor="middle", fill="#e8eef6", op=1.0):
    return (f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{"600" if bold else "400"}" text-anchor="{anchor}" '
            f'fill="{fill}" fill-opacity="{op}">{esc(s)}</text>')


def node_svg(d):
    fill, stroke = COL[d["kind"]]
    x, y, w, h = d["x"], d["y"], d["w"], d["h"]
    out = []
    if d["kind"] == "dec":
        cx, cy = x + w / 2, y + h / 2
        out.append(f'<polygon points="{x},{cy} {cx},{y} {x+w},{cy} {cx},{y+h}" fill="{fill}" stroke="{stroke}" stroke-width="1.6"/>')
        out.append(text(cx, cy + 5, d["title"], 13, True))
        return "\n".join(out)
    dash = ' stroke-dasharray="5 4"' if d["kind"] == "note" else ""
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="{fill}" stroke="{stroke}" stroke-width="1.6"{dash}/>')
    if d["kind"] == "jev":
        out.append(f'<rect x="{x+w-38}" y="{y+6}" width="32" height="16" rx="8" fill="{stroke}"/>')
        out.append(text(x + w - 22, y + 18, "Jev", 10, True, fill="#1a1204"))
    ty = y + 20 if d["lines"] else y + h / 2 + 5
    out.append(text(x + w / 2, ty, d["title"], 13.5, True))
    for i, ln in enumerate(d["lines"]):
        out.append(text(x + w / 2, ty + 17 + i * 15, ln, 11, False, fill="#c9d4e3"))
    return "\n".join(out)


def edge_svg(a, b, label, pts, dashed):
    A, B = nodes[a], nodes[b]
    if pts is None:
        sx, sy = A["x"] + A["w"] / 2, A["y"] + A["h"]
        ex, ey = B["x"] + B["w"] / 2, B["y"]
        if abs(sx - ex) < 1:
            pts = [(sx, sy), (ex, ey)]
        else:
            my = sy + (ey - sy) / 2
            pts = [(sx, sy), (sx, my), (ex, my), (ex, ey)]
    d = "M " + " L ".join(f"{x},{y}" for x, y in pts)
    dash = ' stroke-dasharray="4 4"' if dashed else ""
    s = f'<path d="{d}" fill="none" stroke="#8aa0b8" stroke-width="1.5" marker-end="url(#arr)"{dash}/>'
    if label:
        (x1, y1), (x2, y2) = pts[0], pts[-1]
        s += text((x1 + x2) / 2, min(y1, y2) - 6, label, 12, True, fill="#f3c969")
    return s


parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif">',
         '<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
         '<path d="M 0 0 L 10 5 L 0 10 z" fill="#8aa0b8"/></marker></defs>',
         f'<rect width="{W}" height="{H}" fill="#0f141b"/>',
         text(W / 2, 30, "Off-Label Radar — program flow & decision tree", 20, True),
         text(200, 48, "BY DRUG", 11, True, fill="#7aa7d6"), text(910, 48, "BY CONDITION", 11, True, fill="#7aa7d6"),
         f'<line x1="590" y1="40" x2="590" y2="500" stroke="#2a3441" stroke-dasharray="3 6"/>',
         text(590, 500, "shared from here", 11, False, fill="#7c8a9b")]
parts += [edge_svg(*ed) for ed in edges]
parts += [node_svg(d) for d in nodes.values()]
# legend
lx, ly = 60, 985
for k, lab in (("step", "public API step"), ("jev", "Jev typed decision"), ("dec", "threshold"), ("on", "on-label bucket"), ("out", "deliverable"), ("note", "cache / politeness")):
    f, s = COL[k]
    parts.append(f'<rect x="{lx}" y="{ly-11}" width="14" height="14" rx="3" fill="{f}" stroke="{s}"/>')
    parts.append(text(lx + 20, ly, lab, 11, False, "start", fill="#c9d4e3"))
    lx += 22 + 8 * len(lab) + 24
parts.append("</svg>")
svg = "\n".join(parts)
os.makedirs(os.path.join(HERE, "docs"), exist_ok=True)
for p in ("static/flow.svg", "docs/flow.svg"):
    with open(os.path.join(HERE, p), "w") as f:
        f.write(svg)
print("wrote flow.svg", len(svg), "bytes")