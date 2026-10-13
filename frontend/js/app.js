// State, actions and event wiring. All authoritative work is an HTTP call; the browser only caches what the API returned.
import { api, get, post, put, ApiError, getUser, setUser } from './api.js';
import { esc, spin } from './util.js';
import * as V from './views.js';

const S = { config: null, sourceSchema: null, targetSchema: null, rules: [], source: { meta: null, records: [] }, plans: [], dry: {}, gate: {}, runs: [], target: [], recon: {}, audit: [], logs: [] };
const U = { tab: 'data', version: null, draft: null, fields: {}, busy: {}, loading: {}, agent: { mode: null, error: null }, confirm: null, runId: null, formErr: null, importErr: null,
  showAll: {}, diffs: {}, traces: {}, quarantine: {}, tests: null, user: '', boot: 'loading', bootErr: null };
const views = { data: V.viewData, plan: V.viewPlan, dry: V.viewDry, run: V.viewRun, recon: V.viewRecon, audit: V.viewAudit, docs: V.viewDocs };

/* ---------- rendering ---------- */
function toast(msg, kind) { const t = document.getElementById('toast'); t.textContent = msg; t.className = 'toast show ' + (kind || ''); clearTimeout(toast.t); toast.t = setTimeout(() => { t.className = 'toast'; }, 6000); }
function render() {
  const main = document.getElementById('main'); const y = window.scrollY;
  if (U.boot === 'loading') { main.innerHTML = `<div class="empty" style="padding-top:80px">${spin('Loading workbench…')}</div>`; return; }
  if (U.boot === 'error') { main.innerHTML = `<div class="empty" style="padding-top:80px"><b>Could not load the workbench</b>${esc(U.bootErr)}<div style="margin-top:12px"><button class="btn primary" data-act="retry_boot">Try again</button></div></div>`; return; }
  document.getElementById('nav').innerHTML = V.nav(S, U);
  const showConfirm = U.tab !== 'run' ? V.confirmBox(U) : '';
  main.innerHTML = `<div class="top">${V.topbar(S, U)}${V.ribbon(S, U)}</div>${showConfirm}${U.tab !== 'run' && U.tab !== 'recon' && U.tab !== 'audit' ? V.banner(U) : ''}${views[U.tab](S, U)}`;
  window.scrollTo(0, y);
}

/* ---------- error handling ---------- */
function fail(e) {
  if (!(e instanceof ApiError)) { console.error(e); e = new ApiError(0, 'client_error', 'Unexpected error in the page: ' + (e && e.message), null, null); }
  let hint = '';
  if (e.status === 401 || e.status === 403) hint = ' <span class="sm">Choose a demo identity in the header.</span>';
  if (e.code === 'validation_error' && Array.isArray(e.details)) hint = `<ul class="sm" style="margin:4px 0 0 18px">${e.details.slice(0, 5).map(d => `<li>${esc((d.loc || []).join('.'))}: ${esc(d.message)}</li>`).join('')}</ul>`;
  if (e.details && e.details.issues) hint += `<ul class="sm" style="margin:4px 0 0 18px">${e.details.issues.slice(0, 5).map(i => `<li>${esc(i.message)}</li>`).join('')}</ul>`;
  if (e.details && e.details.reasons) hint += ` <span class="sm">(${esc(e.details.reasons.join(', '))})</span>`;
  U.formErr = { message: e.message, code: e.code, requestId: e.requestId, hint };
  toast(e.message, 'bad');
  return e;
}
async function act(key, fn, okMsg) {
  U.busy[key] = true; U.formErr = null; render();
  try { const r = await fn(); if (okMsg) toast(typeof okMsg === 'function' ? okMsg(r) : okMsg); return { ok: true, r }; }
  catch (e) { return { ok: false, e: fail(e) }; }
  finally { U.busy[key] = false; render(); }
}

