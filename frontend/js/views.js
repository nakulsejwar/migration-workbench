// Pure rendering: (state S, ui U) -> HTML string. Nothing here mutates state or talks to the server.
import { esc, num, when, money, chip, spin } from './util.js';

const curPlan = (S, U) => S.plans.find(p => p.version === U.version) || S.plans[S.plans.length - 1] || null;
export { curPlan };
const empty = (t, d, btn) => `<div class="card flat empty"><b>${esc(t)}</b>${esc(d)}${btn ? `<div style="margin-top:12px">${btn}</div>` : ''}</div>`;
const goBtn = (tab, label, cls) => `<button class="btn ${cls || ''}" data-act="nav" data-tab="${tab}">${esc(label)}</button>`;
const busy = (U, k) => !!U.busy[k];
const STATUS = { proposed: ['Proposed', 'src'], draft: ['Draft', ''], approved: ['Approved', 'ok'], approval_stale: ['Approval stale', 'warn'], approval_revoked: ['Approval revoked', 'warn'], rejected: ['Rejected', 'bad'] };
const statusChip = p => { const [t, k] = STATUS[p.status] || [p.status, '']; return chip(t, k); };
const ORIGIN = { agent: 'planning agent (LLM)', baseline: 'deterministic baseline', user: 'reviewer' };
const RUN_KIND = { running: 'warn', completed: 'ok', failed: 'bad', rolled_back: '', rolling_back: 'warn' };
const REASONS = { plan_changed: 'the plan changed', source_changed: 'the source data changed', dry_run_changed: 'the dry run no longer matches' };

export function banner(U) {
  if (U.formErr) return `<div class="banner bad" role="alert">${esc(U.formErr.message)}${U.formErr.requestId ? ` <span class="sm mono">request ${esc(U.formErr.requestId)}</span>` : ''}${U.formErr.hint || ''}</div>`;
  return '';
}

export function ribbon(S, U) {
  const p = curPlan(S, U); const d = p && p.dry_run;
  if (!d) return `<div class="ribbon empty"><div class="stats"><div class="stat" title="Total rows read from the source snapshot"><b>${num(S.source.meta ? S.source.meta.record_count : 0)}</b><span>source records</span></div><div class="stat" title="Pipeline stage: records where all transformations completed"><b>–</b><span>transformed</span></div><div class="stat a" title="Terminal state: passed all checks, ready for migration"><b>–</b><span>accepted</span></div><div class="stat q" title="Terminal state: rejected or held with evidence"><b>–</b><span>quarantined</span></div></div><div class="bar"></div><div class="cap">${p ? `No dry run yet for v${p.version}.` : 'No plan yet.'} Invariant: source = accepted + quarantined. Transformed is a stage metric.</div></div>`;
  const c = d.counts;
  const src = c.source_count !== undefined ? c.source_count : c.source;
  const tr = c.transformed_count !== undefined ? c.transformed_count : c.transformed;
  const acc = c.accepted_count !== undefined ? c.accepted_count : c.accepted;
  const qu = c.quarantined_count !== undefined ? c.quarantined_count : c.rejected;
  return `<div class="ribbon"><div class="stats">
    <div class="stat" title="source_count: Total records in the source snapshot"><b>${num(src)}</b><span>source records</span></div>
    <div class="stat" title="transformed_count: Pipeline stage metric — records that completed all transformations before target validation"><b>${num(tr)}</b><span>transformed</span></div>
    <div class="stat a" title="accepted_count: Terminal state — passed all transformations, business policies, and target validation"><b>${num(acc)}</b><span>accepted</span></div>
    <div class="stat q" title="quarantined_count: Terminal state — rejected or held in quarantine with field-level evidence"><b>${num(qu)}</b><span>quarantined</span></div>
  </div>
  <div class="bar" role="img" aria-label="${acc} accepted, ${qu} quarantined"><span class="seg ok" style="flex:${acc}"></span><span class="seg q" style="flex:${qu}"></span></div>
  <div class="cap">Dry run of v${p.version}, fingerprint <span class="mono">${esc(d.fingerprint.slice(0, 8))}</span>${d.stale ? ' · <b style="color:var(--warn)">source data has changed since; run again</b>' : ''} · <span title="source_count = accepted_count + quarantined_count">Invariant: ${src} = ${acc} + ${qu} (transformed is a stage metric)</span></div></div>`;
}

export function nav(S, U) {
  const p = curPlan(S, U); const d = p && p.dry_run;
  const done = { data: !!S.source.meta, plan: !!p, dry: !!d && !d.stale, run: S.runs.some(r => r.status === 'completed'), recon: Object.values(S.recon).some(r => r.passed) };
  const T = [['data', 1, 'Data'], ['plan', 2, 'Mapping plan'], ['dry', 3, 'Dry run'], ['run', 4, 'Approve and run'], ['recon', 5, 'Reconcile']];
  return T.map(([id, n, l]) => `<button data-act="nav" data-tab="${id}" ${U.tab === id ? 'aria-current="page"' : ''}><span class="n ${done[id] ? 'done' : ''}">${done[id] ? '✓' : n}</span>${l}</button>`).join('') + '<hr>' +
    `<button class="plain" data-act="nav" data-tab="audit" ${U.tab === 'audit' ? 'aria-current="page"' : ''}>History and logs</button><button class="plain" data-act="nav" data-tab="docs" ${U.tab === 'docs' ? 'aria-current="page"' : ''}>About and tests</button>`;
}

export function topbar(S, U) {
  const users = (S.config && S.config.demo_users) || [];
  return `<div class="toprow"><h1>Migration workbench <span class="flow"><span class="s">${esc(S.sourceSchema ? S.sourceSchema.name : '')}</span> → <span class="t">${esc(S.targetSchema ? S.targetSchema.name : '')}</span></span></h1>
   <div class="userbox"><label for="who">Demo identity</label><select id="who" data-change="user">${users.map(u => `<option ${U.user === u ? 'selected' : ''}>${esc(u)}</option>`).join('')}</select></div></div>`;
}

export function confirmBox(U) {
  const c = U.confirm; if (!c) return '';
  return `<div class="confirm ${c.danger ? 'danger' : ''}" role="alertdialog" aria-label="${esc(c.title)}"><h3>${esc(c.title)}</h3><p>${esc(c.body)}</p><div class="row"><button class="btn ${c.danger ? 'danger' : 'go'}" data-act="confirm_yes" ${busy(U, 'confirm') ? 'disabled' : ''}>${busy(U, 'confirm') ? spin('Working…') : esc(c.yes)}</button><button class="btn" data-act="confirm_no">Cancel</button></div></div>`;
}

