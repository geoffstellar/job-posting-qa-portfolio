// Templates management page (Phase 3 of Root Cause Clusters, 2026-04-21).
// Lists captured Templates-Category pairs with mute / rename / retire /
// bulk-resolve controls and an inline ticket drawer. Backed by
// /api/templates and /api/templates/<name>/* endpoints.

// ── Module state ────────────────────────────────────────────────────────────
let _templates = [];
let _expandedNames = new Set();      // which cards are showing their ticket drawer
let _retireContext = null;           // { name, openCount } while retire modal is open

// ── Utilities (inlined so this page is self-contained) ──────────────────────
function escHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function truncate(s, n) {
  s = String(s == null ? '' : s);
  return s.length > n ? s.slice(0, n) + '\u2026' : s;
}

function pluralize(n, singular, plural) {
  return n === 1 ? singular : (plural || singular + 's');
}

// ── Main entry ──────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => { loadTemplates(); });

async function loadTemplates() {
  const body = document.getElementById('templates-body');
  body.innerHTML = '<div class="loading">Loading\u2026</div>';
  try {
    const r = await fetch('/api/templates');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    _templates = await r.json();
    renderTemplates();
  } catch (e) {
    body.innerHTML = '<div class="empty">Could not load templates: ' + escHtml(e.message) + '</div>';
  }
}

function renderTemplates() {
  const body  = document.getElementById('templates-body');
  const count = document.getElementById('templates-count');
  if (!_templates.length) {
    if (count) count.textContent = '0';
    body.innerHTML = `<div class="empty">
      No templates captured yet.<br>
      Go to the <a href="/tickets">Tickets page</a>, turn on
      <em>Group possible root-cause patterns</em>, and use <strong>Capture
      as Template</strong> on any rollup card to create your first.
    </div>`;
    return;
  }
  if (count) count.textContent = `${_templates.length} ${pluralize(_templates.length, 'template')}`;
  const cards = _templates.map(t => renderTemplateCard(t)).join('');
  body.innerHTML = `<div class="tmpl-cards">${cards}</div>`;
}