/* ---------- data loading (read models) ---------- */
const cur = () => V.curPlan(S, U);
async function loadPlans() { S.plans = await get('/plans'); if (!S.plans.find(p => p.version === U.version)) U.version = S.plans.length ? S.plans[S.plans.length - 1].version : null; }
async function loadRuns() { S.runs = await get('/runs'); }
async function loadTarget() { S.target = await get('/target'); }
async function loadSource() { const [meta, sample] = await Promise.all([get('/source'), get('/source/sample')]); S.source = { meta, records: sample.records }; }
async function loadDry(v) {
  if (v == null) return; const p = S.plans.find(x => x.version === v); if (!p || !p.dry_run) { delete S.dry[v]; return; }
  U.loading.dry = true; render();
  try { S.dry[v] = await get(`/plans/${v}/dry-run`); } catch (e) { if (e.status !== 404) fail(e); delete S.dry[v]; } finally { U.loading.dry = false; }
}
async function loadGate(v) { if (v == null) return; try { S.gate[v] = await get(`/plans/${v}/approval-gate`); } catch (e) { fail(e); } }
async function loadDiff(p) { if (p && p.parent && !U.diffs[p.version]) { try { U.diffs[p.version] = await get(`/plans/${p.parent}/diff/${p.version}`); } catch (e) { /* optional detail */ } } }
async function loadTrace(p) {
  if (!p || p.origin !== 'agent' || U.traces[p.version]) return;
  try { const logs = await get('/logs?limit=2000'); U.traces[p.version] = logs.filter(l => l.event === 'agent.trace' && l.fields.plan_version === p.version).sort((a, b) => a.id - b.id).map(l => l.fields); } catch (e) { /* optional detail */ }
}
async function loadAudit() { U.loading.audit = true; render(); try { [S.audit, S.logs] = await Promise.all([get('/audit?limit=300'), get('/logs?limit=300')]); } catch (e) { fail(e); } finally { U.loading.audit = false; } }
async function loadQuarantine(id) { if (!U.quarantine[id]) { try { U.quarantine[id] = await get(`/runs/${id}/quarantine`); } catch (e) { fail(e); } } }
async function refreshAll() { await Promise.all([loadPlans(), loadRuns(), loadTarget(), loadSource()]); S.dry = {}; S.gate = {}; U.diffs = {}; U.traces = {}; await enter(U.tab); }

async function enter(tab) {
  const p = cur();
  if (tab === 'plan' && p) { await Promise.all([loadDiff(p), loadTrace(p)]); }
  if (tab === 'dry' && p && !S.dry[p.version]) await loadDry(p.version);
  if (tab === 'run') { await Promise.all([p ? loadGate(p.version) : null, loadRuns(), loadTarget(), loadPlans()]); }
  if (tab === 'recon') { await Promise.all([loadRuns(), loadTarget()]); const r = S.runs.find(x => x.id === U.runId) || S.runs[0]; if (r) { U.runId = r.id; await loadQuarantine(r.id); if (!S.recon[r.id]) { try { S.recon[r.id] = await get(`/runs/${r.id}/reconciliation`); } catch (e) { if (e.status !== 404) fail(e); } } } }
  if (tab === 'audit') await loadAudit();
  render();
}

async function boot() {
  U.boot = 'loading'; U.bootErr = null; render();
  try {
    [S.config, S.sourceSchema, S.targetSchema, S.rules] = await Promise.all([get('/config'), get('/schema/source'), get('/schema/target'), get('/rules')]);
    await Promise.all([loadPlans(), loadRuns(), loadTarget(), loadSource()]);
    const saved = getUser(); U.user = S.config.demo_users.includes(saved) ? saved : S.config.demo_users[0] || ''; setUser(U.user);
    U.boot = 'ready';
    const p = cur(); if (p) { await loadDry(p.version); }
  } catch (e) { U.boot = 'error'; U.bootErr = e instanceof ApiError ? e.message + (e.requestId ? ` (request ${e.requestId})` : '') : String(e); }
  render();
}

/* ---------- actions ---------- */
const failAfter = () => { const v = (U.fields.fail_after || '').trim(); if (v === '') return { v: null }; const n = Number(v); return Number.isInteger(n) && n >= 0 ? { v: n } : { err: 'Simulated failure must be a whole number of 0 or more, or empty.' }; };