/* ---------------- data ---------------- */
export function viewData(S, U) {
  const recs = S.source.records; const sf = S.sourceSchema.fields, tf = S.targetSchema.fields; const show = U.showAll.data ? recs : recs.slice(0, 12); const m = S.source.meta;
  return `<h2>Source and target</h2><p class="lede">One bounded dataset: ${esc(S.sourceSchema.name)} into ${esc(S.targetSchema.name)}. Limit: ${S.config.max_records} records. Loaded: ${num(recs.length)} (${esc(m.label)}, snapshot ${m.id}, <span class="mono">${esc(m.hash)}</span>).</p>
  <div class="grid2">
   <div class="card"><h3><span class="srccol">Source</span> ${esc(S.sourceSchema.name)}</h3><p class="sm mute">${esc(S.sourceSchema.description)}</p><div class="scroll"><table><tr><th>Field</th><th>Null</th><th>Notes</th></tr>${sf.map(f => `<tr><td class="srccol mono">${esc(f.name)}</td><td>${f.nullable ? 'yes' : 'no'}</td><td class="sm">${esc(f.description)}</td></tr>`).join('')}</table></div></div>
   <div class="card"><h3><span class="tgtcol">Target</span> ${esc(S.targetSchema.name)}</h3><p class="sm mute">${esc(S.targetSchema.description)} Key: <span class="mono">${esc(S.targetSchema.primary_key)}</span>.</p><div class="scroll"><table><tr><th>Field</th><th>Type</th><th>Rule</th></tr>${tf.map(f => `<tr><td class="tgtcol mono">${esc(f.name)}</td><td class="sm">${esc(f.type)}${f.required ? ' · required' : ''}</td><td class="sm">${esc(f.hint || (f.enum ? f.enum.slice(0, 5).join(', ') + (f.enum.length > 5 ? '…' : '') : f.description))}</td></tr>`).join('')}</table></div></div>
  </div>
  <div class="card"><h3>Supported transformation rules ${chip(S.rules.length)}</h3><p class="sm mute">These are the only rules the engine and the agent can use. No arbitrary code is ever executed.</p><div class="scroll"><table><tr><th>Rule</th><th>Settings</th><th>What it does</th></tr>${S.rules.map(r => `<tr><td class="mono">${esc(r.id)}</td><td class="sm mono">${esc(Object.keys(r.params).join(', ') || '—')}</td><td class="sm">${esc(r.description)}</td></tr>`).join('')}</table></div></div>
  <div class="card"><div class="row sp"><h3>Sample source records</h3><button class="btn sm" data-act="toggle_all" data-key="data">${U.showAll.data ? 'Show first 12' : 'Show all ' + recs.length}</button></div>
   <div class="scroll"><table><tr><th class="num">Row</th>${sf.map(f => `<th>${esc(f.name)}</th>`).join('')}</tr>${show.map((r, i) => `<tr><td class="num mute">${i + 1}</td>${sf.map(f => `<td class="mono">${r[f.name] == null ? '<span class="mute">null</span>' : esc(r[f.name])}</td>`).join('')}</tr>`).join('')}</table></div></div>
  <div class="card"><h3>Replace the source data</h3><p class="sm mute">Paste a JSON array (1 to ${S.config.max_records} objects) using the source field names. The server stores it as a new snapshot; earlier dry runs and approvals become stale.</p>
   ${U.importErr ? `<div class="banner bad" role="alert">${esc(U.importErr)}</div>` : ''}
   <div class="field"><label for="imp">Source records (JSON)</label><textarea id="imp" data-field="import_text" placeholder='[{"cust_id":"CUST-1","full_name":"Rao, Isha", ...}]'>${esc(U.fields.import_text || '')}</textarea></div>
   <button class="btn primary" data-act="import" ${busy(U, 'import') ? 'disabled' : ''}>${busy(U, 'import') ? spin('Uploading…') : 'Replace records'}</button></div>`;
}

