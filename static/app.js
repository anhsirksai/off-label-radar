const $ = s => document.querySelector(s);
let ROWS = [], STATUS = null, polling = false, wasRunning = false, COND = null, CONDS = [];

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) { let t = await r.text(); try { t = JSON.parse(t).detail || t; } catch (e) {} throw new Error(t); }
  return r.json();
}
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = (n, d = 2) => (n == null ? '—' : Number(n).toFixed(d));

function pbar(v, cls) {
  if (v == null) return '<span class="muted">—</span>';
  const pct = Math.round(v * 100);
  const c = cls || (v >= 0.8 ? 'good' : v >= 0.6 ? '' : 'warn');
  return `<div class="pbar ${c}"><div class="t"><div class="f" style="width:${pct}%"></div></div><span class="n">${fmt(v)}</span></div>`;
}
function plausBar(v, conf) {
  if (v == null) return '<span class="muted">—</span>';
  const pct = Math.round((v - 1) / 4 * 100);
  return `<div class="pbar" title="confidence ${fmt(conf)}"><div class="t"><div class="f" style="width:${pct}%;background:var(--good)"></div></div><span class="n">${fmt(v, 1)}</span></div>`;
}
const phaseTxt = p => !p ? 'N/A' : p.split(',').map(x => x.replace('PHASE', 'P').replace('NA', 'N/A').replace('EARLY_P1', 'Early P1')).join('/');

// ---- status / counters --------------------------------------------------
function renderStatus(s) {
  STATUS = s;
  const r = s.run, st = s.stats;
  $('#stage').textContent = r.stage;
  $('#msg').textContent = r.message || '';
  const pct = r.pairs_total ? Math.round(r.pairs_done / r.pairs_total * 100) : (r.running ? 5 : 0);
  $('#prog').style.width = pct + '%';
  $('#log').textContent = r.log.join('\n'); $('#log').scrollTop = 1e9;
  $('#runBtn').disabled = r.running; $('#runCondBtn').disabled = r.running;
  const judged = st.gated;
  $('#cPairs').textContent = r.running ? r.pairs_done : judged;
  const rate = r.elapsed_s > 0 ? (r.jev_calls + r.jev_cached + r.jev_reused) / r.elapsed_s : 0;
  $('#cRate').textContent = rate.toFixed(1);
  $('#cCost').textContent = r.jev_cost_cents.toFixed(2) + '¢';
  $('#cHours').textContent = ((r.running ? r.pairs_done : judged) * 10 / 60).toFixed(1) + ' h';
  $('#cJev').textContent = r.jev_calls;
  $('#cCached').textContent = (r.jev_cached || r.jev_reused) ? `(+${r.jev_cached} cached, ${r.jev_reused} reused from DB — tick "re-judge" to call Jev again)` : (r.force ? '(cache bypassed)' : '');
  $('#cTok').textContent = r.jev_tokens.toLocaleString();
  $('#hosts').innerHTML = Object.keys(r.counts).map(h => `<tr><td>${h}</td><td>${r.counts[h]}</td><td>${r.ceilings[h]}</td></tr>`).join('');
  $('#dbStats').textContent = `DB: ${st.drugs} drugs · ${st.labels} labels · ${st.trials} trials · ${st.pairs} pairs · ${st.judged} judged · ${st.jev_calls} Jev calls total (${st.jev_tokens.toLocaleString()} tok ≈ ${(st.jev_tokens / 1e6 * 4.2).toFixed(2)}¢) · Jev ${r.jev_model || 'jev-latest'} via ${s.jev_provider}${r.backoffs ? ' · backoffs: ' + r.backoffs : ''}`;
  if (!$('#drugs').value) $('#drugs').value = s.default_seeds.join(', ');
  if (!s.signed_in) $('#runMsg').textContent = 'Read-only view (open from PromptQL to run).';
  if (wasRunning && !r.running) loadConds(r.mode === 'condition' ? r.condition : undefined);
  wasRunning = r.running;
}
async function poll() {
  try { renderStatus(await api('/api/status')); } catch (e) { $('#msg').textContent = e.message; }
  setTimeout(poll, STATUS && STATUS.run.running ? 1000 : 6000);
}