async function doAgent(mode, revise) {
  U.agent = { mode, error: null };
  const body = { mode }; if (mode === 'ai' && revise && cur()) body.base_version = cur().version;
  const r = await act('agent', () => post('/plans/propose', body), p => `Proposal saved as v${p.version}. Review the mapping, risks and questions before approving.`);
  if (r.ok) { U.agent = { mode: null, error: null }; U.version = r.r.version; U.draft = null; await loadPlans(); S.dry = {}; await loadDry(U.version); await enter('plan'); }
  else { U.agent = { mode, error: { code: r.e.code, message: r.e.message, details: r.e.details, requestId: r.e.requestId } }; U.formErr = null; render(); }
}
async function doDry() {
  const p = cur(); const r = await act('dry', () => post(`/plans/${p.version}/dry-run`), d => `Dry run of v${p.version}: ${d.counts.accepted} accepted, ${d.counts.rejected} quarantined. Nothing was written to the target.`);
  if (r.ok) { await loadPlans(); S.dry[p.version] = r.r; delete S.gate[p.version]; await loadGate(p.version); render(); }
}
async function saveDraft() {
  const p = cur(); const d = U.draft; if (!d) return;
  const mappings = Object.entries(d.rows).filter(([, r]) => r.rule).map(([target, r]) => ({ target, source: r.source || null, rule: r.rule, params: r.params || {} }));
  const r = await act('draft', () => post('/plans', { base_version: p.version, mappings }), n => `Saved as v${n.version}.`);
  if (r.ok) { U.draft = null; U.version = r.r.version; await loadPlans(); await enter('plan'); }
}
function defaultParams(rule, target) {
  const tf = S.targetSchema.fields.find(f => f.name === target) || {};
  return {
    split_name: { part: /^last/.test(target) ? 'last' : 'first' },
    parse_date: { format: 'DMY' },
    parse_datetime: { format: 'DMY' },
    decimal_to_minor: { decimals: 2 },
    normalize_phone: { default_region: 'IN' },
    map_enum: { map: {} },
    default_value: {
      policy: 'approved_default',
      value: tf.enum ? tf.enum[0] : tf.type === 'boolean' ? false : tf.type === 'integer' ? 0 : '',
      rationale: 'Reviewer-approved default'
    }
  }[rule] || {};
}
function startDraft(p) { if (!U.draft || U.draft.base !== p.version) U.draft = { base: p.version, rows: Object.fromEntries(p.mappings.map(m => [m.target, { source: m.source, rule: m.rule, params: JSON.parse(JSON.stringify(m.params || {})) }])) }; return U.draft; }
function draftChange(el) {
  const t = el.dataset.target, key = el.dataset.key; const d = startDraft(cur()); const row = d.rows[t] || (d.rows[t] = { source: null, rule: null, params: {} });
  const rules = Object.fromEntries(S.rules.map(r => [r.id, r]));
  if (key === 'rule') { if (!el.value) delete d.rows[t]; else { row.rule = el.value; row.params = defaultParams(el.value, t); if (!rules[el.value].uses_source) row.source = null; } }
  else if (key === 'source') row.source = el.value || null;
  else if (key.startsWith('param:')) {
    const k = key.slice(6); let v = el.value; const tf = S.targetSchema.fields.find(f => f.name === t);
    if (k === 'map') { const m = {}; v.split(',').forEach(pr => { const i = pr.indexOf('='); if (i > 0) m[pr.slice(0, i).trim().toLowerCase()] = pr.slice(i + 1).trim(); }); v = m; }
    else if (k === 'decimals') v = v === '' ? undefined : Number(v);
    else if (k === 'default_region') {
      v = (v || '').trim().toUpperCase();
      row.params.default_region = v || null;
      if (v) row.params.default_cc = v;
      else delete row.params.default_cc;
    }
    else if (k === 'policy') {
      row.params.policy = v;
      if (v === 'quarantine' || v === 'unresolved') {
        row.params.value = null;
      } else if (v === 'approved_default' && row.params.value == null) {
        row.params.value = tf.enum ? tf.enum[0] : tf.type === 'boolean' ? false : '';
      }
    }
    else if (k === 'value') {
      if (tf.type === 'boolean') v = v === 'true';
      else if (tf.type === 'integer') v = Number(v);
      row.params[k] = v;
    } else {
      row.params[k] = v;
    }
  }
  render();
}
async function doApprove() {
  const p = cur();
  const r = await act('approve', () => post(`/plans/${p.version}/approve`, { comment: (U.fields.comment || '').trim(), acknowledge_risks: !!U.fields.ack_risks, acknowledge_open_questions: !!U.fields.ack_questions }), a => `v${p.version} approved by ${a.by}.`);
  if (r.ok) { U.fields.ack_risks = U.fields.ack_questions = false; U.fields.comment = ''; await loadPlans(); await loadGate(p.version); render(); }
}
async function doReject() {
  const p = cur(); const c = (U.fields.comment || '').trim();
  if (!c) { U.formErr = { message: 'Enter a comment explaining why you are rejecting this version.' }; return render(); }
  const r = await act('approve', () => post(`/plans/${p.version}/reject`, { comment: c }), `v${p.version} rejected.`);
  if (r.ok) { U.fields.comment = ''; await loadPlans(); await loadGate(p.version); render(); }
}
async function doRevoke(v) { const r = await act('confirm', () => post(`/plans/${v}/revoke`), `Approval of v${v} revoked.`); U.confirm = null; if (r.ok) { await loadPlans(); await loadGate(v); } render(); }
async function doExecute() {
  const p = cur(); const fa = failAfter(); if (fa.err) { U.confirm = null; U.formErr = { message: fa.err }; return render(); }
  const r = await act('confirm', () => post('/runs', { plan_version: p.version, simulate_failure_after: fa.v }), run => run.status === 'failed' ? `Run ${run.id} stopped: ${run.attempts[run.attempts.length - 1].error.message}. Retry is safe.` : `Run ${run.id} finished: ${run.attempts[0].inserted} inserted, ${run.attempts[0].skipped_present} already present, ${run.attempts[0].conflicts} conflicts.`);
  U.confirm = null; await Promise.all([loadRuns(), loadTarget(), loadPlans()]); if (r.ok) { U.runId = r.r.id; delete S.recon[r.r.id]; } render();
}
async function doRetry(id) {
  const fa = failAfter(); if (fa.err) { U.formErr = { message: fa.err }; return render(); }
  const r = await act('run_' + id, () => post(`/runs/${id}/retry`, { simulate_failure_after: fa.v }), run => `Retry of ${id}: ${run.status}. ${run.attempts[run.attempts.length - 1].inserted} inserted, no duplicates created.`);
  await Promise.all([loadRuns(), loadTarget()]); delete S.recon[id]; render();
}
async function doRollback(id) {
  const r = await act('confirm', () => post(`/runs/${id}/rollback`), run => `Rolled back ${id}: ${run.rollbacks.find(x => x.status === 'completed').removed} rows removed. Pre-existing rows were not touched.`);
  U.confirm = null; await Promise.all([loadRuns(), loadTarget()]); delete S.recon[id]; render();
}
async function doReconcile(id) {
  const r = await act('recon', () => post(`/runs/${id}/reconciliation`), rep => rep.passed ? 'Reconciliation passed.' : 'Reconciliation found problems. See the failed checks.');
  if (r.ok) S.recon[id] = r.r; render();
}
async function doImport() {
  U.importErr = null; const txt = (U.fields.import_text || '').trim();
  if (!txt) { U.importErr = 'Paste a JSON array of source records first.'; return render(); }
  let arr; try { arr = JSON.parse(txt); } catch (e) { U.importErr = 'That is not valid JSON: ' + e.message; return render(); }
  if (!Array.isArray(arr)) { U.importErr = 'Expected a JSON array of objects.'; return render(); }
  const r = await act('import', () => put('/source', { records: arr, label: 'Imported by ' + (U.user || 'reviewer') }), s => `Loaded ${s.record_count} records.${s.unknown_fields.length ? ' Unknown fields kept but unused: ' + s.unknown_fields.join(', ') + '.' : ''}${s.approvals_invalidated.length ? ' Approvals invalidated for ' + s.approvals_invalidated.map(x => 'v' + x).join(', ') + '.' : ''}`);
  if (r.ok) { U.fields.import_text = ''; await refreshAll(); } else if (r.e.code === 'validation_error') { U.importErr = r.e.message + ': ' + (r.e.details || []).map(d => d.message).slice(0, 2).join('; '); U.formErr = null; render(); }
}