function renderTemplateCard(t) {
  const name  = t.name || '';
  const nameAttr = escHtml(name);
  const statusPill = t.muted
    ? '<span class="tmpl-pill tmpl-pill-muted">Muted</span>'
    : '<span class="tmpl-pill tmpl-pill-active">Active</span>';
  const sev = (t.severity || 'MEDIUM').toUpperCase();
  const ageDays = t.age_days === null || t.age_days === undefined
    ? '\u2014'
    : (t.age_days === 0 ? 'today' : `${t.age_days} ${pluralize(t.age_days, 'day')} ago`);
  const expanded  = _expandedNames.has(name);
  const chevron   = expanded ? '\u25BC' : '\u25B6';
  const pattern   = t.pattern || '';
  const patClamp  = truncate(pattern, 220);
  const fixInstr  = (t.fix_instruction || '').trim();
  const openCnt   = t.open_count     || 0;
  const resCnt    = t.resolved_count || 0;
  const totCnt    = t.total_count    || 0;

  // Action buttons. "Mark Fixed" appears when muted (unmute -> regressions
  // surface); "Re-mute" appears when active (admin chose to hear regressions
  // loud but now wants to quiet them again).
  const muteToggleBtn = t.muted
    ? `<button class="btn-sm btn-apply" onclick="markFixedTemplate('${nameAttr}')"
               title="Unmute: matching findings will appear in Live Tickets so regressions show loud.">
         Mark Fixed
       </button>`
    : `<button class="btn-sm btn-clear" onclick="reMuteTemplate('${nameAttr}')"
               title="Re-mute: hide matching findings from Live Tickets again while you address a regression.">
         Re-mute
       </button>`;

  const resolveBtn = openCnt > 0
    ? `<button class="btn-sm btn-clear" onclick="resolveCurrentTickets('${nameAttr}')"
               title="Resolve all ${openCnt} currently-open ticket(s) linked to this template. Future matches still auto-route here.">
         Resolve ${openCnt} Open
       </button>`
    : '';

  return `
  <div class="tmpl-card ${t.muted ? 'is-muted' : 'is-active'}" id="tmpl-card-${nameAttr}">
    <div class="tmpl-card-head">
      <div class="tmpl-card-name-wrap">
        <span class="tmpl-card-name" id="tmpl-name-${nameAttr}">${escHtml(name)}</span>
        <button class="tmpl-card-rename-btn"
                onclick="startRenameTemplate('${nameAttr}')"
                title="Rename this template">\u270E</button>
      </div>
      ${statusPill}
      <span class="tmpl-card-sev sev sev-${escHtml(sev)}">${escHtml(sev)}</span>
      <span class="tmpl-card-age" title="${escHtml(t.muted_at || '')}">Captured ${escHtml(ageDays)}</span>
    </div>

    <div class="tmpl-card-pattern"
         onclick="toggleTemplateExpand('${nameAttr}')"
         title="Click to expand linked tickets">
      <span class="tmpl-card-chevron">${chevron}</span>
      <code>${escHtml(patClamp)}</code>
    </div>

    <div class="tmpl-card-meta">
      <span class="tmpl-card-counts">
        <strong>${openCnt}</strong> open &middot;
        <strong>${resCnt}</strong> resolved &middot;
        <strong>${totCnt}</strong> total
      </span>
      ${fixInstr ? `<span class="tmpl-card-fix">Fix: ${escHtml(fixInstr)}</span>` : ''}
    </div>

    <div class="tmpl-card-actions">
      ${muteToggleBtn}
      ${resolveBtn}
      <button class="btn-sm btn-danger" onclick="openRetireModal('${nameAttr}')"
              title="Permanently retire this template. The pair moves to the Rejected list.">
        Retire
      </button>
    </div>

    <div class="tmpl-card-drawer" id="tmpl-drawer-${nameAttr}"
         style="display:${expanded ? 'block' : 'none'}">
      ${expanded ? '<div class="tmpl-drawer-loading">Loading tickets\u2026</div>' : ''}
    </div>
  </div>`;
}

// ── Expand / collapse ───────────────────────────────────────────────────────
async function toggleTemplateExpand(name) {
  const drawer = document.getElementById('tmpl-drawer-' + name);
  const cardPattern = document.querySelector(`#tmpl-card-${CSS.escape(name)} .tmpl-card-chevron`);
  if (!drawer) return;
  if (_expandedNames.has(name)) {
    _expandedNames.delete(name);
    drawer.style.display = 'none';
    if (cardPattern) cardPattern.textContent = '\u25B6';
    return;
  }
  _expandedNames.add(name);
  drawer.style.display = 'block';
  if (cardPattern) cardPattern.textContent = '\u25BC';
  drawer.innerHTML = '<div class="tmpl-drawer-loading">Loading tickets\u2026</div>';
  try {
    const r = await fetch(`/api/templates/${encodeURIComponent(name)}/tickets`);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const rows = await r.json();
    drawer.innerHTML = renderDrawer(rows);
  } catch (e) {
    drawer.innerHTML = `<div class="tmpl-drawer-empty">Could not load tickets: ${escHtml(e.message)}</div>`;
  }
}