// ---- table ---------------------------------------------------------------
function filters() {
  return { drug: $('#fDrug').value.toLowerCase(), cat: $('#fCat').value, phase: $('#fPhase').value, sponsor: $('#fSponsor').value,
    poff: +$('#fPoff').value, pl: +$('#fPl').value, combo: $('#fCombo').checked, all: $('#fAll').checked };
}
function renderRows() {
  const f = filters();
  $('#fPoffV').textContent = f.poff.toFixed(2); $('#fPlV').textContent = f.pl.toFixed(1);
  const out = ROWS.filter(r => {
    if (f.drug && !(r.drug_name || '').toLowerCase().includes(f.drug)) return false;
    if (f.cat && r.category !== f.cat) return false;
    if (f.phase && !((r.phases || 'NA').includes(f.phase))) return false;
    if (f.sponsor && r.sponsor_class !== f.sponsor) return false;
    if (COND && r.p_off_label != null && r.p_off_label < f.poff) return true; // condition mode keeps the "label covers it" bucket
    if (!f.all) {
      if (r.p_off_label == null || r.p_off_label < f.poff) return false;
      if (r.plausibility != null && r.plausibility < f.pl) return false;
      if (!f.combo && (r.category === 'combination' || r.category === 'unclear')) return false;
    }
    return true;
  });
  $('#count').textContent = `${out.length} of ${ROWS.length} pairs`;
  const rowHtml = (r, i) => {
    const dis = r.p_off_label != null && r.on_label_hint != null && ((r.on_label_hint === 1) === (r.p_off_label >= 0.5));
    const flags = (dis ? '<span class="flag dis" title="heuristic and Jev disagree — worth a human look">disagree</span>' : '') +
      (r.on_label_hint === 1 ? '<span class="flag hint" title="a trial condition appears verbatim in the label">on-label hint</span>' : '') +
      (r.p_active != null && r.p_active < 0.5 ? '<span class="flag" title="Jev thinks the drug is comparator/background">comparator?</span>' : '');
    return `<tr class="r" tabindex="0" data-id="${r.id}">
      <td class="muted">${i + 1}</td>
      <td><b>${esc(r.drug_name)}</b></td>
      <td><span class="title">${esc(r.trial_title)}</span><a class="nct" href="https://clinicaltrials.gov/study/${r.nct_id}" target="_blank" rel="noopener">${r.nct_id} ↗</a></td>
      <td><span class="cond">${esc(r.condition)}</span></td>
      <td>${r.category ? `<span class="badge ${r.category}">${r.category.replace('_', ' ')}</span>` : '<span class="muted">—</span>'}</td>
      <td>${pbar(r.p_off_label)}</td>
      <td>${plausBar(r.plausibility, r.plaus_conf)}</td>
      <td>${phaseTxt(r.phases)}</td>
      <td class="muted">${esc(r.sponsor_class || '')}</td>
      <td class="muted">${esc(r.last_update || '')}</td>
      <td>${flags}</td>
      <td>${fmt(r.rank_score, 3)}</td></tr>`;
  };
  if (!COND) { $('#rows').innerHTML = out.map(rowHtml).join(''); $('#condSummary').innerHTML = ''; return; }
  const off = out.filter(r => r.p_off_label >= f.poff), on = out.filter(r => r.p_off_label < f.poff);
  const grp = (icon, title, rows) => rows.length ? `<tr class="grp"><td colspan="12">${icon} ${title} <span class="muted">— ${rows.length} trial${rows.length === 1 ? '' : 's'}</span></td></tr>` + rows.map(rowHtml).join('') : '';
  $('#rows').innerHTML = grp('🧪', `Approved drugs being tested <b>off-label</b> for “${esc(COND.query)}”`, off)
    + grp('✅', `Label already covers “${esc(COND.query)}” — approved use / standard of care`, on);
  renderCondSummary(off, on);
}
function drugChips(rows, cls) {
  const m = {};
  rows.forEach(r => { const d = m[r.drug_name] || (m[r.drug_name] = { n: 0, p: 0, pl: 0 }); d.n++; d.p = Math.max(d.p, r.p_off_label || 0); d.pl = Math.max(d.pl, r.plausibility || 0); });
  return Object.entries(m).sort((a, b) => b[1].pl - a[1].pl || b[1].p - a[1].p || b[1].n - a[1].n)
    .map(([k, d]) => `<span class="chip ${cls}" data-drug="${esc(k)}" title="${d.n} trial(s) · max P(off-label) ${fmt(d.p)} · max plausibility ${fmt(d.pl, 1)}"><b>${esc(k)}</b><span class="muted">${d.n}×${cls === 'off' && d.pl ? ' · plaus ' + fmt(d.pl, 1) : ''}</span></span>`).join('')
    || '<span class="muted">none</span>';
}
function renderCondSummary(off, on) {
  const c = COND;
  $('#condSummary').innerHTML = `<div class="cs">
    <div>You asked <b>“${esc(c.query)}”</b> → Jev mapped it to MeSH <b>${esc(c.mesh_match || 'no clean match among top terms')}</b> · ${c.n_trials} newest active trials scanned · ${c.drugs.length} drug ingredients resolved and checked against their FDA labels.</div>
    <div class="csrow"><span class="lbl off">Being tested off-label</span>${drugChips(off, 'off')}</div>
    <div class="csrow"><span class="lbl on">Label already covers it</span>${drugChips(on, 'on')}</div>
    <div class="muted small">Research signal from public registries and labels — <b>not medical advice</b>. Click a chip to filter to that drug, a row for the label text, trial, abstracts and Jev's probabilities.</div></div>`;
  $('#condSummary').querySelectorAll('.chip').forEach(ch => ch.onclick = () => { $('#fDrug').value = $('#fDrug').value === ch.dataset.drug ? '' : ch.dataset.drug; renderRows(); });
}
async function loadConds(selectQuery) {
  try { CONDS = await api('/api/conditions'); } catch (e) { CONDS = []; }
  const sel = $('#fCond');
  sel.innerHTML = '<option value="">all runs (drug mode)</option>' + CONDS.map(c => `<option value="${c.id}">condition: ${esc(c.query)} (${c.n_pairs} pairs)</option>`).join('');
  if (selectQuery !== undefined) { const hit = CONDS.find(c => c.query === (selectQuery || '').toLowerCase().trim()); sel.value = hit ? hit.id : ''; }
  else if (COND) sel.value = COND.id;
  COND = CONDS.find(c => String(c.id) === sel.value) || null;
  await loadRows();
}
async function loadRows() { ROWS = await api('/api/candidates' + (COND ? '?condition=' + COND.id : '')); renderRows(); }