function onClick(a, d) {
  switch (a) {
    case 'nav': U.tab = d.tab; U.formErr = null; if (d.tab !== 'run') U.confirm = null; render(); window.scrollTo(0, 0); enter(d.tab); break;
    case 'retry_boot': boot(); break;
    case 'selver': U.version = Number(d.v); U.draft = null; U.formErr = null; render(); enter(U.tab); break;
    case 'agent': doAgent(d.mode, d.revise === '1'); break;
    case 'draft_save': saveDraft(); break;
    case 'draft_discard': U.draft = null; render(); break;
    case 'answer': { const p = cur(); const q = p.questions.find(x => x.id === d.q); const v = U.fields['ans_' + d.q] !== undefined ? U.fields['ans_' + d.q] : (q.answer || '');
      act('ans_' + d.q, () => post(`/plans/${p.version}/questions/${d.q}/answer`, { answer: v }), 'Answer saved.').then(async r => { if (r.ok) { delete U.fields['ans_' + d.q]; await loadPlans(); await loadGate(p.version); render(); } }); break; }
    case 'dry': doDry(); break;
    case 'approve': doApprove(); break;
    case 'reject': doReject(); break;
    case 'ask_revoke': U.confirm = { kind: 'revoke', title: 'Revoke approval?', body: 'This version can no longer be executed until it is approved again.', yes: 'Revoke approval', payload: cur().version, danger: true }; render(); break;
    case 'ask_execute': { const p = cur(); U.confirm = { kind: 'execute', title: `Execute v${p.version} into the mock target?`, body: 'The server re-verifies the approval, plan hash and source data first. It can be retried safely and rolled back.', yes: 'Execute' }; render(); break; }
    case 'ask_rollback': U.confirm = { kind: 'rollback', title: `Roll back ${d.run}?`, body: 'Removes only the rows this run inserted. Pre-existing rows and rows from other runs stay. The server refuses if the run ledger does not match the target.', yes: 'Roll back', payload: d.run, danger: true }; render(); break;
    case 'confirm_yes': { const c = U.confirm; if (!c) break; if (c.kind === 'execute') doExecute(); else if (c.kind === 'rollback') doRollback(c.payload); else if (c.kind === 'revoke') doRevoke(c.payload); break; }
    case 'confirm_no': U.confirm = null; render(); break;
    case 'retry': doRetry(d.run); break;
    case 'select_run': U.runId = d.run; U.tab = 'recon'; U.formErr = null; render(); window.scrollTo(0, 0); enter('recon'); break;
    case 'reconcile': doReconcile(d.run); break;
    case 'import': doImport(); break;
    case 'toggle_all': U.showAll[d.key] = !U.showAll[d.key]; render(); break;
    case 'reload_audit': loadAudit().then(render); break;
    case 'run_tests': act('tests', () => post('/tests/run'), t => (t.passed ? 'Tests passed: ' : 'Tests failed: ') + t.summary).then(r => { if (r.ok) U.tests = r.r; render(); }); break;
  }
}
document.addEventListener('click', e => { const el = e.target.closest('[data-act]'); if (el && !el.disabled) onClick(el.dataset.act, el.dataset); });
document.addEventListener('input', e => { const el = e.target; if (el.dataset && el.dataset.field) U.fields[el.dataset.field] = el.type === 'checkbox' ? el.checked : el.value; });
document.addEventListener('change', e => {
  const el = e.target;
  if (el.dataset.field && el.type === 'checkbox') { U.fields[el.dataset.field] = el.checked; return; }
  if (el.dataset.change === 'draft') draftChange(el);
  else if (el.dataset.change === 'qfilter') { U.fields.qfilter = el.value; render(); }
  else if (el.dataset.change === 'loglevel') { U.fields.logLevel = el.value; render(); }
  else if (el.dataset.change === 'user') { U.user = el.value; setUser(U.user); toast('Acting as ' + U.user); render(); }
});

boot();