function renderDrawer(rows) {
  if (!rows || !rows.length) {
    return '<div class="tmpl-drawer-empty">No tickets currently linked to this template.</div>';
  }
  const head = `<table class="tmpl-drawer-table">
    <thead><tr>
      <th>Ticket</th><th>Community</th><th>Posting</th>
      <th>Status</th><th>Severity</th><th>Originally</th>
    </tr></thead><tbody>`;
  const body = rows.map(r => {
    const sev = (r.severity || '').toUpperCase();
    const jobLink = r.job_url
      ? `<a href="${escHtml(r.job_url)}" target="_blank" rel="noopener">${escHtml(truncate(r.job_title || '', 40))}</a>`
      : escHtml(truncate(r.job_title || '', 40));
    return `<tr class="tmpl-drawer-row tmpl-drawer-row-${escHtml(r.status || 'Open')}">
      <td style="font-family:monospace;font-size:12px;color:#888">${escHtml(r.ticket_id)}</td>
      <td style="font-weight:600">${escHtml(r.community || '')}</td>
      <td style="color:#555">${jobLink}</td>
      <td>${escHtml(r.status || '')}</td>
      <td><span class="sev sev-${escHtml(sev)}">${escHtml(sev)}</span></td>
      <td style="font-size:12px;color:#888">${escHtml(r.captured_from || '\u2014')}</td>
    </tr>`;
  }).join('');
  return head + body + '</tbody></table>';
}

// ── Rename ──────────────────────────────────────────────────────────────────
function startRenameTemplate(name) {
  const el = document.getElementById('tmpl-name-' + name);
  if (!el) return;
  // Replace the <span> with an inline input + Save / Cancel buttons.
  const current = _templates.find(t => t.name === name)?.name || '';
  const wrap = el.closest('.tmpl-card-name-wrap');
  if (!wrap) return;
  wrap.innerHTML = `
    <input type="text" class="tmpl-rename-input" id="tmpl-rename-input-${escHtml(name)}"
           value="${escHtml(current)}" maxlength="200">
    <button class="btn-sm btn-apply" onclick="submitRenameTemplate('${escHtml(name)}')">Save</button>
    <button class="btn-sm btn-clear" onclick="cancelRenameTemplate()">Cancel</button>
    <span class="tmpl-rename-status" id="tmpl-rename-status-${escHtml(name)}"></span>`;
  const input = document.getElementById('tmpl-rename-input-' + name);
  if (input) { input.focus(); input.select(); }
}

function cancelRenameTemplate() {
  // Simplest recovery: re-render the list from current state.
  renderTemplates();
}

async function submitRenameTemplate(oldName) {
  const input  = document.getElementById('tmpl-rename-input-' + oldName);
  const status = document.getElementById('tmpl-rename-status-' + oldName);
  const newName = (input?.value || '').trim();
  if (!newName) {
    if (status) { status.textContent = 'Name cannot be empty.'; status.className = 'tmpl-rename-status error'; }
    input?.focus();
    return;
  }
  if (newName === oldName) { cancelRenameTemplate(); return; }
  if (status) { status.textContent = 'Saving\u2026'; status.className = 'tmpl-rename-status'; }
  try {
    const r = await fetch(`/api/templates/${encodeURIComponent(oldName)}/rename`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ new_name: newName }),
    });
    const data = await r.json();
    if (!r.ok || data.error) throw new Error(data.error || 'Server error');
    showToast('success', `Renamed to "${newName}" (${data.renamed_tickets} ticket${data.renamed_tickets === 1 ? '' : 's'} updated).`);
    await loadTemplates();
  } catch (e) {
    if (status) { status.textContent = e.message; status.className = 'tmpl-rename-status error'; }
  }
}

// ── Mute / Unmute ───────────────────────────────────────────────────────────
async function markFixedTemplate(name) {
  try {
    const r = await fetch(`/api/templates/${encodeURIComponent(name)}/unmute`, { method: 'POST' });
    const data = await r.json();
    if (!r.ok || data.error) throw new Error(data.error || 'Server error');
    showToast('success', `Template marked Fixed. Matching findings will now show in Live Tickets as regressions.`);
    await loadTemplates();
  } catch (e) {
    showToast('error', 'Could not mark fixed: ' + e.message);
  }
}

async function reMuteTemplate(name) {
  try {
    const r = await fetch(`/api/templates/${encodeURIComponent(name)}/mute`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: 'Re-muted from Templates page' }),
    });
    const data = await r.json();
    if (!r.ok || data.error) throw new Error(data.error || 'Server error');
    showToast('success', `Template re-muted. Matching findings will hide from Live Tickets again.`);
    await loadTemplates();
  } catch (e) {
    showToast('error', 'Could not re-mute: ' + e.message);
  }
}