// ---- drawer --------------------------------------------------------------
function highlight(text, terms) {
  let h = esc(text);
  terms.filter(t => t && t.length > 3).sort((a, b) => b.length - a.length).forEach(t => {
    const re = new RegExp(esc(t).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
    h = h.replace(re, m => `<mark>${m}</mark>`);
  });
  return h;
}
function dist(probs, legend) {
  const keys = Object.keys(probs || {});
  return `<div class="dist">${keys.map(k => `<div class="d"><span class="l" title="${esc(legend && legend[k] || '')}">${esc(legend && legend[k] ? ((Number(k) + 1) + ' · ' + legend[k].slice(0, 22) + '…') : k.replace('_', ' '))}</span><div class="t"><div class="f" style="width:${Math.round(probs[k] * 100)}%"></div></div><span class="n">${fmt(probs[k])}</span></div>`).join('')}</div>`;
}
async function openDrawer(id) {
  const d = await api('/api/candidates/' + id);
  const p = d.pair, t = d.trial || {}, lab = d.label || {}, terms = [...(t.conditions || []), ...(t.condition_mesh || [])];
  const gate = d.jev.gate, judge = d.jev.judge;
  $('#dTitle').innerHTML = `${esc(p.drug_name)} × <a class="nct" href="https://clinicaltrials.gov/study/${p.nct_id}" target="_blank" rel="noopener">${p.nct_id}</a> — ${esc(t.title || p.trial_title)}`;
  let html = `<div class="kv">
    <span class="k">Category</span><span>${p.category ? `<span class="badge ${p.category}">${p.category.replace('_', ' ')}</span> confidence ${fmt(p.cat_conf)}` : '—'}</span>
    <span class="k">P(off-label)</span><span>${fmt(p.p_off_label)} &nbsp; <span class="muted">P(active arm) ${fmt(p.p_active)}</span></span>
    <span class="k">Plausibility</span><span>${fmt(p.plausibility, 2)} / 5 <span class="muted">confidence ${fmt(p.plaus_conf)}</span></span>
    <span class="k">Rank score</span><span>${fmt(p.rank_score, 3)} <span class="muted">= P(off) × P(active) × plaus/5 × phase × recency</span></span>
    <span class="k">Phase / status</span><span>${phaseTxt(p.phases)} · ${esc(t.status || '')} · started ${esc(t.start_date || '?')} · updated ${esc(t.last_update || '?')}</span>
    <span class="k">Sponsor</span><span>${esc(t.sponsor && t.sponsor.name)} <span class="muted">(${esc(p.sponsor_class || '')})</span></span>
    <span class="k">Label</span><span>${esc(lab.title || '')} <span class="muted">setid ${esc(lab.setid || '')} · effective ${esc(lab.effective_time || '?')}${lab.truncated ? ' · <span class="flag trunc">truncated for Jev</span>' : ''}</span></span>
    <span class="k">Heuristic</span><span>${p.on_label_hint == null ? 'no label text' : p.on_label_hint ? 'condition found in label (on-label hint)' : 'no condition term found in label'}</span>
  </div>
  <div class="two">
    <div><h3>Approved Indications &amp; Usage (verbatim, DailyMed)</h3><div class="box">${highlight(lab.indications_text || '(none)', terms)}</div></div>
    <div><h3>Trial</h3><div class="box">
      <b>Conditions:</b> ${esc((t.conditions || []).join('; '))}<br><b>MeSH:</b> <span class="muted">${esc((t.condition_mesh || []).join('; '))}</span><br>
      <b>Interventions:</b> ${(t.interventions || []).map(i => `${esc(i.name)} <span class="muted">(${esc(i.type)})</span>`).join(', ')}<br>
      <b>Arms:</b> ${(t.arms || []).map(a => `${esc(a.label)} <span class="muted">[${esc(a.type)}: ${esc((a.interventions || []).join(', '))}]</span>`).join('; ')}<br><br>
      ${esc(t.brief_summary || '')}</div></div>
  </div>`;
  html += `<h3>Jev answers</h3>`;
  if (gate) {
    const a = gate.response.answers;
    html += `<div class="q"><div class="qi"><b>off_label</b> (noul) — ${esc(gate.request.questions.off_label.instructions)}</div>${pbar(a.off_label.noul)}</div>
             <div class="q"><div class="qi"><b>is_active_arm</b> (noul)</div>${pbar(a.is_active_arm.noul)}</div>`;
  } else html += `<p class="muted">Not gated yet.</p>`;
  if (judge) {
    const a = judge.response.answers;
    html += `<div class="q"><div class="qi"><b>plausibility</b> (score) → ${fmt(a.plausibility.score, 2)} · confidence ${fmt(a.plausibility.confidence)}</div>${dist(a.plausibility.probabilities, a.plausibility.legend)}</div>
             <div class="q"><div class="qi"><b>category</b> (choice) → ${esc(a.category.choice)} · confidence ${fmt(a.category.confidence)}</div>${dist(a.category.probabilities)}</div>
             <p class="muted small">model ${esc(judge.model)} · ${judge.input_tokens} input tokens · request hash ${judge.state_hash.slice(0, 12)}…</p>`;
  } else if (gate) html += `<p class="muted">Below gate threshold — no evidence pulled, no judge call.</p>`;
  html += `<h3>PubMed evidence (top ${d.evidence.length})</h3>`;
  html += d.evidence.length ? d.evidence.map(e => `<div class="abs"><div class="t"><a class="nct" href="https://pubmed.ncbi.nlm.nih.gov/${e.pmid}/" target="_blank" rel="noopener">PMID ${e.pmid} ↗</a> ${esc(e.title)}</div><div class="m">${esc(e.journal || '')} · ${esc(e.year || '')} · ${esc((e.pub_types || []).slice(0, 2).join(', '))}</div><p>${esc((e.abstract || '').slice(0, 900))}${(e.abstract || '').length > 900 ? '…' : ''}</p></div>`).join('') : '<p class="muted">none</p>';
  if (gate) html += `<details><summary>exact state JSON sent to Jev (gate)</summary><pre class="json">${esc(JSON.stringify(gate.request.state, null, 2))}</pre></details>`;
  if (judge) html += `<details><summary>exact state JSON sent to Jev (judge)</summary><pre class="json">${esc(JSON.stringify(judge.request.state, null, 2))}</pre></details>`;
  $('#dBody').innerHTML = html;
  $('#drawer').classList.remove('hidden'); $('#scrim').classList.remove('hidden');
}
function closeDrawer() { $('#drawer').classList.add('hidden'); $('#scrim').classList.add('hidden'); }

// ---- wiring ------------------------------------------------------------------
$('#runBtn').onclick = async () => {
  $('#runMsg').textContent = '';
  try {
    const drugs = $('#drugs').value.split(',').map(s => s.trim()).filter(Boolean);
    await api('/api/run', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ drugs, threshold: +$('#thr').value, max_trials: +$('#maxt').value, force: $('#force').checked }) });
    $('#runMsg').textContent = 'started'; wasRunning = true;
  } catch (e) { $('#runMsg').textContent = e.message; }
};
$('#runCondBtn').onclick = async () => {
  $('#runMsg').textContent = '';
  try {
    await api('/api/run_condition', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ condition: $('#cond').value.trim(), threshold: +$('#thr').value, max_trials: +$('#maxt').value, force: $('#force').checked }) });
    $('#runMsg').textContent = 'started'; wasRunning = true;
  } catch (e) { $('#runMsg').textContent = e.message; }
};
$('#cond').addEventListener('keydown', e => { if (e.key === 'Enter') $('#runCondBtn').click(); });
document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x === t));
  document.body.dataset.mode = t.dataset.mode;
  if (t.dataset.mode === 'condition' && !$('#maxt').dataset.touched) $('#maxt').value = 60;
});
$('#maxt').addEventListener('input', () => { $('#maxt').dataset.touched = 1; });
$('#fCond').onchange = () => { COND = CONDS.find(c => String(c.id) === $('#fCond').value) || null; loadRows(); };
['fDrug', 'fCat', 'fPhase', 'fSponsor', 'fPoff', 'fPl', 'fCombo', 'fAll'].forEach(id => $('#' + id).addEventListener('input', renderRows));
$('#rows').addEventListener('click', e => { const tr = e.target.closest('tr.r'); if (tr && !e.target.closest('a')) openDrawer(tr.dataset.id); });
$('#rows').addEventListener('keydown', e => { const tr = e.target.closest('tr.r'); if (tr && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openDrawer(tr.dataset.id); } });
$('#dClose').onclick = closeDrawer; $('#scrim').onclick = closeDrawer;
$('#csvBtn').onclick = () => { window.location.href = '/api/export.csv'; };
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });
poll(); loadConds();