/* ---------------- plan ---------------- */
function agentPanel(S, U, p) {
  const A = U.agent; const ai = S.config.llm_configured; const run = busy(U, 'agent');
  return `<div class="card"><h3>Planning agent</h3>
   <p class="sm mute">The agent can only read the schemas, list supported rules, profile or sample source values and test-run candidate mappings through the deterministic engine. It cannot approve, execute or write to the target. Everything it returns is validated; unsupported rules are discarded and recorded.</p>
   <div class="row">
    <button class="btn primary" data-act="agent" data-mode="ai" data-revise="${p ? 1 : 0}" ${!ai || run ? 'disabled' : ''}>${run && A.mode === 'ai' ? spin('Agent working…') : (p ? `Ask the agent to revise v${p.version}` : 'Ask the agent for a proposal')}</button>
    ${p ? `<button class="btn" data-act="agent" data-mode="ai" data-revise="0" ${!ai || run ? 'disabled' : ''}>Fresh AI proposal</button>` : ''}
    <button class="btn" data-act="agent" data-mode="baseline" ${run ? 'disabled' : ''}>${run && A.mode === 'baseline' ? spin('Building…') : 'Baseline proposal (no AI)'}</button>
   </div>
   ${ai ? `<p class="sm mute" style="margin-top:8px">Model: <span class="mono">${esc(S.config.primary_model)}</span>${S.config.fallback_models.length ? ' with fallback ' + esc(S.config.fallback_models.join(', ')) : ''}. A run can take 30 to 90 seconds. ${p && p.questions.some(q => q.answer) ? 'Your answers to the questions below are sent with a revision.' : ''}</p>` : `<p class="sm mute" style="margin-top:8px">No LLM is configured on the server (OPENROUTER_API_KEY is not set), so only the deterministic baseline is available.</p>`}
   ${A.error ? `<div class="banner bad" role="alert" style="margin-top:12px">
     <b>${A.error.details && (A.error.details.provider_status === 429 || String(A.error.details.provider_message).includes('Rate limit') || String(A.error.details.provider_message).includes('free-models-per-day')) ? 'OpenRouter daily free request limit reached (50/50 used today).' : 'The agent did not produce a plan.'}</b> ${esc(A.error.message)} <span class="sm mono">${esc(A.error.code)}${A.error.requestId ? ' · request ' + esc(A.error.requestId) : ''}</span>
     ${A.error.details && A.error.details.provider_message ? `<div class="sm" style="margin-top:6px;background:rgba(0,0,0,0.04);padding:6px;border-radius:4px"><b>Provider message:</b> ${esc(A.error.details.provider_message)}</div>` : ''}
     ${A.error.details && A.error.details.dropped ? `<ul class="sm">${A.error.details.dropped.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
     ${A.error.details && A.error.details.issues ? `<ul class="sm">${A.error.details.issues.map(x => `<li>${esc(x.message)}</li>`).join('')}</ul>` : ''}
     <div style="margin-top:10px"><button class="btn sm primary" data-act="agent" data-mode="baseline" ${run ? 'disabled' : ''}>Use the baseline proposal instead (instant, no AI quota)</button></div>
   </div>` : ''}
  </div>`;
}

function mappingEditor(S, U, p) {
  const d = U.draft && U.draft.base === p.version ? U.draft : null;
  const rows = d ? d.rows : Object.fromEntries(p.mappings.map(m => [m.target, m]));
  const rules = Object.fromEntries(S.rules.map(r => [r.id, r]));
  const srcOpts = sel => `<option value="">—</option>` + S.sourceSchema.fields.map(f => `<option value="${esc(f.name)}" ${sel === f.name ? 'selected' : ''}>${esc(f.name)}</option>`).join('');
  const ruleOpts = sel => `<option value="">not mapped</option>` + S.rules.map(r => `<option value="${esc(r.id)}" ${sel === r.id ? 'selected' : ''}>${esc(r.label)}</option>`).join('');
  const sel = (t, k, val, opts) => `<select aria-label="${esc(k)} for ${esc(t)}" data-change="draft" data-target="${esc(t)}" data-key="${esc(k)}">${opts.map(o => `<option value="${esc(o)}" ${String(val) === String(o) ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>`;
  const params = (t, r) => {
    const pr = r.params || {}; const tf = S.targetSchema.fields.find(f => f.name === t);
    switch (r.rule) {
      case 'split_name': return sel(t, 'param:part', pr.part, ['first', 'last']);
      case 'parse_date': case 'parse_datetime': return sel(t, 'param:format', pr.format, ['DMY', 'MDY', 'YMD']);
      case 'decimal_to_minor': return `<input type="number" min="0" max="6" aria-label="decimals for ${esc(t)}" value="${esc(pr.decimals === undefined ? '' : pr.decimals)}" data-change="draft" data-target="${esc(t)}" data-key="param:decimals">`;
      case 'normalize_phone': {
        const curRegion = pr.default_region !== undefined ? pr.default_region : (pr.default_cc || '');
        return `<div class="row sm" style="gap:4px">
          <input type="text" style="width:120px" aria-label="default country region for ${esc(t)}" value="${esc(curRegion || '')}" placeholder="IN, US (or empty)" data-change="draft" data-target="${esc(t)}" data-key="param:default_region">
          ${curRegion ? chip('Region: ' + curRegion, 'ok') : chip('Unresolved', 'warn')}
        </div>`;
      }
      case 'map_enum': return `<input type="text" aria-label="value map for ${esc(t)}" value="${esc(Object.entries(pr.map || {}).map(([k, v]) => k + '=' + v).join(', '))}" placeholder="a=active, i=inactive" data-change="draft" data-target="${esc(t)}" data-key="param:map">`;
      case 'default_value': {
        const pol = pr.policy || 'approved_default';
        const polSel = `<select style="margin-bottom:4px" aria-label="business policy for ${esc(t)}" data-change="draft" data-target="${esc(t)}" data-key="param:policy">
          <option value="approved_default" ${pol === 'approved_default' ? 'selected' : ''}>Approved business default</option>
          <option value="quarantine" ${pol === 'quarantine' ? 'selected' : ''}>Quarantine pending decision</option>
          <option value="unresolved" ${pol === 'unresolved' ? 'selected' : ''}>Unresolved (blocks approval)</option>
        </select>`;
        if (pol === 'quarantine') return polSel + `<div class="sm mute">Quarantined by policy</div>`;
        if (pol === 'unresolved') return polSel + `<div class="sm" style="color:var(--bad)">Unresolved decision</div>`;
        if (tf.enum) return polSel + sel(t, 'param:value', pr.value, tf.enum);
        if (tf.type === 'boolean') return polSel + sel(t, 'param:value', String(pr.value), ['true', 'false']);
        return polSel + `<input type="text" aria-label="constant for ${esc(t)}" value="${esc(pr.value === undefined ? '' : pr.value)}" data-change="draft" data-target="${esc(t)}" data-key="param:value">`;
      }
      default: return '<span class="mute">none</span>';
    }
  };
  const bad = new Set(d ? [] : p.validation.issues.filter(i => i.level === 'error' && i.target).map(i => i.target));
  return `<div class="scroll"><table><tr><th>Target field</th><th>Source field</th><th>Rule</th><th>Rule settings</th><th>Confidence</th></tr>
   ${S.targetSchema.fields.map(f => { const r = rows[f.name]; const m = p.mappings.find(x => x.target === f.name);
     return `<tr><td><span class="tgtcol mono">${esc(f.name)}</span>${f.required ? ' <span class="mute sm">required</span>' : ''}${bad.has(f.name) ? ' ' + chip('needs fix', 'bad') : ''}</td>
     <td>${r && rules[r.rule] && !rules[r.rule].uses_source ? '<span class="mute">no source</span>' : `<select aria-label="source for ${esc(f.name)}" data-change="draft" data-target="${esc(f.name)}" data-key="source">${srcOpts(r && r.source)}</select>`}</td>
     <td><select aria-label="rule for ${esc(f.name)}" data-change="draft" data-target="${esc(f.name)}" data-key="rule">${ruleOpts(r && r.rule)}</select></td>
     <td>${r && r.rule ? params(f.name, r) : ''}</td>
     <td class="sm">${!d && m && m.confidence != null ? `<b>${Math.round(m.confidence * 100)}%</b><div class="mute">${esc(m.rationale || '')}</div>` : ''}</td></tr>`; }).join('')}</table></div>
   ${!d && p.validation.issues.some(i => i.level === 'error') ? `<div class="banner bad" style="margin-top:12px" role="alert"><b>This version has errors:</b><ul style="margin:4px 0 0 18px;padding:0">${p.validation.issues.filter(i => i.level === 'error').map(i => `<li>${esc(i.message)}</li>`).join('')}</ul></div>` : ''}`;
}

export function viewPlan(S, U) {
  const p = curPlan(S, U);
  let h = `<h2>Mapping plan</h2><p class="lede">Every version is immutable once saved, including at the database level. Editing a mapping creates a new version that needs its own dry run and approval.</p>${agentPanel(S, U, p)}`;
  if (!p) return h + empty('No plan yet', 'Ask the agent for a proposal, or build the baseline proposal to start from.');
  const warns = p.validation.issues.filter(i => i.level === 'warn'); const inc = p.incompatibilities || [];
  const diff = U.diffs[p.version]; const trace = U.traces[p.version] || [];
  const drafting = U.draft && U.draft.base === p.version;
  h += `<div class="card"><div class="row sp"><h3>Version ${p.version} ${statusChip(p)}</h3><div class="verlist" role="group" aria-label="Plan versions">${S.plans.map(x => `<button class="ver" aria-pressed="${x.version === p.version}" data-act="selver" data-v="${x.version}">v${x.version}</button>`).join('')}</div></div>
   <dl class="kv"><dt>Created</dt><dd>${esc(when(p.created_at))} by ${esc(p.created_by)} (${esc(ORIGIN[p.origin] || p.origin)})${p.model_used ? ` · model <span class="mono">${esc(p.model_used)}</span>` : ''}</dd><dt>Based on</dt><dd>${p.parent ? 'v' + p.parent : 'nothing (first proposal)'}</dd><dt>Content hash</dt><dd class="mono">${esc(p.hash)}</dd>
   ${p.approval && p.approval.decision ? `<dt>Decision</dt><dd>${esc(p.approval.decision)} by ${esc(p.approval.by)}, ${esc(when(p.approval.at))}${p.approval.reasons && p.approval.reasons.length ? ` · stale because ${esc(p.approval.reasons.map(r => REASONS[r] || r).join(', '))}` : ''}</dd>` : ''}</dl>
   ${p.summary ? `<p style="margin-top:10px">${esc(p.summary)}</p>` : ''}
   ${p.dropped && p.dropped.length ? `<div class="banner warn"><b>Parts of the model's answer were rejected</b> because they used something outside the supported catalogue:<ul style="margin:4px 0 0 18px;padding:0">${p.dropped.map(x => `<li>${esc(x)}</li>`).join('')}</ul></div>` : ''}
   ${p.status === 'approved' ? `<div class="banner info">This version is approved. Saving changes creates a new version that must be approved again.</div>` : ''}
   ${diff && diff.changes.length ? `<details style="margin-bottom:10px"><summary>${diff.changes.length} change(s) from v${diff.from}</summary><ul class="sm">${diff.changes.map(x => `<li><b>${esc(x.target)}</b>: <span class="mono">${esc(x.from)}</span> → <span class="mono">${esc(x.to)}</span></li>`).join('')}</ul></details>` : ''}
   ${mappingEditor(S, U, p)}
   ${drafting ? `<div class="row" style="margin-top:12px"><button class="btn primary" data-act="draft_save" ${busy(U, 'draft') ? 'disabled' : ''}>${busy(U, 'draft') ? spin('Saving…') : 'Save as new version'}</button><button class="btn" data-act="draft_discard">Discard edits</button><span class="sm mute">The server validates the mapping when you save and dry-run it.</span></div>` : ''}
  </div>`;
  if (trace.length) h += `<div class="card"><h3>Agent tool calls ${chip(trace.length)}</h3><ul class="steps">${trace.map(t => `<li><span class="mono">${esc(t.tool)}</span> ${chip(t.outcome, t.outcome === 'ok' ? 'ok' : 'warn')} ${esc(t.summary || '')}</li>`).join('')}</ul></div>`;
  h += `<div class="grid2"><div class="card"><h3>Missing, incompatible or unused fields</h3>${inc.length || warns.length ? `<ul class="sm" style="margin:0 0 0 18px;padding:0">${inc.map(i => `<li>${chip(i.kind.replace(/_/g, ' '), i.kind === 'missing_in_source' ? 'bad' : 'warn')} ${i.field ? `<span class="mono">${esc(i.field)}</span> ` : ''}${esc(i.text)}</li>`).join('')}${warns.filter(w => w.code === 'source_unused' && !inc.some(i => i.field === w.field)).map(w => `<li>${chip('not carried over', 'warn')} ${esc(w.message)}</li>`).join('')}</ul>` : '<p class="mute sm">Nothing flagged.</p>'}</div>
   <div class="card"><h3>Mapping risks</h3>${p.risks.length ? `<ul class="sm" style="margin:0 0 0 18px;padding:0">${p.risks.map(r => `<li>${chip(r.level, r.level === 'high' ? 'bad' : r.level === 'medium' ? 'warn' : '')} ${r.field ? `<span class="mono">${esc(r.field)}</span> ` : ''}${esc(r.text)}</li>`).join('')}</ul>` : '<p class="mute sm">No risks recorded.</p>'}</div></div>`;
  h += `<div class="card"><h3>Clarification questions ${chip(p.questions.filter(q => !q.answer).length + ' open', p.questions.some(q => !q.answer) ? 'warn' : 'ok')}</h3>
   ${p.questions.length ? p.questions.map(q => `<div class="field" style="border-bottom:1px solid var(--line);padding-bottom:10px"><label for="q-${esc(q.id)}">${q.field ? `<span class="mono">${esc(q.field)}</span>: ` : ''}${esc(q.text)}</label>${q.assumption ? `<span class="sm mute">Assumed meanwhile: ${esc(q.assumption)}</span>` : ''}
     <div class="row"><input type="text" id="q-${esc(q.id)}" style="flex:1;min-width:200px" data-field="ans_${esc(q.id)}" value="${esc(U.fields['ans_' + q.id] !== undefined ? U.fields['ans_' + q.id] : (q.answer || ''))}" placeholder="Your answer" maxlength="500"><button class="btn sm" data-act="answer" data-q="${esc(q.id)}" ${busy(U, 'ans_' + q.id) ? 'disabled' : ''}>Save answer</button></div></div>`).join('') : '<p class="mute sm">No open questions.</p>'}
   <p class="sm mute">Answers are notes stored beside this version; they do not change the mapping. To apply an answer, edit the mapping or ask the agent to revise.</p></div>`;
  return h;
}

/* ---------------- dry run ---------------- */
function errBlock(e) {
  return `<div class="err"><span class="mono tgtcol">${esc(e.field)}</span> ${chip(e.code, 'bad')} <span class="mute sm">${esc(e.stage)}</span><div class="m">${esc(e.message)}</div>${e.rule ? `<div class="sm mute">${esc(e.rule)}(<span class="srccol mono">${esc(e.source || '—')}</span>)${e.source_value != null ? ` from <span class="ev">${esc(JSON.stringify(e.source_value))}</span>` : ''}${e.value != null && e.stage === 'validate' ? ` produced <span class="ev">${esc(JSON.stringify(e.value))}</span>` : ''}</div>` : ''}</div>`;
}
export { errBlock };

export function viewDry(S, U) {
  const p = curPlan(S, U); if (!p) return `<h2>Dry run</h2>` + empty('No plan to test', 'Create a mapping plan first.', goBtn('plan', 'Go to mapping plan', 'primary'));
  const d = S.dry[p.version]; const hasErr = p.validation.issues.some(i => i.level === 'error');
  let h = `<h2>Dry run of v${p.version}</h2><p class="lede">The dry run is computed by the server's deterministic engine, never by the LLM. It applies the plan to every source record and writes nothing to the target. Identical inputs always give the same fingerprint.</p>
   <div class="row" style="margin-bottom:14px"><button class="btn primary" data-act="dry" ${hasErr || busy(U, 'dry') ? 'disabled' : ''}>${busy(U, 'dry') ? spin('Running…') : (d ? 'Run again' : 'Run dry run')}</button>${hasErr ? `<span class="sm" style="color:var(--bad)">The mapping has errors. Fix them on the plan page.</span>` : ''}</div>`;
  if (U.loading.dry) return h + `<div class="empty">${spin('Loading dry run…')}</div>`;
  if (!d) return h + empty('No dry run for this version yet', 'Run it to see transformed, accepted and quarantined counts with field-level evidence.');
  const c = d.counts;
  h += `${d.stale ? `<div class="banner warn" role="alert"><b>Out of date.</b> The source data changed after this dry run. Run it again before approving.</div>` : ''}
   <div class="card"><div class="grid2" style="grid-template-columns:repeat(4,minmax(0,1fr))">
   ${[
     ['Source records', c.source_count !== undefined ? c.source_count : c.source, 'source_count: rows read from snapshot', 'source_count: Number of records in the source snapshot'],
     ['Transformed', c.transformed_count !== undefined ? c.transformed_count : c.transformed, 'transformed_count: pipeline stage (rules ran without error)', 'transformed_count: Pipeline-stage metric — records that completed all transformations before target validation. This is NOT a terminal category and must never be added to accepted/quarantined.'],
     ['Accepted', c.accepted_count !== undefined ? c.accepted_count : c.accepted, 'accepted_count: terminal state (valid & unique key)', 'accepted_count: Terminal state — source records that passed all transformations, business-policy checks, and target schema validation; eligible for migration.'],
     ['Quarantined', c.quarantined_count !== undefined ? c.quarantined_count : c.rejected, 'quarantined_count: terminal state (rejected/held with evidence)', 'quarantined_count: Terminal state — source records rejected or held for manual review; cannot currently be migrated.']
   ].map(([l, n, s, tip]) => `<div title="${esc(tip)}"><div class="lbl">${l}</div><div style="font-size:28px;font-weight:650;font-variant-numeric:tabular-nums">${num(n)}</div><div class="sm mute">${s}</div></div>`).join('')}</div>
   <p class="sm mute" style="margin:12px 0 0">
     <b>Count Semantics &amp; Invariants:</b> <code>source_count = accepted_count + quarantined_count</code> (${c.source} = ${c.accepted} + ${c.rejected}). Transformed (${c.transformed}) is a pipeline-stage metric, not an additional terminal category.<br>
     Count check: ${c.source === c.accepted + c.rejected ? '<span style="color:var(--ok)">✓ Invariant verified</span>' : '<span style="color:var(--bad)">✗ Invariant failed</span>'}. ${d.verified ? 'Computed deterministically on the server with fingerprint' : 'Internal run fingerprint'} <span class="mono">${esc(d.fingerprint)}</span>${d.identical_to_previous ? ' · identical to previous dry run with same inputs' : ''}.
   </p></div>`;
  if (d.target_preview) h += `<div class="card"><h3>If executed now</h3><p class="sm" style="margin:0">Against the current mock target: <b>${d.target_preview.insert}</b> would be inserted, <b>${d.target_preview.skip}</b> are identical and would be skipped, <b>${d.target_preview.conflict}</b> share a key with a different record and would be reported as conflicts.</p></div>`;
  h += `<div class="card"><h3>Errors by field</h3>${d.error_summary.length ? `<div class="scroll"><table><tr><th>Target field</th><th>Error</th><th class="num">Records</th></tr>${d.error_summary.map(e => `<tr><td class="tgtcol mono">${esc(e.field)}</td><td>${chip(e.code, 'bad')}</td><td class="num">${e.count}</td></tr>`).join('')}</table></div>` : '<p class="mute sm">No errors. Every record was accepted.</p>'}</div>`;
  const f = (U.fields.qfilter || '').trim().toLowerCase(); const q = d.quarantine || [];
  const rej = q.filter(r => !f || JSON.stringify(r).toLowerCase().includes(f));
  h += `<div class="card"><div class="row sp"><h3>Quarantine ${chip(q.length)}</h3><input type="text" style="max-width:260px" aria-label="Filter quarantine" placeholder="Filter by field, code or value" data-field="qfilter" data-change="qfilter" value="${esc(U.fields.qfilter || '')}"></div>
   ${rej.length ? `<div class="scroll"><table><tr><th class="num">Row</th><th>Source key</th><th>Field-level evidence</th></tr>${rej.map(r => `<tr><td class="num">${r.row}</td><td class="mono">${esc(r.source_key == null ? 'null' : r.source_key)}</td><td>${r.errors.map(errBlock).join('')}</td></tr>`).join('')}</table></div>` : `<p class="mute sm">${q.length ? 'Nothing matches that filter.' : 'Nothing was quarantined.'}</p>`}</div>`;
  if (d.accepted_preview && d.accepted_preview.length) { const cols = S.targetSchema.fields.map(f => f.name);
    h += `<div class="card"><h3>Accepted records, as they will be written ${chip('first ' + d.accepted_preview.length + ' of ' + c.accepted)}</h3><div class="scroll"><table><tr>${cols.map(k => `<th>${esc(k)}</th>`).join('')}</tr>${d.accepted_preview.map(a => `<tr>${cols.map(k => `<td class="mono">${a.record[k] == null ? '<span class="mute">null</span>' : esc(k === 'balance_minor' ? a.record[k] + ' (' + money(a.record[k]) + ')' : a.record[k])}</td>`).join('')}</tr>`).join('')}</table></div></div>`; }
  return h;
}

/* ---------------- approve and run ---------------- */
export function viewRun(S, U) {
  const p = curPlan(S, U); let h = `<h2>Approve and run</h2><p class="lede">The server decides. It will not execute an unapproved, stale or invalid plan whatever this page sends, and it re-verifies the approval against the plan hash, the source snapshot and a recomputed dry run.</p>`;
  if (!p) return h + empty('No plan to approve', 'Create a mapping plan and dry-run it first.', goBtn('plan', 'Go to mapping plan', 'primary'));
  const g = S.gate[p.version]; const a = p.approval; const open = p.questions.filter(q => !q.answer).length;
  const valid = a && a.status === 'valid';
  h += `${banner(U)}${confirmBox(U)}
  <div class="card"><div class="row sp"><h3>Decision on v${p.version} ${statusChip(p)}</h3><div class="verlist">${S.plans.map(x => `<button class="ver" aria-pressed="${x.version === p.version}" data-act="selver" data-v="${x.version}">v${x.version}</button>`).join('')}</div></div>
   <p class="sm mute">Acting as <b>${esc(U.user || 'nobody')}</b> (demo identity: the server records this as the approver; it is not authentication).</p>
   ${g ? `<ul class="checks">${g.checks.map(c => `<li><span class="dot ${c.ok ? 'ok' : 'no'}">${c.ok ? '✓' : '×'}</span><div>${esc(c.label)}</div></li>`).join('')}<li><span class="dot ${open ? 'wn' : 'ok'}">${open ? '!' : '✓'}</span><div>${open ? `${open} clarification question(s) unanswered` : 'All clarification questions answered'}</div></li></ul>` : `<p class="mute">${spin('Loading checks…')}</p>`}
   ${a && a.status === 'stale' ? `<div class="banner warn" role="alert"><b>Approval is stale</b> (${esc((a.reasons || []).map(r => REASONS[r] || r).join(', '))}). It cannot be used to execute. Dry-run and approve again.</div>` : ''}
   ${a && a.status === 'rejected' ? `<div class="banner bad">Rejected by ${esc(a.by)} on ${esc(when(a.at))}: “${esc(a.comment)}”</div>` : ''}
   ${valid ? `<div class="banner ok"><b>Approved by ${esc(a.by)}</b> on ${esc(when(a.at))}.${a.comment ? ' “' + esc(a.comment) + '”' : ''}<div style="margin-top:8px"><button class="btn sm" data-act="ask_revoke">Revoke approval</button></div></div>` :
   `<div style="margin-top:14px">
     <div class="field"><label for="cmt">Comment (required to reject, optional to approve)</label><input type="text" id="cmt" maxlength="500" data-field="comment" value="${esc(U.fields.comment || '')}"></div>
     <label class="row sm" style="margin-bottom:6px"><input type="checkbox" data-field="ack_risks" ${U.fields.ack_risks ? 'checked' : ''}> I reviewed the mapping, risks and fields that will be dropped.</label>
     ${open ? `<label class="row sm" style="margin-bottom:6px"><input type="checkbox" data-field="ack_questions" ${U.fields.ack_questions ? 'checked' : ''}> I accept the agent's assumptions for the ${open} unanswered question(s).</label>` : ''}
     <div class="row"><button class="btn go" data-act="approve" ${busy(U, 'approve') || (g && !g.can_approve) ? 'disabled' : ''}>${busy(U, 'approve') ? spin('Approving…') : `Approve v${p.version}`}</button><button class="btn danger" data-act="reject" ${busy(U, 'approve') ? 'disabled' : ''}>Reject v${p.version}</button></div></div>`}
  </div>`;
  h += `<div class="card"><h3>Execute into the mock target</h3>
   ${valid ? `<p class="sm">Executes the approved plan against the persisted mock target table (${S.target.length} rows now). Quarantined records stay out; conflicting keys are reported, never overwritten.</p>` : `<p class="sm mute">Not available: v${p.version} has no valid approval.</p>`}
   <div class="field" style="max-width:380px"><label for="fa">Simulate a target outage after N inserts (testing aid)</label><input type="number" min="0" max="${S.config.max_records}" id="fa" data-field="fail_after" value="${esc(U.fields.fail_after || '')}" placeholder="leave empty for a normal run"><span class="sm mute">Watch a failed run recover through retry without duplicates.</span></div>
   <button class="btn go" data-act="ask_execute" ${valid && !busy(U, 'execute') ? '' : 'disabled'}>Execute v${p.version}</button></div>`;
  h += `<h3 style="margin:22px 0 10px">Runs</h3>` + (S.runs.length ? S.runs.map(r => `<div class="card"><div class="row sp"><h3>${esc(r.id)} · v${r.plan_version} ${chip(r.status.replace('_', ' '), RUN_KIND[r.status])}</h3><div class="row">
      ${r.status === 'failed' || r.status === 'completed' ? `<button class="btn sm ${r.status === 'failed' ? 'go' : ''}" data-act="retry" data-run="${esc(r.id)}" ${busy(U, 'run_' + r.id) ? 'disabled' : ''}>${r.status === 'failed' ? 'Retry' : 'Verify (retry)'}</button>` : ''}${r.status === 'completed' || r.status === 'failed' ? `<button class="btn sm danger" data-act="ask_rollback" data-run="${esc(r.id)}" ${busy(U, 'run_' + r.id) ? 'disabled' : ''}>Roll back</button>` : ''}<button class="btn sm" data-act="select_run" data-run="${esc(r.id)}">Reconcile</button></div></div>
     <p class="sm mute">Started ${esc(when(r.created_at))} by ${esc(r.created_by)}. Target had ${r.pre_count} rows before. ${r.inserted_total} inserted in total; checkpoint ${r.checkpoint} of ${r.counts.accepted} accepted rows${r.rollbacks.some(x => x.status === 'completed') ? '; rolled back' : ''}.</p>
     <div class="scroll"><table><tr><th class="num">#</th><th>Kind</th><th>When</th><th class="num">From row</th><th class="num">Inserted</th><th class="num">Already applied</th><th class="num">Already present</th><th class="num">Conflicts</th><th class="num">Not attempted</th><th>Outcome</th></tr>
     ${r.attempts.map(a => `<tr><td class="num">${a.n}</td><td>${esc(a.kind)}</td><td class="sm">${esc(when(a.started_at))}</td><td class="num">${a.from_checkpoint}</td><td class="num">${a.inserted}</td><td class="num">${a.skipped_own}</td><td class="num">${a.skipped_present}</td><td class="num">${a.conflicts}</td><td class="num">${a.not_attempted}</td><td>${a.error ? chip(a.error.message, 'bad') : chip(a.status, 'ok')}</td></tr>`).join('')}</table></div>
     ${r.conflicts.length ? `<details style="margin-top:8px"><summary>${r.conflicts.length} key conflict(s), left untouched</summary><ul class="sm">${r.conflicts.map(c => `<li><span class="mono">${esc(c.key)}</span> (row ${c.row}) differs from the existing target row in: ${esc(c.diff_fields.join(', '))}</li>`).join('')}</ul></details>` : ''}
     ${r.rollbacks.length ? `<details style="margin-top:8px"><summary>${r.rollbacks.length} rollback request(s)</summary><ul class="sm">${r.rollbacks.map(x => `<li>${esc(when(x.at))} by ${esc(x.by)}: ${esc(x.status)}${x.status === 'completed' ? ', removed ' + x.removed : ''}${x.detail && x.detail.reason ? ' (' + esc(x.detail.reason) + ')' : ''}</li>`).join('')}</ul></details>` : ''}</div>`).join('') : empty('No runs yet', 'Approve a version and execute it to create the first run.'));
  return h;
}

/* ---------------- reconcile ---------------- */
export function viewRecon(S, U) {
  let h = `<h2>Reconcile</h2><p class="lede">Independent checks computed by the server from the persisted target table, the run ledger and a recomputed dry run of the run's own source snapshot.</p>${banner(U)}`;
  if (!S.runs.length) return h + empty('Nothing to reconcile yet', 'Execute an approved plan to create a run.', goBtn('run', 'Go to approve and run', 'primary'));
  const run = S.runs.find(r => r.id === U.runId) || S.runs[0]; const rc = S.recon[run.id];
  h += `<div class="row" style="margin-bottom:14px"><span class="lbl">Run</span><div class="verlist">${S.runs.map(r => `<button class="ver" aria-pressed="${r.id === run.id}" data-act="select_run" data-run="${esc(r.id)}">${esc(r.id)}</button>`).join('')}</div>${chip(run.status.replace('_', ' '), RUN_KIND[run.status])}<button class="btn sm" data-act="reconcile" data-run="${esc(run.id)}" ${busy(U, 'recon') ? 'disabled' : ''}>${busy(U, 'recon') ? spin('Computing…') : 'Run reconciliation'}</button></div>`;
  if (!rc) return h + (busy(U, 'recon') ? `<div class="empty">${spin('Reconciling…')}</div>` : empty('No report yet for ' + run.id, 'Run the reconciliation to compute the checks.'));
  const t = rc.totals;
  h += `<div class="banner ${rc.passed ? 'ok' : 'bad'}" role="status"><b>${rc.passed ? (run.status === 'rolled_back' ? 'Rollback verified.' : 'Reconciled.') : 'Reconciliation failed.'}</b> ${rc.passed ? 'All checks pass for ' + esc(run.id) + '.' : rc.checks.filter(c => !c.ok).length + ' check(s) need attention.'} <span class="sm">Report ${rc.id}, ${esc(when(rc.created_at))}.</span></div>
  <div class="grid2"><div class="card"><h3>Totals</h3><div class="scroll"><table>
   ${[['Source records', t.source_rows], ['Transformed', t.transformed], ['Accepted', t.accepted], ['Quarantined by dry run', t.quarantined], ['Inserted by this run', t.inserted], ['Already present, identical (skipped)', t.already_present], ['Key conflicts left untouched', t.conflicts], ['Accepted but not in target', t.not_applied], ['Target rows before the run', t.target_before], ['Target rows now', t.target_now]].map(([l, n]) => `<tr><td>${l}</td><td class="num"><b>${num(n)}</b></td></tr>`).join('')}</table></div>
   ${rc.sums ? `<p class="sm mute" style="margin:10px 0 0">Balance control total: accepted ${money(rc.sums.accepted)} · target ${money(rc.sums.target)}</p>` : ''}
   <p class="sm mute" style="margin:10px 0 0">Formulas: source = accepted + quarantined; accepted = inserted + already present + conflicts + not applied; ledger of inserted keys = target rows tagged to the run.</p></div>
  <div class="card"><h3>Checks</h3><ul class="checks">${rc.checks.map(c => `<li><span class="dot ${c.ok ? 'ok' : 'no'}">${c.ok ? '✓' : '×'}</span><div>${esc(c.label)}<div class="sm mute">${esc(c.detail)}</div></div></li>`).join('')}</ul></div></div>`;
  const q = U.quarantine[run.id] || [];
  h += `<div class="card"><h3>Quarantine evidence kept with this run ${chip(q.length)}</h3>${q.length ? `<details><summary>Show field-level evidence</summary><div class="scroll"><table><tr><th class="num">Row</th><th>Source key</th><th>Evidence</th></tr>${q.map(r => `<tr><td class="num">${r.row}</td><td class="mono">${esc(r.source_key)}</td><td>${r.errors.map(errBlock).join('')}</td></tr>`).join('')}</table></div></details>` : '<p class="mute sm">Nothing was quarantined.</p>'}</div>`;
  const shown = U.showAll.target ? S.target : S.target.slice(0, 15);
  h += `<div class="card"><div class="row sp"><h3>Mock target table ${chip(S.target.length + ' rows')}</h3><button class="btn sm" data-act="toggle_all" data-key="target">${U.showAll.target ? 'Show first 15' : 'Show all'}</button></div><div class="scroll"><table><tr><th>account_id</th><th>Name</th><th>email</th><th>country</th><th>status</th><th class="num">balance</th><th>Written by</th></tr>
   ${shown.map(r => `<tr><td class="mono tgtcol">${esc(r.account_id)}</td><td>${esc(r.record.first_name + ' ' + r.record.last_name)}</td><td class="mono">${esc(r.record.email)}</td><td>${esc(r.record.country_code)}</td><td>${esc(r.record.status)}</td><td class="num mono">${money(r.record.balance_minor)}</td><td>${chip(r.run_ref, r.run_ref === 'seed' ? '' : 'ok')}</td></tr>`).join('')}</table></div></div>`;
  return h;
}

/* ---------------- audit and logs ---------------- */
function describe(e) {
  const d = e.detail || {}; const v = e.plan_version ? 'v' + e.plan_version : '';
  switch (e.type) {
    case 'plan_created': return `Plan ${v} created${d.parent ? ' from v' + d.parent : ''} (${d.origin})`;
    case 'agent_proposal': return `Proposal saved as ${v} (${d.mode}${d.model ? ', ' + d.model : ''}, ${d.tool_calls} tool calls, ${d.rejected_tool_calls} refused, ${d.dropped} items rejected)`;
    case 'agent_failed': return `Agent failed: ${d.code}`;
    case 'dry_run': return `Dry run of ${v}: ${d.counts.accepted} accepted, ${d.counts.rejected} quarantined, fingerprint ${String(d.fingerprint).slice(0, 8)}`;
    case 'plan_approved': return `${v} approved by ${e.actor}${d.unanswered_questions ? ' with ' + d.unanswered_questions + ' unanswered questions' : ''}${d.comment ? ': “' + d.comment + '”' : ''}`;
    case 'plan_rejected': return `${v} rejected: “${d.comment}”`;
    case 'approval_revoked': return `Approval of ${v} revoked`;
    case 'question_answered': return `${d.question} on ${v} ${d.answered ? 'answered' : 'cleared'}`;
    case 'execute_started': return `Execution ${e.run_ref} started for ${v}${d.simulate_failure_after != null ? ' (simulated outage after ' + d.simulate_failure_after + ')' : ''}`;
    case 'execute_attempt': case 'retry_attempt': return `${e.run_ref} attempt ${d.attempt}: ${d.inserted} inserted, ${(d.skipped_own || 0) + (d.skipped_present || 0)} skipped, ${d.conflicts || 0} conflicts, checkpoint ${d.checkpoint} → ${d.status}${d.error_code ? ' (' + d.error_code + ')' : ''}`;
    case 'retry_requested': return `Retry of ${e.run_ref} requested (was ${d.previous_status}, resuming at row ${d.resume_from})`;
    case 'rollback': return `${e.run_ref} rolled back: ${d.removed} rows removed`;
    case 'rollback_rejected': return `Rollback of ${e.run_ref} refused: ${d.reason}`;
    case 'reconciliation': return `Reconciliation of ${e.run_ref}: ${d.passed ? 'passed' : 'FAILED ' + (d.failed_checks || []).join(', ')}`;
    case 'source_replaced': return `Source replaced: ${d.records} records${d.approvals_invalidated && d.approvals_invalidated.length ? '; approvals invalidated for ' + d.approvals_invalidated.map(x => 'v' + x).join(', ') : ''}`;
    case 'tests_run': return `Tests run: ${d.summary}`;
    default: return e.type.replace(/_/g, ' ');
  }
}
export function viewAudit(S, U) {
  const lf = U.fields.logLevel || 'all'; const logs = S.logs.filter(l => lf === 'all' || l.level === lf).slice(0, 100);
  if (U.loading.audit && !S.audit.length) return `<h2>History and logs</h2><div class="empty">${spin('Loading…')}</div>`;
  return `<h2>History and logs</h2><p class="lede">History is the append-only audit trail stored in the database (approvals, executions, retries, rollbacks). Logs are structured events, including every agent tool call, each carrying the request id.</p>${banner(U)}
  <div class="card"><div class="row sp"><h3>Audit history ${chip(S.audit.length)}</h3><div class="row"><button class="btn sm" data-act="reload_audit">Refresh</button><a class="btn sm" href="/api/audit/download" download>Download (JSON)</a></div></div>
   ${S.audit.length ? `<div class="scroll"><table><tr><th>When</th><th>Actor</th><th>Event</th><th>Request</th></tr>${S.audit.map(e => `<tr><td class="sm" style="white-space:nowrap">${esc(when(e.ts))}</td><td>${chip(e.actor, e.actor === 'system' ? '' : 'src')}</td><td>${esc(describe(e))}</td><td class="mono sm">${esc(e.request_id || '')}</td></tr>`).join('')}</table></div>` : '<p class="mute sm">No events yet.</p>'}</div>
  <div class="card"><div class="row sp"><h3>Structured logs ${chip(S.logs.length)}</h3><div class="row"><select aria-label="Filter log level" style="width:auto" data-change="loglevel">${['all', 'info', 'warning', 'error'].map(l => `<option ${lf === l ? 'selected' : ''}>${l}</option>`).join('')}</select><a class="btn sm" href="/api/logs/download" download>Download (JSONL)</a></div></div>
   ${logs.length ? `<div class="scroll"><table><tr><th>Time</th><th>Level</th><th>Event</th><th>Request</th><th>Fields</th></tr>${logs.map(l => `<tr><td class="sm mono" style="white-space:nowrap">${esc(l.ts.slice(11, 19))}</td><td>${chip(l.level, l.level === 'error' ? 'bad' : l.level === 'warning' ? 'warn' : '')}</td><td class="mono">${esc(l.event)}</td><td class="mono sm">${esc(l.request_id || '')}</td><td class="mono sm">${esc(JSON.stringify(l.fields).slice(0, 240))}</td></tr>`).join('')}</table></div>` : '<p class="mute sm">No log entries match.</p>'}</div>`;
}

/* ---------------- about and tests ---------------- */
export function viewDocs(S, U) {
  const T = U.tests; const cfg = S.config;
  return `<h2>About and tests</h2>
  <div class="card"><h3>What this is</h3><p>A full-stack workbench for planning and validating one bounded migration, <span class="mono">legacy_customers</span> into <span class="mono">accounts</span>, up to ${cfg.max_records} records. An LLM agent proposes mappings through allowlisted tools, a person approves an exact version, and a deterministic server-side engine performs the dry run, execution, retry, rollback and reconciliation against a mock target table in SQLite.</p>
   <h4>Architecture</h4><p class="sm">FastAPI backend (Pydantic validation, SQLAlchemy 2, SQLite in WAL mode) serves this UI and the JSON API. This page holds no authoritative state; it only renders what the API returns. Interactive API docs: <a href="/docs">/docs</a>. Health: <a href="/health">/health</a>, readiness: <a href="/ready">/ready</a>.</p>
   <h4>Identity (demo only)</h4><p class="sm">${esc(cfg.identity_note)} The server records the approver from the <span class="mono">X-Demo-User</span> header, never from the request body. Anyone who can reach the API can choose any configured demo name. This is not authentication or role-based access control.</p>
   <h4>Limits</h4><ul class="sm"><li>One source, one target, at most ${cfg.max_records} records.</li><li>SQLite serialises writers; every transaction takes the write lock, so this suits a demo, not high concurrency.</li><li>Source times are assumed UTC; balances use two decimals and a single currency.</li><li>The mock target is a table in the same database, not a separate system.</li></ul></div>
  <div class="card"><h3>Server configuration</h3><dl class="kv"><dt>LLM</dt><dd>${cfg.llm_configured ? 'configured' : 'not configured'} · ${esc(cfg.primary_model)}${cfg.fallback_models.length ? ' (fallback: ' + esc(cfg.fallback_models.join(', ')) + ')' : ''}</dd><dt>API key</dt><dd>stored only in the server environment; never sent to this page</dd></dl></div>
  <div class="card"><div class="row sp"><h3>Test suite</h3><button class="btn primary" data-act="run_tests" ${!cfg.test_runner_enabled || busy(U, 'tests') ? 'disabled' : ''}>${busy(U, 'tests') ? spin('Running (up to a few minutes)…') : (T ? 'Run again' : 'Run tests')}</button></div>
   ${!cfg.test_runner_enabled ? '<p class="sm mute">The in-app test runner is disabled on this deployment. Run <span class="mono">pytest</span> in the backend directory.</p>' : ''}
   ${T ? `<div class="banner ${T.passed ? 'ok' : 'bad'}"><b>${T.passed ? 'Passed' : 'Failed'}:</b> ${esc(T.summary)}</div><pre class="out">${esc(T.output)}</pre>` : '<p class="sm mute">Runs the backend pytest suite on the server and shows its real output.</p>'}</div>`;
}