// ── Resolve currently-open tickets ──────────────────────────────────────────
async function resolveCurrentTickets(name) {
  const t = _templates.find(x => x.name === name);
  const n = t?.open_count || 0;
  if (n === 0) { showToast('success', 'No open tickets to resolve.'); return; }
  if (!confirm(`Resolve ${n} currently-open ticket${n === 1 ? '' : 's'} linked to "${name}"?\n\nThis clears the open queue for this template. The template itself stays active — future matching findings still auto-route here.`)) {
    return;
  }
  try {
    const r = await fetch(`/api/templates/${encodeURIComponent(name)}/resolve-current`, { method: 'POST' });
    const data = await r.json();
    if (!r.ok || data.error) throw new Error(data.error || 'Server error');
    showToast('success', `Resolved ${data.tickets_resolved} open ticket${data.tickets_resolved === 1 ? '' : 's'} for "${name}".`);
    await loadTemplates();
  } catch (e) {
    showToast('error', 'Could not resolve: ' + e.message);
  }
}

// ── Retire (with optional bundled bulk-resolve) ─────────────────────────────
function openRetireModal(name) {
  const t = _templates.find(x => x.name === name);
  if (!t) return;
  _retireContext = { name, openCount: t.open_count || 0 };
  const modal    = document.getElementById('retire-modal');
  const namePrev = document.getElementById('retire-name-preview');
  const openCnt  = document.getElementById('retire-open-count');
  const chk      = document.getElementById('retire-also-resolve');
  const status   = document.getElementById('retire-status');
  const btn      = document.getElementById('retire-confirm-btn');
  if (namePrev) namePrev.textContent = name;
  if (openCnt)  openCnt.textContent  = t.open_count || 0;
  if (chk)      chk.checked = false;
  if (status)   { status.textContent = ''; status.className = 'tmpl-modal-status'; }
  if (btn)      { btn.disabled = false; btn.textContent = 'Retire'; }
  if (modal)    modal.style.display = 'flex';
}

function closeRetireModal() {
  const modal = document.getElementById('retire-modal');
  if (modal) modal.style.display = 'none';
  _retireContext = null;
}

async function confirmRetireTemplate() {
  if (!_retireContext) return;
  const { name } = _retireContext;
  const chk    = document.getElementById('retire-also-resolve');
  const status = document.getElementById('retire-status');
  const btn    = document.getElementById('retire-confirm-btn');
  const alsoResolve = !!chk?.checked;
  if (btn)    { btn.disabled = true; btn.textContent = 'Retiring\u2026'; }
  if (status) { status.textContent = 'Saving\u2026'; status.className = 'tmpl-modal-status'; }
  try {
    const r = await fetch(`/api/templates/${encodeURIComponent(name)}/retire`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ also_resolve_open: alsoResolve }),
    });
    const data = await r.json();
    if (!r.ok || data.error) throw new Error(data.error || 'Server error');
    const resolvedNote = alsoResolve && data.tickets_resolved
      ? ` (${data.tickets_resolved} open ticket${data.tickets_resolved === 1 ? '' : 's'} resolved)`
      : '';
    closeRetireModal();
    // Fade the card out, then reload the list so counts re-aggregate cleanly.
    const card = document.getElementById('tmpl-card-' + name);
    if (card) {
      card.style.transition = 'opacity 0.4s';
      card.style.opacity    = '0';
      setTimeout(() => { card.remove(); loadTemplates(); }, 420);
    } else {
      loadTemplates();
    }
    showToast('success', `Retired "${name}"${resolvedNote}. Pair is now in the Rejected list.`);
  } catch (e) {
    if (status) { status.textContent = e.message; status.className = 'tmpl-modal-status error'; }
    if (btn)    { btn.disabled = false; btn.textContent = 'Retire'; }
  }
}
