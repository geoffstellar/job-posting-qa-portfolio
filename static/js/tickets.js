// ── Filter state ─────────────────────────────────────────────────────────────
// Direction B redesign (2026-04-21): severity / status / detected moved from
// single-select chip rows to multi-select dropdowns, matching the Community /
// Check Type component. All three use the same _msData shape so toggleMs /
// buildMsList / toggleMsItem work uniformly.
//
// Multi-select state: key → { all: [full list], selected: Set, hasSearch: bool }
const _msData = {
  community: { all: [], selected: new Set(), hasSearch: true },
  checktype: { all: [], selected: new Set(), grouped: {}, hasSearch: true },
  severity:  { all: [], selected: new Set(), hasSearch: false },
  status:    { all: [], selected: new Set(['Open']), hasSearch: false },
  detected:  { all: [], selected: new Set(), hasSearch: false },
};

// Default button-label text when nothing is selected. Keys must match _msData.
const _MS_DEFAULT_LABELS = {
  community: 'All communities',
  checktype: 'All check types',
  severity:  'All severities',
  status:    'Any status',
  detected:  'All sources',
};

// ── Multi-select helpers ──────────────────────────────────────────────────────
function toggleMs(key) {
  const dd  = document.getElementById('ms-' + key + '-dd');
  const btn = document.getElementById('ms-' + key + '-btn');
  const isOpen = dd.classList.contains('open');
  // Close all dropdowns first
  document.querySelectorAll('.ms-dropdown').forEach(d => d.classList.remove('open'));
  document.querySelectorAll('.ms-btn').forEach(b => b.classList.remove('open'));
  if (!isOpen) { dd.classList.add('open'); btn.classList.add('open'); }
}

document.addEventListener('click', e => {
  // After buildMsList replaces innerHTML, the clicked element is orphaned
  // and closest('.ms-wrap') returns null even though the click was inside
  // the dropdown. Check whether the target is still in the DOM before
  // closing — if it's detached, the click came from a re-rendered list item.
  if (!e.target.closest('.ms-wrap') && document.contains(e.target)) {
    document.querySelectorAll('.ms-dropdown').forEach(d => d.classList.remove('open'));
    document.querySelectorAll('.ms-btn').forEach(b => b.classList.remove('open'));
  }
});

function buildMsList(key, query) {
  if (key === 'checktype') { buildChecktypeList(query); return; }
  const list = document.getElementById('ms-' + key + '-list');
  const data = _msData[key];
  const q    = (query || '').toLowerCase();
  const items = data.all.filter(v => !q || v.toLowerCase().includes(q));
  const allChecked = data.all.length > 0 && data.selected.size === data.all.length;
  let html = '';
  // Select All row
  html += `<div class="ms-item" onclick="toggleMsAll('${key}')">
    <input type="checkbox" ${allChecked ? 'checked' : ''} onclick="event.stopPropagation();toggleMsAll('${key}')">
    <span><strong>Select All</strong></span>
  </div><hr class="ms-divider">`;
  items.forEach(v => {
    const chk = data.selected.has(v) ? 'checked' : '';
    const esc2 = v.replace(/'/g, "\'").replace(/"/g, '&quot;');
    html += `<div class="ms-item" onclick="toggleMsItem('${key}','${esc2}')">
      <input type="checkbox" ${chk} onclick="event.stopPropagation();toggleMsItem('${key}','${esc2}')">
      <span>${escHtml(v)}</span>
    </div>`;
  });
  list.innerHTML = html;
}

function buildChecktypeList(query) {
  const list = document.getElementById('ms-checktype-list');
  if (!list) return;
  const data = _msData.checktype;
  const q = (query || '').toLowerCase();

  // Flat model (2026-04-17): there is ONE taxonomy axis — Category — and the
  // dropdown groups check types by Category only. Category order is fixed
  // (matches taxonomy.py CATEGORIES). Any non-canonical value (shouldn't
  // happen post-flatten) falls back to alpha-sort after the canonical six.
  const CATEGORY_ORDER = ['Tone', 'Content', 'Formatting', 'Structure', 'Job Title', 'HTML'];

  // Group check_types by their Category (parsed from the "Category — Issue Type" format)
  const grouped = {};
  data.all.forEach(ct => {
    const sep = ct.indexOf(' \u2014 ');
    const cat = sep > -1 ? ct.substring(0, sep) : 'Other';
    if (!grouped[cat]) grouped[cat] = [];
    grouped[cat].push(ct);
  });

  // Filter by query
  const filteredGrouped = {};
  Object.keys(grouped).forEach(cat => {
    const items = grouped[cat].filter(ct => !q || ct.toLowerCase().includes(q));
    if (items.length > 0) filteredGrouped[cat] = items;
  });

  const allChk = data.all.length > 0 && data.selected.size === data.all.length;

  let html = `<div class="ms-item" onclick="toggleMsAll('checktype')">
    <input type="checkbox" ${allChk ? 'checked' : ''} onclick="event.stopPropagation();toggleMsAll('checktype')">
    <span><strong>All Check Types</strong></span>
  </div><hr class="ms-divider">`;

  function renderGroup(cat, items) {
    const groupItems = grouped[cat] || [];
    const allGroupSel = groupItems.length > 0 && groupItems.every(ct => data.selected.has(ct));
    const esc = cat.replace(/'/g, "\'").replace(/"/g, '&quot;');
    html += `<div class="category-group-hdr" onclick="toggleChecktypeGroup('${esc}')">
      <input type="checkbox" ${allGroupSel ? 'checked' : ''} onclick="event.stopPropagation();toggleChecktypeGroup('${esc}')">
      <span>${escHtml(cat)}</span>
      <span style="margin-left:auto;font-size:11px;color:#999;font-weight:400">${groupItems.length}</span>
    </div>`;
    items.forEach(ct => {
      const chk = data.selected.has(ct) ? 'checked' : '';
      const esc2 = ct.replace(/'/g, "\'").replace(/"/g, '&quot;');
      // Show only the issue_type part (after the em-dash) for cleaner display
      const sep = ct.indexOf(' \u2014 ');
      const displayText = sep > -1 ? ct.substring(sep + 3) : ct;
      html += `<div class="ms-item ms-item-sub" onclick="toggleMsItem('checktype','${esc2}')">
        <input type="checkbox" ${chk} onclick="event.stopPropagation();toggleMsItem('checktype','${esc2}')">
        <span>${escHtml(displayText)}</span>
      </div>`;
    });
  }

  // Render in canonical Category order first, then any non-canonical
  // (post-migration stragglers) sorted alphabetically after.
  const canonical = CATEGORY_ORDER.filter(c => filteredGrouped[c]);
  const other     = Object.keys(filteredGrouped)
                      .filter(c => !CATEGORY_ORDER.includes(c))
                      .sort();
  [...canonical, ...other].forEach(cat => renderGroup(cat, filteredGrouped[cat]));

  list.innerHTML = html;
}

function toggleChecktypeGroup(category) {
  const data = _msData.checktype;
  const SEP = ' \u2014 ';
  const groupItems = data.all.filter(ct => {
    const idx = ct.indexOf(SEP);
    return idx > -1 ? ct.substring(0, idx) === category : false;
  });
  const allSel = groupItems.length > 0 && groupItems.every(ct => data.selected.has(ct));
  groupItems.forEach(ct => allSel ? data.selected.delete(ct) : data.selected.add(ct));
  updateMsBtn('checktype');
  const searchEl = document.querySelector('#ms-checktype-dd .ms-search');
  buildChecktypeList(searchEl ? searchEl.value : '');
}

function toggleMsAll(key) {
  const data = _msData[key];
  if (data.selected.size === data.all.length) {
    data.selected.clear();
  } else {
    data.all.forEach(v => data.selected.add(v));
  }
  updateMsBtn(key);
  buildMsList(key);
}

function toggleMsItem(key, val) {
  const data = _msData[key];
  if (data.selected.has(val)) data.selected.delete(val);
  else data.selected.add(val);
  updateMsBtn(key);
  const searchEl = document.querySelector('#ms-' + key + '-dd .ms-search');
  buildMsList(key, searchEl ? searchEl.value : '');
}

function filterMs(key, q) { buildMsList(key, q); }

function updateMsBtn(key) {
  const data  = _msData[key];
  const label = document.getElementById('ms-' + key + '-label');
  const btn   = document.getElementById('ms-' + key + '-btn');
  const badge = document.getElementById('ms-' + key + '-badge');
  if (!label || !btn) return;
  const n = data.selected.size;
  if (n === 0) {
    label.textContent = _MS_DEFAULT_LABELS[key] || 'All';
    btn.classList.remove('has-selection');
    if (badge) badge.style.display = 'none';
  } else if (n === 1) {
    // Single selection — show the value itself, no count badge needed.
    let text = [...data.selected][0];
    // For check_type ("Category \u2014 Issue Type"), just show the issue_type.
    if (key === 'checktype') {
      const sep = text.indexOf(' \u2014 ');
      if (sep > -1) text = text.substring(sep + 3);
    }
    label.textContent = text;
    btn.classList.add('has-selection');
    if (badge) badge.style.display = 'none';
  } else {
    // Multiple — show a short default label + a count badge so the count is
    // always visible without reading the button text.
    label.textContent = _MS_DEFAULT_LABELS[key] || 'Selected';
    btn.classList.add('has-selection');
    if (badge) { badge.textContent = n; badge.style.display = 'inline-block'; }
  }
}

function resetMs(key) {
  _msData[key].selected.clear();
  updateMsBtn(key);
  buildMsList(key);
}

function getMsParam(key) {
  const sel = _msData[key].selected;
  return sel.size > 0 ? [...sel].join(',') : '';
}

// ── Reset filters ──────────────────────────────────────────────────────────
// Direction B (2026-04-21): all state filters are now multi-select, so reset
// just clears every _msData set and re-seeds Status with the default Open.
function resetFilters() {
  resetMs('community');
  resetMs('checktype');
  resetMs('severity');
  resetMs('status');
  _msData.status.selected.add('Open');
  updateMsBtn('status');
  buildMsList('status');
  resetMs('detected');
  // Clear Section dropdown
  const sec = document.getElementById('filter-section');
  if (sec) sec.value = '';
  // Clear date range
  const df = document.getElementById('date-from');
  const dt = document.getElementById('date-to');
  if (df) df.value = '';
  if (dt) dt.value = '';
  clearDatePreset();
  loadTickets();
}

// ── Date range presets (2026-04-16) ──────────────────────────────────────────

function _isoDate(d) {
  // Format a Date as YYYY-MM-DD in local time
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function setDatePreset(preset, btn) {
  const today = new Date();
  let from, to;
  switch (preset) {
    case 'today':
      from = to = _isoDate(today);
      break;
    case 'yesterday': {
      const y = new Date(today); y.setDate(y.getDate() - 1);
      from = to = _isoDate(y);
      break;
    }
    case 'last7': {
      const d7 = new Date(today); d7.setDate(d7.getDate() - 6);
      from = _isoDate(d7); to = _isoDate(today);
      break;
    }
    case 'week': {
      // Monday of this week (ISO: Monday=1)
      const dow = today.getDay(); // 0=Sun
      const diff = dow === 0 ? 6 : dow - 1;
      const mon = new Date(today); mon.setDate(mon.getDate() - diff);
      from = _isoDate(mon); to = _isoDate(today);
      break;
    }
    case 'month':
      from = _isoDate(new Date(today.getFullYear(), today.getMonth(), 1));
      to = _isoDate(today);
      break;
    default: return;
  }
  const df = document.getElementById('date-from');
  const dt = document.getElementById('date-to');
  if (df) df.value = from;
  if (dt) dt.value = to;
  // Highlight the active preset chip
  document.querySelectorAll('.date-chip').forEach(c => c.classList.remove('active'));
  if (btn) btn.classList.add('active');
}

function clearDatePreset() {
  document.querySelectorAll('.date-chip').forEach(c => c.classList.remove('active'));
}

// ── Boot ─────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  loadFilters();
  loadTickets();
  checkDirty();
  // pending_tickets system retired 2026-04-16. Badge fetch removed.
  // New checks come through the Claude qa-rules-maintenance skill
  // (the in-dashboard Add Check wizard was removed 2026-04-24).
  // Prime the rejected badge count so it shows immediately, not only after
  // the Rejected tab is clicked.
  fetch('/api/rejected-count').then(r => r.json()).then(d => {
    const mb = document.getElementById('rejected-mode-badge');
    if (mb) {
      mb.textContent = d.count || 0;
      mb.style.display = (d.count || 0) > 0 ? 'inline-block' : 'none';
    }
  }).catch(() => {});
});

async function loadFilters() {
  const r = await fetch('/api/ticket-filters');
  const d = await r.json();
  _msData.community.all = d.communities || [];
  _msData.checktype.all = d.check_types || [];
  _msData.severity.all  = d.severities  || ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'];
  _msData.status.all    = d.statuses    || ['Open', 'Resolved', 'Acknowledged'];
  _msData.detected.all  = d.detected_by || ['AUTO', 'CLAUDE'];
  buildMsList('community');
  buildMsList('checktype');
  buildMsList('severity');
  buildMsList('status');
  buildMsList('detected');
  // Status default (Open) is pre-seeded in _msData — sync the button label now
  // that the list has rendered.
  updateMsBtn('status');
}

// ── Load + render tickets ─────────────────────────────────────────────────────
let _tickets = [];

async function loadTickets() {
  document.getElementById('tickets-body').innerHTML = '<div class="loading">Loading…</div>';
  const params = new URLSearchParams();
  // All five filters are now multi-select. Backend /api/tickets accepts
  // comma-separated values and builds an IN (...) clause per column.
  const status = getMsParam('status');
  const sev    = getMsParam('severity');
  const det    = getMsParam('detected');
  const comm   = getMsParam('community');
  const chk    = getMsParam('checktype');
  if (status) params.set('status',      status);
  if (sev)    params.set('severity',    sev);
  if (det)    params.set('detected_by', det);
  if (comm)   params.set('community',   comm);
  if (chk)    params.set('check_type',  chk);
  // Section filter (2026-04-17 flatten). Value "(null)" filters to cross-cutting.
  const sec = document.getElementById('filter-section');
  if (sec && sec.value) params.set('section', sec.value);
  // Date range filter (2026-04-16)
  const df = document.getElementById('date-from');
  const dt = document.getElementById('date-to');
  if (df && df.value) params.set('date_from', df.value);
  if (dt && dt.value) params.set('date_to',   dt.value);

  try {
    const r = await fetch('/api/tickets?' + params);
    _tickets = await r.json();
    if (!Array.isArray(_tickets)) throw new Error(_tickets.error || 'Bad response');
    renderTickets(_tickets);
  } catch(e) {
    document.getElementById('tickets-body').innerHTML = '<div class="empty">Error loading tickets: ' + e.message + '</div>';
  }
}

function statusStyle(status) {
  if (status === 'Resolved')           return 'color:#065f46;font-weight:600;font-size:12px';
  if (status === 'Flagged Incorrectly') return 'color:#856404;font-weight:600;font-size:12px';
  return 'color:#1a1a2e;font-weight:600;font-size:12px';
}

// Template-sourced rollup threshold: groups of tickets sharing the same
// (category, issue_type, offending_text) across this many or more distinct
// postings get rolled up into a single expandable row when the "Group
// template-sourced" filter is on. Three was picked based on the real-data
// distribution (2026-04-17) — most junk template matches repeat across
// dozens or hundreds of postings; 3 is enough signal to not hide one-offs.
const _TEMPLATE_ROLLUP_THRESHOLD = 3;

function _templateGroupKey(t) {
  return (t.category || '') + '\u0001' + (t.issue_type || t.check_type || '') + '\u0001' + (offText(t) || '');
}

// Split the ticket list into (rollup groups, passthrough tickets) based on the
// "Group template-sourced" checkbox + threshold. Returns:
//   { groups: [{key, sample, tickets, distinctPostings}], flat: [tickets not in any group] }
function _computeTemplateGroups(tickets) {
  const toggle = document.getElementById('filter-template-rollup');
  if (!toggle || !toggle.checked) return { groups: [], flat: tickets };
  const buckets = new Map();
  tickets.forEach(t => {
    const key = _templateGroupKey(t);
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(t);
  });
  const groups = [];
  const flat   = [];
  buckets.forEach((bucketTickets, key) => {
    const distinctPostings = new Set(bucketTickets.map(t => (t.community || '') + '|' + (t.req_id || ''))).size;
    if (distinctPostings >= _TEMPLATE_ROLLUP_THRESHOLD) {
      groups.push({ key, sample: bucketTickets[0], tickets: bucketTickets, distinctPostings });
    } else {
      bucketTickets.forEach(t => flat.push(t));
    }
  });
  // Sort rollups by ticket count desc so the biggest offenders surface first
  groups.sort((a, b) => b.tickets.length - a.tickets.length);
  return { groups, flat };
}

// Called when the user toggles the "Group template-sourced" filter. Re-renders
// the current ticket list without a server round-trip.
function onTemplateRollupToggle() {
  if (Array.isArray(_tickets)) renderTickets(_tickets);
}

function renderTickets(tickets) {
  const { groups, flat } = _computeTemplateGroups(tickets);
  const totalCount = tickets.length;
  const countLabel = groups.length
    ? `${totalCount} ticket${totalCount !== 1 ? 's' : ''}  (${groups.length} possible pattern${groups.length !== 1 ? 's' : ''})`
    : `${totalCount} ticket${totalCount !== 1 ? 's' : ''}`;
  document.getElementById('ticket-count').textContent = countLabel;
  if (!tickets.length) {
    document.getElementById('tickets-body').innerHTML = '<div class="empty">No tickets match the current filters.</div>';
    return;
  }
  let html = '<div class="bulk-bar" id="bulk-bar" style="display:none">'
    + '<span id="bulk-count">0 selected</span>'
    + '<button class="btn-bulk-resolve" onclick="bulkResolve()">Resolve Selected</button>'
    + '<button class="btn-bulk-cancel" onclick="clearSelection()">Clear</button>'
    + '</div>';

  // Template-sourced rollups (if any): render each as a single collapsible
  // "group" card above the regular ticket table. Individual tickets inside a
  // rollup are NOT shown in the flat table below.
  if (groups.length) {
    html += '<div class="template-rollups">';
    groups.forEach((g, gi) => {
      const s = g.sample;
      const ar    = s.category       || '';
      const itype = s.issue_type || s.check_type || '';
      const off   = offText(s) || '';
      const ticketIdsJson = JSON.stringify(g.tickets.map(t => t.ticket_id));
      // Capture-as-Template action is gated by window.CAN_CAPTURE_TEMPLATES
      // (server-rendered from the user's 'templates' page permission). Users
      // without access see the rollup card but no capture button \u2014 they can
      // still expand the card to interact with individual tickets below.
      const captureBtn = window.CAN_CAPTURE_TEMPLATES
        ? `<button class="btn-sm btn-apply template-rollup-capture"
                   onclick="event.stopPropagation(); captureAsTemplate(${gi})"
                   title="Create a managed Templates entry so future matching findings auto-route here. Tickets get hidden from Live Tickets while you fix the source.">
             Capture as Template
           </button>`
        : '';
      html += `<div class="template-rollup" id="tmpl-rollup-${gi}">
        <div class="template-rollup-head" onclick="toggleTemplateRollup(${gi})">
          <span class="chevron">&#9658;</span>
          <span class="template-rollup-badge" title="These tickets share the same offending text across 3+ postings. The pattern may or may not trace to a shared template \u2014 use judgment.">Possible pattern</span>
          <span class="template-rollup-where">${escHtml(ar)} &middot; ${escHtml(itype)}</span>
          <span class="template-rollup-count">${g.tickets.length} tickets &middot; ${g.distinctPostings} postings</span>
          ${captureBtn}
        </div>
        <div class="template-rollup-text" title="${escHtml(off)}">${escHtml(truncate(off, 140))}</div>
        <div class="template-rollup-body" id="tmpl-rollup-body-${gi}" style="display:none"></div>
        <script type="application/json" id="tmpl-rollup-data-${gi}">${ticketIdsJson}</script>
      </div>`;
    });
    html += '</div>';
  }

  html += '<table class="ticket-table"><thead><tr>'
    + '<th style="width:32px"><input type="checkbox" id="select-all" onclick="toggleSelectAll(this)" title="Select all"></th>'
    + '<th></th><th>Ticket</th><th>Date</th><th>Community</th><th>Job Title</th>'
    + '<th>Severity</th><th>Category</th><th>Section</th><th>Issue Type</th>'
    + '<th>Detected By</th><th>Status</th>'
    + '</tr></thead><tbody>';

  flat.forEach((t, i) => {
    const resolved   = t.status === 'Resolved';
    const flaggedBad = t.status === 'Flagged Incorrectly';
    const acked      = t.status === 'Acknowledged';
    const sevUp      = (t.severity || '').toUpperCase();
    const ackEligible = sevUp === 'MEDIUM' || sevUp === 'LOW';
    const rowClass   = resolved ? ' resolved' : (flaggedBad ? ' flagged-incorrect' : (acked ? ' acknowledged' : ''));
    const ar         = t.category        || '';   // Category under flat model
    const sec        = t.section     || '';   // display-only Section tag
    const secDisplay = sec ? escHtml(sec) : '<span style="color:#bbb">\u2014</span>';
    const itype      = t.issue_type  || t.check_type || '';
    const canSelect = !resolved && !flaggedBad && !acked;
    html += `<tr class="ticket-row${rowClass}" data-idx="${i}" onclick="toggleRow(${i})">
      <td onclick="event.stopPropagation()">
        ${canSelect ? `<input type="checkbox" class="ticket-cb" data-tid="${escHtml(t.ticket_id)}" data-idx="${i}" onchange="updateBulkBar()">` : ''}
      </td>
      <td><span class="chevron">&#9658;</span></td>
      <td style="font-family:monospace;font-size:12px;color:#888">${escHtml(t.ticket_id)}</td>
      <td style="white-space:nowrap;color:#888;font-size:12px">${escHtml(t.date_flagged||'')}</td>
      <td style="font-weight:600">${escHtml(t.community||'')}</td>
      <td style="color:#555">${escHtml(truncate(t.job_title||'',40))}</td>
      <td><span class="sev sev-${escHtml(t.severity)}">${escHtml(t.severity)}</span></td>
      <td style="max-width:110px;color:#666;font-size:12px">${escHtml(ar)}</td>
      <td style="max-width:150px;color:#888;font-size:12px">${secDisplay}</td>
      <td style="max-width:170px">${escHtml(itype)}</td>
      <td><span class="det det-${escHtml(t.detected_by)}">${escHtml(t.detected_by)}</span></td>
      <td style="${statusStyle(t.status)}">${escHtml(t.status)}</td>
    </tr>
    <tr class="detail-row" id="detail-${i}" style="display:none">
      <td colspan="12">
        <div class="detail-inner">
          <div class="detail-grid">
            <div class="detail-block">
              <label>Area</label>
              <div class="val ${ar?'':'empty-val'}">${escHtml(ar||'—')}</div>
            </div>
            <div class="detail-block">
              <label>Issue Type</label>
              <div class="val ${itype?'':'empty-val'}">${escHtml(itype||'—')}</div>
            </div>
            <div class="detail-block">
              <label>Issue Summary</label>
              <div class="val ${t.issue_summary?'':'empty-val'}">${escHtml(t.issue_summary||'—')}</div>
            </div>
            <div class="detail-block">
              <label>Offending Text</label>
              <div class="val ${offText(t)?'':'empty-val'}">${escHtml(offText(t)||'—')}</div>
            </div>
            ${t.job_url ? `<div class="detail-block detail-block-url">
              <label>Job Posting</label>
              <a class="job-link" href="${escHtml(t.job_url)}" target="_blank" rel="noopener">
                View on Hireology ↗
              </a>
            </div>` : ''}
          </div>
          <div class="resolve-row">
            ${resolved
              ? `<span class="resolve-status done">✓ Resolved</span>`
              : flaggedBad
                ? `<span class="resolve-status flagged">⚑ Flagged as Incorrect</span>`
                : acked
                  ? `<span class="resolve-status acknowledged">✓ Acknowledged${t.reason ? ' — ' + escHtml(t.reason) : ''}</span>
                     <button class="btn-flag" id="btn-reopen-${i}" onclick="reopenTicket(event,'${escHtml(t.ticket_id)}',${i})">Reopen</button>`
                  : `<button class="btn-resolve" id="btn-resolve-${i}" onclick="resolveTicket(event,'${escHtml(t.ticket_id)}',${i})">Mark Resolved</button>
                     ${ackEligible ? `<button class="btn-ack" id="btn-ack-${i}" onclick="acknowledgeTicket(event,'${escHtml(t.ticket_id)}',${i})">Acknowledge</button>` : ''}
                     <button class="btn-flag"    id="btn-flag-${i}"    onclick="flagIncorrect(event,'${escHtml(t.ticket_id)}',${i})">Flag as Incorrect</button>
                     <span class="resolve-status" id="rs-${i}"></span>`
            }
          </div>
          ${renderLastAction(t, i)}
          <div class="ticket-history-block" id="hist-block-${i}" style="display:none">
            <div class="ticket-history-list" id="hist-list-${i}"></div>
          </div>
          <div class="ticket-flag-panel" id="flag-panel-${i}">
            <div style="font-size:12px;font-weight:600;margin-bottom:6px;color:#92590a">Why is this a false positive? (optional)</div>
            <textarea class="ticket-flag-notes" id="flag-notes-${i}" placeholder="e.g. This community uses a different wage format — not actually an error…"></textarea>
            <div class="ticket-flag-actions">
              <button class="btn-flag-ticket-confirm" onclick="confirmFlagIncorrect(event,'${escHtml(t.ticket_id)}',${i})">Confirm Flag</button>
              <button class="btn-flag-ticket-cancel"  onclick="cancelFlagIncorrect(event,${i})">Cancel</button>
            </div>
          </div>
          <div class="ticket-notes-block">
            <div class="ticket-notes-header">
              <strong>Notes</strong>
              <span class="ticket-notes-count" id="notes-count-${i}">·&nbsp;—</span>
            </div>
            <div class="ticket-notes-list" id="notes-list-${i}"></div>
            <div class="ticket-notes-add">
              <textarea class="ticket-notes-input" id="note-new-${i}" placeholder="Add a note&hellip;" rows="1"></textarea>
              <button class="btn-notes-post" onclick="addTicketNote('${escHtml(t.ticket_id)}',${i})">Post</button>
            </div>
          </div>
          <div class="ticket-ack-panel" id="ack-panel-${i}">
            <div style="font-size:12px;font-weight:600;margin-bottom:6px;color:#334155">Why acknowledge this instead of fixing?</div>
            <select class="ticket-ack-category" id="ack-cat-${i}" onchange="_ackCategoryChanged(${i})">
              <option value="">— Select reason —</option>
              <option value="Correct in context">Correct in context</option>
              <option value="Rep/community preference">Rep/community preference</option>
              <option value="Already addressed elsewhere">Already addressed elsewhere</option>
              <option value="Other">Other (explain below)</option>
            </select>
            <textarea class="ticket-ack-detail" id="ack-detail-${i}" placeholder="Free-text detail (required if 'Other')" style="display:none;margin-top:6px;"></textarea>
            <div class="ticket-flag-actions" style="margin-top:8px;">
              <button class="btn-flag-ticket-confirm" onclick="confirmAcknowledge(event,'${escHtml(t.ticket_id)}',${i})">Confirm Acknowledge</button>
              <button class="btn-flag-ticket-cancel"  onclick="cancelAcknowledge(event,${i})">Cancel</button>
              <span class="resolve-status" id="ack-status-${i}"></span>
            </div>
          </div>
        </div>
      </td>
    </tr>`;
  });
  html += '</tbody></table>';
  document.getElementById('tickets-body').innerHTML = html;
}

function offText(t) {
  const o = t.offending_text || '';
  try { const p = JSON.parse(o); return p.text || ''; } catch(e) { return o; }
}
function truncate(s, n) { return s.length > n ? s.slice(0,n)+'…' : s; }
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Audit-trail attribution (Phase 3, 2026-04-27) ────────────────────────────
// Backed by tickets.last_action_* (denorm cache, populated by
// db.record_ticket_action) and /api/tickets/<id>/history (full timeline).

function _formatActionVerb(action) {
  const map = {
    resolve:        'Resolved',
    acknowledge:    'Acknowledged',
    flag_incorrect: 'Flagged as incorrect',
    restore:        'Restored',
    archive:        'Archived',
    notes_update:   'Notes edited',
    note_create:    'Note added',
    note_edit:      'Note edited',
    note_delete:    'Note deleted',
  };
  return map[action] || action;
}

function _formatRelativeTime(iso) {
  // Audit timestamps are stored as UTC ISO strings without a timezone suffix
  // (e.g. "2026-04-27T19:43:09"). Treat them as UTC explicitly so the
  // browser converts to local time for display.
  if (!iso) return '';
  const t = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
  if (isNaN(t.getTime())) return iso;
  const diffSec = Math.max(0, (Date.now() - t.getTime()) / 1000);
  if (diffSec < 60)        return 'just now';
  if (diffSec < 3600)      return Math.floor(diffSec / 60) + ' min ago';
  if (diffSec < 86400)     return Math.floor(diffSec / 3600) + ' hr ago';
  if (diffSec < 86400 * 7) return Math.floor(diffSec / 86400) + ' days ago';
  return t.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function renderLastAction(t, idx) {
  if (!t.last_action || !t.last_action_at) return '';
  const verb = _formatActionVerb(t.last_action);
  const who  = t.last_action_by_name || t.last_action_by_email || 'Unknown user';
  const when = _formatRelativeTime(t.last_action_at);
  return `<div class="last-action-line">
    <span class="last-action-text">${escHtml(verb)} by <strong>${escHtml(who)}</strong> · ${escHtml(when)}</span>
    <button class="btn-history-toggle" onclick="toggleTicketHistory('${escHtml(t.ticket_id)}', ${idx})">Show history</button>
  </div>`;
}

async function toggleTicketHistory(ticketId, idx) {
  const block = document.getElementById('hist-block-' + idx);
  const btn   = document.querySelector(`.ticket-row[data-idx="${idx}"] + .detail-row .btn-history-toggle`);
  if (!block) return;
  const opening = block.style.display === 'none';
  if (!opening) {
    block.style.display = 'none';
    if (btn) btn.textContent = 'Show history';
    return;
  }
  block.style.display = 'block';
  if (btn) btn.textContent = 'Hide history';
  const list = document.getElementById('hist-list-' + idx);
  if (!list) return;
  list.innerHTML = '<div class="hist-empty">Loading…</div>';
  try {
    const r = await fetch('/api/tickets/' + encodeURIComponent(ticketId) + '/history');
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Load failed');
    const events = d.events || [];
    if (!events.length) {
      list.innerHTML = '<div class="hist-empty">No recorded actions on this ticket.</div>';
      return;
    }
    list.innerHTML = events.map(renderHistoryEvent).join('');
  } catch (e) {
    list.innerHTML = '<div class="hist-empty" style="color:#991b1b">Error: ' + escHtml(e.message) + '</div>';
  }
}

function renderHistoryEvent(ev) {
  const who  = ev.actor_name || ev.actor_email_snapshot || 'Unknown user';
  const verb = _formatActionVerb(ev.action);
  const when = ev.created_at ? ev.created_at.slice(0, 16).replace('T', ' ') + ' UTC' : '';
  // Surface the most useful payload bits without dumping raw JSON in the UI.
  let detail = '';
  const p = ev.payload || {};
  if (p.reason) {
    detail = ' — ' + escHtml(p.reason);
  } else if (p.prior_status && p.new_status) {
    detail = ' (' + escHtml(p.prior_status) + ' → ' + escHtml(p.new_status) + ')';
  } else if (ev.action === 'note_create' && p.note_text) {
    detail = ' — ' + escHtml(truncate(p.note_text, 80));
  } else if (ev.action === 'note_edit' && p.new_text) {
    detail = ' — ' + escHtml(truncate(p.new_text, 80));
  }
  return `<div class="hist-row">
    <span class="hist-when">${escHtml(when)}</span>
    <span class="hist-verb">${escHtml(verb)}</span>
    <span class="hist-actor">by <strong>${escHtml(who)}</strong></span>
    <span class="hist-detail">${detail}</span>
  </div>`;
}

// ── Template rollup helpers (2026-04-17) ──────────────────────────────────────

function _templateGroupTicketIds(gi) {
  const el = document.getElementById('tmpl-rollup-data-' + gi);
  if (!el) return [];
  try { return JSON.parse(el.textContent); } catch(e) { return []; }
}

function toggleTemplateRollup(gi) {
  const card = document.getElementById('tmpl-rollup-' + gi);
  const body = document.getElementById('tmpl-rollup-body-' + gi);
  if (!card || !body) return;
  const ids = _templateGroupTicketIds(gi);
  const open = body.style.display !== 'none';
  if (open) {
    body.style.display = 'none';
    card.classList.remove('open');
    return;
  }
  // Lazy-render child list on first expand
  if (!body.dataset.rendered) {
    const children = ids
      .map(id => _tickets.find(t => t.ticket_id === id))
      .filter(Boolean);
    let html = '<ul class="template-rollup-children">';
    children.forEach(t => {
      const resolved = t.status === 'Resolved';
      const flagged  = t.status === 'Flagged Incorrectly';
      const stateTag = resolved ? ' <span style="color:#065f46;font-weight:600">\u2713 Resolved</span>'
                       : flagged ? ' <span style="color:#856404;font-weight:600">\u2691 Flagged</span>'
                       : '';
      const urlLink = t.job_url
        ? ` <a href="${escHtml(t.job_url)}" target="_blank" style="font-size:11px;color:#2563eb" onclick="event.stopPropagation()">View \u2197</a>`
        : '';
      html += `<li>
        <span style="font-family:monospace;color:#888">${escHtml(t.ticket_id)}</span>
        <span style="font-weight:600;margin-left:8px">${escHtml(t.community || '')}</span>
        <span style="color:#555;margin-left:8px">${escHtml(truncate(t.job_title || '', 40))}</span>${urlLink}${stateTag}
      </li>`;
    });
    html += '</ul>';
    body.innerHTML = html;
    body.dataset.rendered = '1';
  }
  body.style.display = '';
  card.classList.add('open');
}

// ── Capture as Template (Root Cause Clusters Phase 2, 2026-04-21) ──────────
// Opens the capture modal for a rollup group. Modal state is held in
// _captureContext so confirmCaptureAsTemplate() can POST without re-reading
// the DOM. See tickets.html #capture-modal and CLAUDE.md "Templates Category"
// for the full data flow.
let _captureContext = null;

function _ticketSeverityRank(sev) {
  // Same ranking the server uses in db.capture_template (highest-wins).
  return ({ CRITICAL: 4, HIGH: 3, MEDIUM: 2, LOW: 1 })[(sev || '').toUpperCase()] || 0;
}

function captureAsTemplate(gi) {
  const ids = _templateGroupTicketIds(gi);
  if (!ids.length) return;
  // Only capture tickets in active statuses \u2014 matches the server-side
  // eligibility filter in db.capture_template.
  const activeTickets = ids
    .map(id => _tickets.find(x => x.ticket_id === id))
    .filter(t => t && !['Resolved', 'Flagged Incorrectly', 'Archived', 'Acknowledged'].includes(t.status));
  if (!activeTickets.length) {
    showToast('error', 'No eligible tickets in this group (all are resolved / archived / acknowledged).');
    return;
  }
  const sample  = activeTickets[0];
  const pattern = (sample.offending_text || sample.offending || '').trim();
  if (!pattern) {
    showToast('error', 'This group has no offending text to capture.');
    return;
  }
  // Highest-wins severity mirrors the server calculation.
  let bestRank = 0;
  let bestSev  = 'MEDIUM';
  activeTickets.forEach(t => {
    const rk = _ticketSeverityRank(t.severity);
    if (rk > bestRank) { bestRank = rk; bestSev = (t.severity || '').toUpperCase(); }
  });
  // Suggested name: first 60 chars of pattern, admin-editable.
  const suggestedName = pattern.length > 60 ? pattern.slice(0, 60).trim() : pattern;

  _captureContext = {
    gi,
    pattern,
    ticketIds: activeTickets.map(t => t.ticket_id),
    severity:  bestSev,
  };

  const modal   = document.getElementById('capture-modal');
  const nameEl  = document.getElementById('capture-name');
  const patEl   = document.getElementById('capture-pattern-preview');
  const cntEl   = document.getElementById('capture-ticket-count');
  const sevEl   = document.getElementById('capture-severity-preview');
  const statusEl = document.getElementById('capture-status');
  const confirmBtn = document.getElementById('capture-confirm-btn');
  if (nameEl)    nameEl.value = suggestedName;
  if (patEl)     patEl.textContent = pattern;
  if (cntEl)     cntEl.textContent = `${activeTickets.length} ticket${activeTickets.length !== 1 ? 's' : ''}`;
  if (sevEl) {
    sevEl.innerHTML = `<span class="sev sev-${escHtml(bestSev)}">${escHtml(bestSev)}</span>`;
  }
  if (statusEl) { statusEl.textContent = ''; statusEl.className = 'capture-modal-status'; }
  if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.textContent = 'Capture'; }
  if (modal) { modal.style.display = 'flex'; }
  // Focus the name field after the modal paints so the admin can edit it.
  setTimeout(() => { if (nameEl) { nameEl.focus(); nameEl.select(); } }, 50);
}

function closeCaptureModal() {
  const modal = document.getElementById('capture-modal');
  if (modal) modal.style.display = 'none';
  _captureContext = null;
}

async function confirmCaptureAsTemplate() {
  if (!_captureContext) return;
  const { gi, pattern, ticketIds } = _captureContext;
  const nameEl    = document.getElementById('capture-name');
  const statusEl  = document.getElementById('capture-status');
  const confirmBtn = document.getElementById('capture-confirm-btn');
  const templateName = (nameEl?.value || '').trim();
  if (!templateName) {
    if (statusEl) { statusEl.textContent = 'Give the template a name first.'; statusEl.className = 'capture-modal-status error'; }
    if (nameEl) nameEl.focus();
    return;
  }
  if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.textContent = 'Capturing\u2026'; }
  if (statusEl)   { statusEl.textContent = 'Saving\u2026'; statusEl.className = 'capture-modal-status'; }
  try {
    const r = await fetch('/api/templates/capture', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        pattern,
        template_name: templateName,
        ticket_ids: ticketIds,
      }),
    });
    const data = await r.json();
    if (!r.ok || data.error) throw new Error(data.error || 'Server error');
    // Success \u2014 fade out the rollup card, show toast, close modal.
    const card = document.getElementById('tmpl-rollup-' + gi);
    closeCaptureModal();
    showToast(
      'success',
      `Captured as "Templates / ${data.template_pair[1]}" \u2014 ${data.rewritten_count} ticket(s) hidden from Live Tickets. Still visible on the Community page.`
    );
    if (card) {
      // Anchor-scroll pattern mirroring _beginRowRemoval: snapshot the next
      // visible sibling's top, fade the card, then preserve that sibling's
      // viewport y after removal. Keeps the admin's eye on the same spot.
      let anchor = card.nextElementSibling;
      while (anchor && anchor.offsetParent === null) anchor = anchor.nextElementSibling;
      const anchorTop = anchor ? anchor.getBoundingClientRect().top : null;
      card.style.transition = 'opacity 0.4s';
      card.style.opacity    = '0';
      setTimeout(() => {
        card.remove();
        if (anchor && anchorTop !== null) {
          const delta = anchor.getBoundingClientRect().top - anchorTop;
          if (Math.abs(delta) > 1) window.scrollBy({ top: delta, behavior: 'auto' });
        }
        // Refresh so the captured tickets disappear from the flat list too
        // (the server rewrote their pair; a simple DOM remove wouldn't catch
        // those because they live outside the rollup card).
        loadTickets();
      }, 420);
    } else {
      loadTickets();
    }
  } catch (e) {
    if (statusEl) { statusEl.textContent = 'Capture failed: ' + e.message; statusEl.className = 'capture-modal-status error'; }
    if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.textContent = 'Capture'; }
  }
}


async function resolveTemplateGroup(gi) {
  const ids = _templateGroupTicketIds(gi);
  if (!ids.length) return;
  // Only resolve tickets that are currently Open — skip Resolved / Flagged
  const openIds = ids.filter(id => {
    const t = _tickets.find(x => x.ticket_id === id);
    return t && t.status !== 'Resolved' && t.status !== 'Flagged Incorrectly';
  });
  if (!openIds.length) {
    showToast('success', 'Nothing to resolve in this group — all tickets already resolved or flagged.');
    return;
  }
  const label = `${openIds.length} ticket${openIds.length !== 1 ? 's' : ''}`;
  if (!confirm(`Resolve ${label} in this group?\n\nThis marks every open ticket with the same offending text as Resolved in one batch. Make sure the pattern is really the shared root cause before confirming.`)) return;
  try {
    const r = await fetch('/api/tickets/bulk-resolve', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticket_ids: openIds, status: 'Resolved',
                             note: 'Bulk-resolved via template rollup' }),
    });
    const data = await r.json();
    if (!r.ok || data.error) throw new Error(data.error || 'Server error');
    showToast('success', `Resolved ${data.updated || openIds.length} ticket${(data.updated || openIds.length) !== 1 ? 's' : ''} in group.`);
    // Refresh the ticket list so the group disappears from the Open view
    loadTickets();
  } catch(e) {
    showToast('error', 'Bulk resolve failed: ' + e.message);
  }
}

// ── Expand / collapse ────────────────────────────────────────────────────────
let _openIdx = null;
function toggleRow(idx) {
  if (_openIdx !== null && _openIdx !== idx) {
    document.querySelector(`.ticket-row[data-idx="${_openIdx}"]`)?.classList.remove('expanded');
    const prev = document.getElementById('detail-' + _openIdx);
    if (prev) prev.style.display = 'none';
  }
  const row    = document.querySelector(`.ticket-row[data-idx="${idx}"]`);
  const detail = document.getElementById('detail-' + idx);
  if (!row || !detail) return;
  const opening = detail.style.display === 'none';
  detail.style.display = opening ? 'table-row' : 'none';
  row.classList.toggle('expanded', opening);
  _openIdx = opening ? idx : null;
  if (opening) {
    // Keep the just-opened row visible without yanking the viewport. If the
    // row or its new detail panel is partially off-screen, scrollIntoView
    // with block:'nearest' makes a minimum adjustment — no jerk when the
    // row is already well within view.
    row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    // Tiny background-flash so the eye tracks the state change — 2026-04-21
    // (Geoff feedback: "I always lose my place"). Purely cosmetic; the
    // CSS keyframe 'row-flash' lives in tickets.css.
    row.classList.remove('just-opened');  // retrigger if already set
    // Force reflow so the animation restarts cleanly.
    void row.offsetWidth;
    row.classList.add('just-opened');
    const cb = row.querySelector('.ticket-cb');
    const tid = cb?.dataset?.tid || row.querySelector('td:nth-child(3)')?.textContent?.trim();
    if (tid) loadTicketNotes(tid, idx);
  }
}

// ── Scroll anchoring for row removals (Migration 2026-04-21 UX polish) ─────
// When a ticket is resolved/flagged/acknowledged, it fades out and every row
// below shifts up. Without anchoring, the user loses their place — the ticket
// they were about to click next has moved. _beginRowRemoval snapshots the
// next-surviving row's viewport offset; _endRowRemoval restores that offset
// after the DOM settles, so the user's eye stays on the same spot on screen.
function _beginRowRemoval(idx) {
  const removedRow = document.querySelector(`.ticket-row[data-idx="${idx}"]`);
  if (!removedRow) return null;
  // Find the next data row that isn't about to be removed.
  let cursor = removedRow.nextElementSibling;
  while (cursor && (!cursor.classList.contains('ticket-row') || cursor.style.opacity === '0')) {
    cursor = cursor.nextElementSibling;
  }
  if (!cursor) return null;
  return { anchorRow: cursor, anchorTop: cursor.getBoundingClientRect().top };
}

function _endRowRemoval(snapshot) {
  if (!snapshot || !snapshot.anchorRow) return;
  const newTop = snapshot.anchorRow.getBoundingClientRect().top;
  const delta  = newTop - snapshot.anchorTop;
  if (Math.abs(delta) > 1) {
    // Adjust scroll so the anchor row appears at the same y it was before.
    window.scrollBy({ top: delta, behavior: 'auto' });
  }
}

// ── Resolve ticket ────────────────────────────────────────────────────────────
async function resolveTicket(e, ticketId, idx) {
  e.stopPropagation();
  const btn  = document.getElementById('btn-resolve-' + idx);
  const btnF = document.getElementById('btn-flag-' + idx);
  const rs   = document.getElementById('rs-' + idx);
  btn.disabled = true; if (btnF) btnF.disabled = true;
  rs.textContent = 'Saving…'; rs.className = 'resolve-status';
  try {
    const r = await fetch('/api/tickets/' + ticketId + '/resolve', { method: 'POST' });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || 'Server error');
    rs.textContent = '✓ Resolved'; rs.className = 'resolve-status done';
    const row = document.querySelector(`.ticket-row[data-idx="${idx}"]`);
    if (row) { row.classList.add('resolved'); row.querySelector('td:last-child').textContent = 'Resolved'; }
    btn.style.display = 'none'; if (btnF) btnF.style.display = 'none';
    checkDirty();
  } catch(err) {
    rs.textContent = 'Error: ' + err.message; rs.className = 'resolve-status error';
    btn.disabled = false; if (btnF) btnF.disabled = false;
  }
}

// ── Flag as incorrect ─────────────────────────────────────────────────────────
function flagIncorrect(e, ticketId, idx) {
  e.stopPropagation();
  // Toggle the reason panel open — actual POST happens on Confirm
  const panel = document.getElementById('flag-panel-' + idx);
  const btnF  = document.getElementById('btn-flag-' + idx);
  if (!panel) return;
  const opening = !panel.classList.contains('open');
  panel.classList.toggle('open', opening);
  if (btnF) btnF.textContent = opening ? '✕ Cancel' : 'Flag as Incorrect';
  if (opening) {
    const ta = document.getElementById('flag-notes-' + idx);
    if (ta) { ta.value = ''; ta.focus(); }
  }
}

function cancelFlagIncorrect(e, idx) {
  e.stopPropagation();
  const panel = document.getElementById('flag-panel-' + idx);
  const btnF  = document.getElementById('btn-flag-' + idx);
  if (panel) panel.classList.remove('open');
  if (btnF) btnF.textContent = 'Flag as Incorrect';
}

async function confirmFlagIncorrect(e, ticketId, idx) {
  e.stopPropagation();
  const panel      = document.getElementById('flag-panel-' + idx);
  const btn        = document.getElementById('btn-resolve-' + idx);
  const btnF       = document.getElementById('btn-flag-' + idx);
  const rs         = document.getElementById('rs-' + idx);
  const reason     = (document.getElementById('flag-notes-' + idx)?.value || '').trim();
  const confirmBtn = panel?.querySelector('.btn-flag-ticket-confirm');

  if (confirmBtn) confirmBtn.disabled = true;
  if (btnF) btnF.disabled = true;
  if (rs) { rs.textContent = 'Saving…'; rs.className = 'resolve-status'; }

  try {
    const r = await fetch('/api/tickets/' + ticketId + '/flag-incorrect', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ reason })
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || 'Server error');
    if (rs) { rs.textContent = '⚑ Flagged as Incorrect'; rs.className = 'resolve-status flagged'; }
    if (btn) btn.style.display = 'none';
    if (btnF) btnF.style.display = 'none';
    if (panel) panel.style.display = 'none';
    const row = document.querySelector(`.ticket-row[data-idx="${idx}"]`);
    if (row) {
      const anchor = _beginRowRemoval(idx);
      row.style.transition = 'opacity 0.4s';
      row.style.opacity = '0';
      setTimeout(() => { row.remove(); _endRowRemoval(anchor); }, 420);
    }
  } catch(err) {
    if (rs) { rs.textContent = 'Error: ' + err.message; rs.className = 'resolve-status error'; }
    if (confirmBtn) confirmBtn.disabled = false;
    if (btnF) { btnF.disabled = false; btnF.textContent = 'Flag as Incorrect'; }
    if (panel) panel.classList.remove('open');
  }
}

// checkDirty() removed April 2026 along with email generator. Kept as a no-op
// so any remaining callers (e.g. after approving a ticket) don't blow up.
async function checkDirty() { /* no-op */ }

// ── Acknowledge (Medium/Low only, Migration 2026-04-20) ─────────────────────
function acknowledgeTicket(e, ticketId, idx) {
  e.stopPropagation();
  const panel = document.getElementById('ack-panel-' + idx);
  const btn   = document.getElementById('btn-ack-' + idx);
  if (!panel) return;
  const opening = !panel.classList.contains('open');
  panel.classList.toggle('open', opening);
  if (btn) btn.textContent = opening ? '✕ Cancel' : 'Acknowledge';
  if (opening) {
    const sel = document.getElementById('ack-cat-' + idx);
    if (sel) { sel.value = ''; sel.focus(); }
    const det = document.getElementById('ack-detail-' + idx);
    if (det) { det.value = ''; det.style.display = 'none'; }
    const st = document.getElementById('ack-status-' + idx);
    if (st) { st.textContent = ''; st.className = 'resolve-status'; }
  }
}

function cancelAcknowledge(e, idx) {
  e.stopPropagation();
  const panel = document.getElementById('ack-panel-' + idx);
  const btn   = document.getElementById('btn-ack-' + idx);
  if (panel) panel.classList.remove('open');
  if (btn) btn.textContent = 'Acknowledge';
}

function _ackCategoryChanged(idx) {
  const sel = document.getElementById('ack-cat-' + idx);
  const det = document.getElementById('ack-detail-' + idx);
  if (!sel || !det) return;
  if (sel.value === 'Other') {
    det.style.display = 'block';
    det.focus();
  } else {
    det.style.display = 'none';
  }
}

async function confirmAcknowledge(e, ticketId, idx) {
  e.stopPropagation();
  const sel = document.getElementById('ack-cat-' + idx);
  const det = document.getElementById('ack-detail-' + idx);
  const st  = document.getElementById('ack-status-' + idx);
  const category = sel ? sel.value : '';
  const detail   = det ? det.value.trim() : '';
  if (!category) {
    if (st) { st.textContent = 'Pick a reason first.'; st.className = 'resolve-status error'; }
    return;
  }
  if (category === 'Other' && !detail) {
    if (st) { st.textContent = 'Explain the reason.'; st.className = 'resolve-status error'; }
    return;
  }
  if (st) { st.textContent = 'Saving…'; st.className = 'resolve-status'; }
  try {
    const r = await fetch('/api/tickets/' + ticketId + '/acknowledge', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ reason_category: category, reason_detail: detail })
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || 'Server error');
    // Fade out the row — matches the Flag-Incorrect UX. Anchor scroll so
    // the user's next-target row stays at the same y.
    const row = document.querySelector(`.ticket-row[data-idx="${idx}"]`);
    if (row) {
      const anchor = _beginRowRemoval(idx);
      row.style.transition = 'opacity 0.4s';
      row.style.opacity = '0';
      setTimeout(() => { row.remove(); _endRowRemoval(anchor); }, 420);
    }
  } catch (err) {
    if (st) { st.textContent = 'Error: ' + err.message; st.className = 'resolve-status error'; }
  }
}

async function reopenTicket(e, ticketId, idx) {
  e.stopPropagation();
  if (!confirm('Reopen this ticket?')) return;
  try {
    const r = await fetch('/api/tickets/' + ticketId + '/restore', { method: 'POST' });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || 'Server error');
    location.reload();
  } catch (err) {
    alert('Reopen failed: ' + err.message);
  }
}

// ── Ticket notes (Tickets-page admin view, Migration 2026-04-20) ───────────
async function loadTicketNotes(tid, idx) {
  const list  = document.getElementById('notes-list-' + idx);
  const count = document.getElementById('notes-count-' + idx);
  if (!list) return;
  list.innerHTML = '<div class="ticket-notes-empty">Loading&hellip;</div>';
  try {
    const r = await fetch('/api/tickets/' + encodeURIComponent(tid) + '/notes');
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Load failed');
    const notes = d.notes || [];
    if (count) count.textContent = '· ' + notes.length;
    if (!notes.length) {
      list.innerHTML = '<div class="ticket-notes-empty">No notes yet.</div>';
      return;
    }
    list.innerHTML = notes.map(n => renderTicketNote(tid, idx, n)).join('');
  } catch (e) {
    list.innerHTML = '<div class="ticket-notes-empty" style="color:#991b1b">Error: ' + escHtml(e.message) + '</div>';
  }
}

function renderTicketNote(tid, idx, n) {
  const display = n.user_name || n.user_email || 'Unknown user';
  const when = n.created_at ? n.created_at.slice(0,16).replace('T',' ') + ' UTC' : '';
  const edited = n.updated_at ? ' · edited' : '';
  // All admins on Tickets page can edit/delete — the page itself is admin-only.
  return `<div class="ticket-note" id="tnote-${n.id}">
    <div class="ticket-note-meta">
      <span><strong>${escHtml(display)}</strong> · ${escHtml(when)}${edited}</span>
      <span>
        <button class="btn-note-act" onclick="editTicketNote('${tid}',${idx},${n.id})">Edit</button>
        <button class="btn-note-act danger" onclick="deleteTicketNote('${tid}',${idx},${n.id})">Delete</button>
      </span>
    </div>
    <div class="ticket-note-text" id="tnote-text-${n.id}">${escHtml(n.note_text || '')}</div>
  </div>`;
}

async function addTicketNote(tid, idx) {
  const ta = document.getElementById('note-new-' + idx);
  const text = (ta?.value || '').trim();
  if (!text) return;
  try {
    const r = await fetch('/api/tickets/' + encodeURIComponent(tid) + '/notes', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ note_text: text })
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Save failed');
    ta.value = '';
    await loadTicketNotes(tid, idx);
  } catch (e) {
    alert('Add note failed: ' + e.message);
  }
}

function editTicketNote(tid, idx, noteId) {
  const textDiv = document.getElementById('tnote-text-' + noteId);
  if (!textDiv) return;
  const original = textDiv.textContent;
  textDiv.innerHTML = `
    <textarea class="ticket-note-edit" id="tnote-edit-${noteId}" rows="2">${escHtml(original)}</textarea>
    <div style="margin-top:6px;display:flex;gap:6px;">
      <button class="btn-notes-post" onclick="saveEditTicketNote('${tid}',${idx},${noteId})">Save</button>
      <button class="btn-note-act" onclick="cancelEditTicketNote(${noteId}, ${JSON.stringify(original)})">Cancel</button>
    </div>`;
  document.getElementById('tnote-edit-' + noteId)?.focus();
}

function cancelEditTicketNote(noteId, original) {
  const textDiv = document.getElementById('tnote-text-' + noteId);
  if (textDiv) textDiv.textContent = original;
}

async function saveEditTicketNote(tid, idx, noteId) {
  const input = document.getElementById('tnote-edit-' + noteId);
  const text = (input?.value || '').trim();
  if (!text) return;
  try {
    const r = await fetch('/api/tickets/' + encodeURIComponent(tid) + '/notes/' + noteId, {
      method: 'PATCH', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ note_text: text })
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Save failed');
    await loadTicketNotes(tid, idx);
  } catch (e) {
    alert('Edit failed: ' + e.message);
  }
}

async function deleteTicketNote(tid, idx, noteId) {
  if (!confirm('Delete this note?')) return;
  try {
    const r = await fetch('/api/tickets/' + encodeURIComponent(tid) + '/notes/' + noteId, { method: 'DELETE' });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Delete failed');
    await loadTicketNotes(tid, idx);
  } catch (e) {
    alert('Delete failed: ' + e.message);
  }
}

// ── Bulk resolve ─────────────────────────────────────────────────────────────

function getSelectedIds() {
  return [...document.querySelectorAll('.ticket-cb:checked')].map(cb => cb.dataset.tid);
}

function updateBulkBar() {
  const ids  = getSelectedIds();
  const bar  = document.getElementById('bulk-bar');
  const cnt  = document.getElementById('bulk-count');
  if (ids.length) {
    bar.style.display = 'flex';
    cnt.textContent   = ids.length + ' selected';
  } else {
    bar.style.display = 'none';
  }
}

function toggleSelectAll(master) {
  document.querySelectorAll('.ticket-cb').forEach(cb => { cb.checked = master.checked; });
  updateBulkBar();
}

function clearSelection() {
  document.querySelectorAll('.ticket-cb').forEach(cb => { cb.checked = false; });
  const sa = document.getElementById('select-all');
  if (sa) sa.checked = false;
  updateBulkBar();
}

async function bulkResolve() {
  const ids = getSelectedIds();
  if (!ids.length) return;
  if (!confirm('Resolve ' + ids.length + ' ticket' + (ids.length > 1 ? 's' : '') + '?')) return;

  const bar = document.getElementById('bulk-bar');
  const cnt = document.getElementById('bulk-count');
  cnt.textContent = 'Resolving…';

  try {
    const r = await fetch('/api/tickets/bulk-resolve', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ticket_ids: ids }),
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || 'Server error');

    // Update rows in place
    ids.forEach(tid => {
      const cb = document.querySelector(`.ticket-cb[data-tid="${tid}"]`);
      if (!cb) return;
      const idx = cb.dataset.idx;
      const row = document.querySelector(`.ticket-row[data-idx="${idx}"]`);
      if (row) {
        row.classList.add('resolved');
        row.querySelector('td:last-child').textContent = 'Resolved';
        cb.remove();  // no longer selectable
      }
    });
    clearSelection();
    cnt.textContent = d.resolved + ' resolved';
    setTimeout(() => { bar.style.display = 'none'; }, 2000);
  } catch(e) {
    cnt.textContent = 'Error: ' + e.message;
  }
}

// ── Mode toggle ───────────────────────────────────────────────────────────────
let _currentMode = 'live';

function setMode(mode) {
  // Pending Review was removed 2026-04-16 when pending_tickets retired —
  // guard every lookup so a missing element doesn't crash the function.
  _currentMode = mode;
  const liveView     = document.getElementById('live-view');
  const rejectedView = document.getElementById('rejected-view');
  const pendingView  = document.getElementById('pending-view');   // may be null
  const btnLive      = document.getElementById('btn-mode-live');
  const btnRejected  = document.getElementById('btn-mode-rejected');
  const btnPending   = document.getElementById('btn-mode-pending'); // may be null

  if (liveView)     liveView.style.display     = mode === 'live'     ? '' : 'none';
  if (pendingView)  pendingView.style.display  = mode === 'pending'  ? '' : 'none';
  if (rejectedView) rejectedView.style.display = mode === 'rejected' ? '' : 'none';
  if (btnLive)      btnLive.classList.toggle('active',     mode === 'live');
  if (btnPending)   btnPending.classList.toggle('active',  mode === 'pending');
  if (btnRejected)  btnRejected.classList.toggle('active', mode === 'rejected');

  if (mode === 'pending'  && typeof loadPending  === 'function') loadPending();
  if (mode === 'rejected' && typeof loadRejected === 'function') loadRejected();
}

// ── Pending review ────────────────────────────────────────────────────────────

// Cached taxonomy — refreshed each time Pending Review is opened.
let _taxonomy = {
  canonical_areas: [], custom_areas: [], check_types: [],
  issue_types: [], aliases: []
};
// Fast lookup: "category::issue_type" lowercased → canonical check_type
let _ctIndex = {};

function _buildCtIndex(taxonomy) {
  const idx = {};
  (taxonomy.check_types || []).forEach(c => {
    const key = ((c.category || '') + '::' + (c.issue_type || '')).toLowerCase().trim();
    idx[key] = c.check_type;
  });
  return idx;
}

async function loadPending() {
  document.getElementById('pending-body').innerHTML = '<div class="loading">Loading…</div>';
  try {
    // Fetch pending + taxonomy in parallel; taxonomy powers the datalist,
    // merge warnings, and the custom-category picker.
    const [pendingR, taxR] = await Promise.all([
      fetch('/api/pending-tickets'),
      fetch('/api/taxonomy')
    ]);
    const groups = await pendingR.json();
    _taxonomy    = await taxR.json();
    _ctIndex     = _buildCtIndex(_taxonomy);

    // Populate the shared <datalist> for Issue Type inputs
    const dl = document.getElementById('dl-issue-types');
    if (dl) {
      dl.innerHTML = (_taxonomy.issue_types || [])
        .map(it => '<option value="' + escHtml(it) + '">').join('');
    }
    renderPending(groups);
  } catch(e) {
    document.getElementById('pending-body').innerHTML =
      '<div class="pending-empty">Error loading pending tickets: ' + e.message + '</div>';
  }
}

// Lookup maps populated by renderPending — no strings passed inline in onclick.
const _pendingCts     = {};   // gi        → check_type (composed, for display)
const _pendingAreas   = {};   // gi        → original category (for API calls)
const _pendingItypes  = {};   // gi        → original issue_type (for API calls)
const _pendingTids    = {};   // "gi-ti"   → ticket_id

const _KNOWN_AREAS = [
  'Job Title','Community Introduction','Who We Are',
  'What We Offer \u2014 Pay','What We Offer \u2014 Benefits','What We Offer \u2014 General',
  'Responsibilities','Qualifications','EEO Statement',
  'HTML','Tone','Formatting','Content','Structure'
];
function buildAreaSelect(gi, aiLoc) {
  aiLoc = aiLoc || '';
  const customAreas = (_taxonomy.custom_areas || []).map(c => c.category);
  const allKnown = _KNOWN_AREAS.concat(customAreas);
  const isKnown = allKnown.includes(aiLoc);
  const sel = v => (aiLoc === v) ? ' selected' : '';
  const unknownOpt = (!isKnown && aiLoc)
    ? '<option value="' + escHtml(aiLoc) + '" selected>' + escHtml(aiLoc) + ' (AI suggestion — will be saved as custom)</option>'
    : '';
  const customGroup = customAreas.length
    ? '<optgroup label="Custom Areas">'
      + customAreas.map(a => '<option value="' + escHtml(a) + '"' + sel(a) + '>' + escHtml(a) + '</option>').join('')
      + '</optgroup>'
    : '';
  return '<select class="ctrl-select" id="ps-category-' + gi + '" onchange="onPendingFieldChange(' + gi + ')">'
    + '<option value="">' + (aiLoc ? '' : '\u2014 select \u2014') + '</option>'
    + unknownOpt
    + '<optgroup label="Posting Sections">'
    + '<option value="Job Title"'                   + sel('Job Title')                   + '>Job Title</option>'
    + '<option value="Community Introduction"'      + sel('Community Introduction')      + '>Community Introduction</option>'
    + '<option value="Who We Are"'                  + sel('Who We Are')                  + '>Who We Are</option>'
    + '<option value="What We Offer \u2014 Pay"'    + sel('What We Offer \u2014 Pay')    + '>What We Offer \u2014 Pay</option>'
    + '<option value="What We Offer \u2014 Benefits"' + sel('What We Offer \u2014 Benefits') + '>What We Offer \u2014 Benefits</option>'
    + '<option value="What We Offer \u2014 General"'  + sel('What We Offer \u2014 General')  + '>What We Offer \u2014 General</option>'
    + '<option value="Responsibilities"'            + sel('Responsibilities')            + '>Responsibilities</option>'
    + '<option value="Qualifications"'              + sel('Qualifications')              + '>Qualifications</option>'
    + '<option value="EEO Statement"'               + sel('EEO Statement')               + '>EEO Statement</option>'
    + '</optgroup>'
    + '<optgroup label="Cross-Cutting">'
    + '<option value="HTML"'       + sel('HTML')       + '>HTML</option>'
    + '<option value="Tone"'       + sel('Tone')       + '>Tone</option>'
    + '<option value="Formatting"' + sel('Formatting') + '>Formatting</option>'
    + '<option value="Content"'    + sel('Content')    + '>Content</option>'
    + '<option value="Structure"'  + sel('Structure')  + '>Structure</option>'
    + '</optgroup>'
    + customGroup
    + '<option value="__add_new__">+ Add new Area\u2026</option>'
    + '</select>';
}

/* Called when Area or Issue Type change. Keeps the "Will save as" preview
   and merge-warning in sync, and handles the "+ Add new Area" prompt. */
function onPendingFieldChange(gi) {
  const areaSel = document.getElementById('ps-category-'  + gi);
  const itIn    = document.getElementById('ps-itype-' + gi);
  if (areaSel && areaSel.value === '__add_new__') {
    const newArea = (prompt('New Area name (will be added to the taxonomy):') || '').trim();
    if (newArea) {
      // Optimistically add to the local taxonomy so subsequent renders include it
      _taxonomy.custom_areas = _taxonomy.custom_areas || [];
      if (!_taxonomy.custom_areas.find(c => c.category === newArea)) {
        _taxonomy.custom_areas.push({ category: newArea });
      }
      // Persist to backend (non-blocking)
      fetch('/api/custom-areas', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ category: newArea })
      }).catch(() => {});
      // Re-render the select with the new option selected
      const parent = areaSel.parentElement;
      parent.innerHTML = buildAreaSelect(gi, newArea);
    } else {
      areaSel.value = '';
    }
  }
  _updatePendingPreview(gi);
}

function _updatePendingPreview(gi) {
  const areaSel = document.getElementById('ps-category-'  + gi);
  const itIn    = document.getElementById('ps-itype-' + gi);
  const prev    = document.getElementById('ps-preview-' + gi);
  if (!areaSel || !itIn || !prev) return;
  const category  = (areaSel.value || '').trim();
  const itype = (itIn.value    || '').trim();
  if (!category || !itype) {
    prev.innerHTML = '<span style="color:#999">Will save as: <em>pick an Area and Issue Type</em></span>';
    return;
  }
  const composite = category + ' \u2014 ' + itype;
  const key = (category + '::' + itype).toLowerCase().trim();
  const existing = _ctIndex[key];
  if (existing) {
    prev.innerHTML =
      '<span style="color:#92400e">\u26A0 Will MERGE into existing: <strong>' + escHtml(existing) + '</strong> '
      + '<span style="color:#b45309;font-weight:400">(pending tickets will be promoted under this canonical check type)</span></span>';
  } else {
    prev.innerHTML = 'Will save as: <strong>' + escHtml(composite) + '</strong>'
      + ' <span style="color:#666;font-weight:400">(new check type)</span>';
  }
}

/* Auto-fill Area + Issue Type from the AI-suggested closest match
   so approving this pending group will merge into that existing type. */
function applyClosestMatch(gi, category, itype) {
  if (!category && !itype) return;
  const areaSel = document.getElementById('ps-category-'  + gi);
  const itIn    = document.getElementById('ps-itype-' + gi);
  if (areaSel && category) {
    // If the category isn't in the dropdown yet, add it so selection sticks.
    const has = Array.from(areaSel.options).some(o => o.value === category);
    if (!has) {
      const opt = document.createElement('option');
      opt.value = category;
      opt.textContent = category;
      // Insert before the "+ Add new Area" option if present
      const addNew = Array.from(areaSel.options).find(o => o.value === '__add_new__');
      if (addNew) areaSel.insertBefore(opt, addNew);
      else        areaSel.appendChild(opt);
    }
    areaSel.value = category;
  }
  if (itIn) itIn.value = itype;
  _updatePendingPreview(gi);
}

function renderPending(groups) {
  // Update badge
  const total = groups.reduce((n, g) => n + g.tickets.length, 0);
  const badge = document.getElementById('pending-mode-badge');
  if (total > 0) { badge.textContent = total; badge.style.display = 'inline-block'; }
  else           { badge.style.display = 'none'; }

  if (!groups.length) {
    document.getElementById('pending-body').innerHTML =
      '<div class="pending-empty">✓ No pending tickets — all check types are established.</div>';
    return;
  }

  // Populate lookup maps before building HTML
  groups.forEach((g, gi) => {
    _pendingCts[gi]    = g.check_type;
    _pendingAreas[gi]  = g.category || '';
    _pendingItypes[gi] = g.issue_type || '';
    g.tickets.forEach((t, ti) => { _pendingTids[gi + '-' + ti] = t.ticket_id; });
  });

  let html = '';
  groups.forEach((g, gi) => {
    const ct = g.check_type;
    const ctEsc = escHtml(ct);
    const n = g.tickets.length;

    const arLabel = g.category  ? `<span class="pending-category-badge">${escHtml(g.category)}</span>`  : '';
    // AI closest-match banner — shows when Claude flagged the finding as
    // "new" but nominated a similar existing (category, issue_type) pair. Lets
    // you see at a glance whether this should really be merged rather than
    // accepted as new.  closest_area and closest_issue_type are separate.
    const hasClose = (g.closest_area || '') || (g.closest_issue_type || '');
    const closeHint = hasClose
      ? `<div class="pending-close-hint" style="margin:6px 0 10px;padding:8px 10px;background:#fef3c7;border:1px solid #fcd34d;border-radius:6px;font-size:13px;color:#78350f;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <span><strong>AI suggests this is close to:</strong>
            <code style="background:#fff;padding:2px 6px;border-radius:3px">${escHtml(g.closest_area || '')}</code>
            <span style="color:#92400e;font-weight:600"> / </span>
            <code style="background:#fff;padding:2px 6px;border-radius:3px">${escHtml(g.closest_issue_type || '')}</code></span>
          <button type="button"
                  onclick="applyClosestMatch(${gi}, ${JSON.stringify(g.closest_area||'').replace(/"/g,'&quot;')}, ${JSON.stringify(g.closest_issue_type||'').replace(/"/g,'&quot;')})"
                  style="background:#78350f;color:#fff;border:none;border-radius:5px;padding:4px 10px;font-size:12px;font-weight:700;cursor:pointer">
            Use this match
          </button>
          <span style="color:#92400e;font-size:12px">\u2014 auto-fills Area + Issue Type so approving will merge into the existing type.</span>
        </div>`
      : '';
    html += `<div class="pending-card" id="pcard-${gi}">
      <div class="pending-card-header">
        <h3>${ctEsc}</h3>
        <span class="pending-ct-badge">NEW CHECK TYPE</span>
        ${arLabel}
        <span class="count-badge">${n} ticket${n !== 1 ? 's' : ''}</span>
      </div>
      ${closeHint}`;

    // All tickets — each with its own flag button
    html += `<div class="pending-examples">
      <div class="pending-examples-label">Tickets <span style="font-weight:400;color:#aaa">(flag individual ones that don't apply)</span></div>`;
    g.tickets.forEach((t, ti) => {
      const off = offTextP(t);
      const urlLink = t.job_url
        ? ` &nbsp;·&nbsp; <a class="job-link-sm" href="${escHtml(t.job_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">View ↗</a>`
        : '';
      const key = gi + '-' + ti;
      html += `<div class="pending-example" id="ptrow-${key}">
        <div class="ex-meta">
          <span class="ex-meta-info">
            ${escHtml(t.ticket_id)} &nbsp;·&nbsp; ${escHtml(t.community||'')} &nbsp;·&nbsp; ${escHtml(t.job_title||'')}
            &nbsp;·&nbsp; <span class="sev sev-${escHtml(t.severity)}">${escHtml(t.severity)}</span>${urlLink}
          </span>
          <button class="btn-flag-ticket" id="ptflag-${key}" onclick="toggleTicketFlagPanel(${gi},${ti})">
            ⚑ Flag
          </button>
        </div>
        <div class="ex-summary">${escHtml(t.issue_summary||'')}</div>
        ${off ? `<div class="ex-offending">"${escHtml(off)}"</div>` : ''}
        <div class="ticket-flag-panel" id="ptpanel-${key}">
          <textarea class="ticket-flag-notes" id="ptnotes-${key}"
            placeholder="Why is this finding incorrect? (optional)"></textarea>
          <div class="ticket-flag-actions">
            <button class="btn-flag-ticket-confirm" onclick="confirmFlagPendingTicket(${gi},${ti})">Confirm &amp; Remove</button>
            <button class="btn-flag-ticket-cancel"  onclick="toggleTicketFlagPanel(${gi},${ti})">Cancel</button>
          </div>
        </div>
      </div>`;
    });
    html += `</div>`;

    // Ticket controls form
    // Determine most common severity among this group's tickets for the default
    const sevCounts = {};
    g.tickets.forEach(t => { sevCounts[t.severity] = (sevCounts[t.severity] || 0) + 1; });
    const defaultSev = ['CRITICAL','HIGH','MEDIUM','LOW'].reduce((best, s) =>
      (sevCounts[s] || 0) > (sevCounts[best] || 0) ? s : best, 'MEDIUM');
    const sevOpts = ['CRITICAL','HIGH','MEDIUM','LOW'].map(s =>
      `<option value="${s}"${s === defaultSev ? ' selected' : ''}>${s}</option>`
    ).join('');

    html += `<div class="pending-controls">
      <div class="pending-controls-label">Configure Ticket Controls for This Check Type</div>
      <div class="ctrl-row">
        <div class="ctrl-group" style="min-width:160px">
          <label>Show on Community Page</label>
          <div class="toggle-wrap" style="margin-top:6px">
            <label class="toggle">
              <input type="checkbox" checked id="ps-comm-${gi}" onchange="updatePendingToggle(this,'comm',${gi})">
              <span class="slider"></span>
            </label>
            <span class="toggle-label" id="ps-comm-lbl-${gi}">Visible</span>
          </div>
        </div>
        <div class="ctrl-group" style="min-width:130px">
          <label>Severity <span style="font-weight:400;color:#aaa">(all tickets)</span></label>
          <select class="ctrl-select" id="ps-severity-${gi}">${sevOpts}</select>
        </div>
        <div class="ctrl-group" style="min-width:200px">
          <label>Area <span style="font-weight:400;color:#aaa">(section or domain)</span></label>
          ${buildAreaSelect(gi, g.category)}
        </div>
        <div class="ctrl-group" style="min-width:220px;flex:1">
          <label>Issue Type <span style="font-weight:400;color:#aaa">(what is wrong \u2014 existing types auto-suggest)</span></label>
          <input class="ctrl-input" id="ps-itype-${gi}" type="text" list="dl-issue-types"
                 value="${escHtml(g.issue_type||'')}" placeholder="e.g. Missing Section\u2026"
                 oninput="_updatePendingPreview(${gi})">
        </div>
        <div class="ctrl-group" style="min-width:240px;flex:1">
          <label>Check Type Notes <span style="font-weight:400;color:#aaa">(about the rule, not this ticket)</span></label>
          <textarea class="ctrl-textarea" id="ps-notes-${gi}" rows="3" placeholder="e.g. Fires on wage ranges — ignore if community intentionally posts a band…"></textarea>
        </div>
      </div>
      <div class="pending-preview" id="ps-preview-${gi}"
           style="margin-top:10px;padding:8px 10px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;font-size:13px;color:#374151">
        Will save as: <em style="color:#999">pick an Area and Issue Type</em>
      </div>
      <label class="ps-alias-opt" id="ps-alias-wrap-${gi}"
             style="display:flex;align-items:flex-start;gap:8px;margin-top:8px;font-size:13px;color:#374151;cursor:pointer">
        <input type="checkbox" id="ps-alias-${gi}" checked style="margin-top:3px">
        <span>Record this mapping as a permanent alias
          <span style="color:#6b7280;font-weight:400">\u2014 next time the AI proposes
          <code style="background:#f3f4f6;padding:1px 5px;border-radius:3px">${ctEsc}</code>,
          it will be auto-redirected to the canonical type you chose (no pending review needed).
          Uncheck if this was a one-off remap.</span>
        </span>
      </label>
    </div>`;

    // Action row — gi is the only argument; check type is looked up via _pendingCts[gi]
    html += `<div class="pending-actions">
      <button class="btn-approve" id="pa-approve-${gi}" onclick="approvePending(${gi})">
        Approve &amp; Add to Live Tickets
      </button>
      <button class="btn-flag-incorrect" id="pa-flag-${gi}" onclick="toggleFlagPanel(${gi})">
        Flag as Incorrect
      </button>
      <span class="pending-action-status" id="pa-status-${gi}"></span>
    </div>
    <div class="flag-panel" id="pa-flag-panel-${gi}">
      <label>Why is this check type incorrect or unnecessary?</label>
      <textarea id="pa-flag-notes-${gi}" placeholder="e.g. Claude is over-flagging this — the community intentionally uses this phrasing…"></textarea>
      <div class="flag-panel-actions">
        <button class="btn-flag-confirm" onclick="submitFlagIncorrect(${gi})">Confirm &amp; Discard</button>
        <button class="btn-flag-cancel"  onclick="toggleFlagPanel(${gi})">Cancel</button>
      </div>
    </div>`;

    html += `</div>`;  // end .pending-card
  });

  document.getElementById('pending-body').innerHTML = html;
  // Initialize the "Will save as" preview for every card with its prefilled
  // Area + Issue Type (so merge warnings appear on page load, not only after
  // the user edits).
  groups.forEach((_g, gi) => _updatePendingPreview(gi));
}

function updatePendingToggle(cb, kind, gi) {
  const lbl = document.getElementById('ps-' + kind + '-lbl-' + gi);
  if (!lbl) return;
  if (kind === 'comm')  lbl.textContent = cb.checked ? 'Visible'  : 'Hidden';
}

function toggleTicketFlagPanel(gi, ti) {
  const key   = gi + '-' + ti;
  const panel = document.getElementById('ptpanel-' + key);
  const btn   = document.getElementById('ptflag-'  + key);
  if (!panel) return;
  const opening = !panel.classList.contains('open');
  panel.classList.toggle('open', opening);
  if (btn) btn.textContent = opening ? '✕ Cancel' : '⚑ Flag';
  if (opening) {
    const ta = document.getElementById('ptnotes-' + key);
    if (ta) ta.focus();
  }
}

async function confirmFlagPendingTicket(gi, ti) {
  const key      = gi + '-' + ti;
  const ticketId = _pendingTids[key];
  const panel    = document.getElementById('ptpanel-' + key);
  const btn      = document.getElementById('ptflag-'  + key);
  const row      = document.getElementById('ptrow-'   + key);
  const notes    = (document.getElementById('ptnotes-' + key)?.value || '').trim();
  const confirmBtn = panel?.querySelector('.btn-flag-ticket-confirm');
  if (!ticketId) return;

  if (confirmBtn) confirmBtn.disabled = true;
  if (btn)       btn.disabled = true;

  try {
    const r = await fetch('/api/pending-tickets/' + ticketId + '/flag-incorrect', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ notes })
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || 'Server error');

    // Fade and remove the ticket row
    if (row) { row.style.opacity = '.35'; row.style.pointerEvents = 'none'; }
    if (btn) { btn.textContent = '⚑ Flagged'; }

    _decrementPendingBadge();

    // Update the count badge on the card header
    const card = document.getElementById('pcard-' + gi);
    if (card) {
      const countBadge = card.querySelector('.count-badge');
      const remaining = card.querySelectorAll('.pending-example:not([style*="opacity"])').length - 1;
      if (countBadge) countBadge.textContent = remaining + ' ticket' + (remaining !== 1 ? 's' : '');
      if (remaining <= 0) {
        setTimeout(() => _removePendingCard(gi), 400);
      }
    }
  } catch(err) {
    if (confirmBtn) confirmBtn.disabled = false;
    if (btn) { btn.disabled = false; btn.textContent = '⚑ Flag'; }
    if (panel) panel.classList.remove('open');
    alert('Could not flag ticket: ' + err.message);
  }
}

function offTextP(t) {
  const o = t.offending_text || '';
  try { const p = JSON.parse(o); return p.text || ''; } catch(e) { return o; }
}

function _decrementPendingBadge() {
  const nb = document.getElementById('pending-nav-badge');
  const mb = document.getElementById('pending-mode-badge');
  const cur = parseInt(mb?.textContent || '0') - 1;
  if (nb) { nb.textContent = cur > 0 ? cur : ''; nb.style.display = cur > 0 ? 'inline-block' : 'none'; }
  if (mb) { mb.textContent = cur > 0 ? cur : ''; mb.style.display = cur > 0 ? 'inline-block' : 'none'; }
}

function _removePendingCard(gi) {
  document.getElementById('pcard-' + gi)?.remove();
  if (!document.querySelector('.pending-card')) {
    document.getElementById('pending-body').innerHTML =
      '<div class="pending-empty">✓ No pending tickets — all check types are established.</div>';
  }
}

// ── Rejected view ─────────────────────────────────────────────────────────────

async function loadRejected() {
  const body = document.getElementById('rejected-body');
  body.innerHTML = '<div class="loading">Loading…</div>';
  try {
    const r = await fetch('/api/rejected-tickets');
    const groups = await r.json();
    if (!Array.isArray(groups)) throw new Error(groups.error || 'Unexpected response');

    // Update badge
    const total = groups.reduce((n, g) => n + g.tickets.length, 0);
    const nb = document.getElementById('rejected-mode-badge');
    if (nb) { nb.textContent = total; nb.style.display = total > 0 ? 'inline-block' : 'none'; }

    if (!groups.length) {
      body.innerHTML = '<div class="pending-empty">✓ No rejected tickets.</div>';
      return;
    }
    renderRejected(groups);
  } catch(e) {
    body.innerHTML = '<div class="pending-empty">Error: ' + escHtml(e.message) + '</div>';
  }
}

function renderRejected(groups) {
  const body = document.getElementById('rejected-body');
  const total = groups.reduce((n, g) => n + g.tickets.length, 0);
  let html = '';

  // Toolbar with Clear All
  html += `<div class="rejected-toolbar" style="display:flex;justify-content:flex-end;align-items:center;gap:12px;margin-bottom:12px">
    <span style="color:#888;font-size:13px">${total} ticket${total !== 1 ? 's' : ''} in Rejected</span>
    <button class="btn-sm btn-clear-rejected" onclick="clearAllRejected()"
            style="background:#5c4a1e;color:#fff;border:1px solid #4a3a17;padding:6px 12px;border-radius:4px;cursor:pointer;font-size:13px"
            ${total === 0 ? 'disabled style="opacity:.4;cursor:not-allowed"' : ''}>
      📦 Archive All Rejected
    </button>
  </div>`;

  groups.forEach(g => {
    const count = g.tickets.length;
    html += `<div class="pending-card" id="rcard-${escHtml(g.check_type.replace(/\W/g,'_'))}">
      <div class="pending-card-header">
        <span class="pending-ct">${escHtml(g.check_type)}</span>
        <span class="count-badge">${count} ticket${count !== 1 ? 's' : ''}</span>
      </div>`;

    g.tickets.forEach(t => {
      const off = (() => { try { return JSON.parse(t.offending_text||'{}').text || ''; } catch(e) { return t.offending_text || ''; } })();
      const urlLink = t.job_url ? ` &nbsp;·&nbsp; <a class="job-link-sm" href="${escHtml(t.job_url)}" target="_blank" onclick="event.stopPropagation()">View ↗</a>` : '';
      // Strip the legacy auto-generated flag stamp from the notes textarea.
      // Pre-Part-5 tickets had "Flagged as incorrect <date> | <reason>"
      // appended to notes; post-Part-5 the reason lives in its own column
      // (t.reason) and notes stays clean.
      const notesTxt = (t.notes || '').replace(/\s*\|\s*Flagged as incorrect[^|]*/g,'').trim().replace(/^\|+|\|+$/g,'').trim();
      const safeId   = escHtml(t.ticket_id);

      // Flag-time reason (Migration Part 5, surfaced here 2026-04-21 after
      // Geoff flagged it missing). Read-only — this is the structured "why"
      // captured at flag time. The editable Notes textarea below is for
      // ongoing conversational commentary, a separate signal.
      const reasonBlock = t.reason
        ? `<div class="rejected-reason-block">
             <span class="rejected-reason-label">Flagged reason (captured when flagged):</span>
             <span class="rejected-reason-text">${escHtml(t.reason)}</span>
           </div>`
        : '';

      html += `<div class="pending-example" id="rrow-${safeId}">
        <div class="ex-meta" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:space-between">
          <span class="ex-meta-info" style="flex:1">
            ${safeId} &nbsp;·&nbsp; ${escHtml(t.community||'')} &nbsp;·&nbsp; ${escHtml(t.job_title||'')}
            &nbsp;·&nbsp; <span class="sev sev-${escHtml(t.severity)}">${escHtml(t.severity)}</span>${urlLink}
          </span>
          <span style="display:inline-flex;gap:6px">
            <button class="btn-restore" onclick="restoreTicket('${safeId}', '${escHtml(g.check_type.replace(/[^a-zA-Z0-9]/g,'_'))}')">
              ↩ Restore
            </button>
            <button class="btn-delete-rejected" onclick="deleteRejectedTicket('${safeId}')"
                    style="background:#5c4a1e;color:#fff;border:1px solid #4a3a17;padding:4px 10px;border-radius:4px;cursor:pointer;font-size:12px"
                    title="Archive this ticket (hidden from views but kept in DB)">
              📦 Archive
            </button>
          </span>
        </div>
        <div class="ex-summary">${escHtml(t.issue_summary||'')}</div>
        ${off ? `<div class="ex-offending">"${escHtml(off)}"</div>` : ''}
        ${reasonBlock}
        <div class="rejected-notes-wrap">
          <label class="rejected-notes-label">Notes (add anything further — separate from the reason above)</label>
          <textarea class="rejected-notes-input" id="rnotes-${safeId}" placeholder="Extra commentary, follow-ups, or context…">${escHtml(notesTxt)}</textarea>
          <div style="display:flex;align-items:center;gap:8px;margin-top:4px">
            <button class="btn-save-rnotes" id="rsave-${safeId}" onclick="saveRejectedNotes('${safeId}')">Save Note</button>
            <span class="rnotes-status" id="rstatus-${safeId}"></span>
          </div>
        </div>
      </div>`;
    });

    html += `</div>`;
  });

  body.innerHTML = html;
}

async function saveRejectedNotes(ticketId) {
  const ta     = document.getElementById('rnotes-'   + ticketId);
  const btn    = document.getElementById('rsave-'    + ticketId);
  const status = document.getElementById('rstatus-'  + ticketId);
  const notes  = (ta?.value || '').trim();
  if (btn) btn.disabled = true;
  if (status) { status.textContent = 'Saving…'; status.style.color = '#888'; }
  try {
    const r = await fetch('/api/tickets/' + encodeURIComponent(ticketId) + '/update-notes', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ notes })
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || 'Server error');
    if (status) { status.textContent = '✓ Saved'; status.style.color = '#22863a'; }
    setTimeout(() => { if (status) status.textContent = ''; }, 2500);
  } catch(e) {
    if (status) { status.textContent = 'Error: ' + e.message; status.style.color = '#c0392b'; }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function deleteRejectedTicket(ticketId) {
  if (!confirm('Archive ticket ' + ticketId + '? It will be hidden but kept in the database.')) return;
  const row = document.getElementById('rrow-' + ticketId);
  const btn = row?.querySelector('.btn-delete-rejected');
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    const r = await fetch('/api/tickets/' + encodeURIComponent(ticketId), { method: 'DELETE' });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || 'Server error');
    if (row) { row.style.opacity = '.25'; row.style.pointerEvents = 'none'; }
    if (btn) btn.textContent = '✓ Archived';
    // Decrement the rejected badge
    const nb = document.getElementById('rejected-mode-badge');
    if (nb) {
      const cur = Math.max(0, parseInt(nb.textContent || '0') - 1);
      nb.textContent = cur; nb.style.display = cur > 0 ? 'inline-block' : 'none';
    }
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = '🗑 Delete'; }
    alert('Could not delete ticket: ' + e.message);
  }
}

async function clearAllRejected() {
  const nb  = document.getElementById('rejected-mode-badge');
  const cur = parseInt(nb?.textContent || '0') || 0;
  if (cur === 0) return;
  if (!confirm('Archive ALL ' + cur + ' rejected ticket(s)? They will be hidden from all views but kept in the database.')) return;
  const btn = document.querySelector('.btn-clear-rejected');
  if (btn) { btn.disabled = true; btn.textContent = 'Archiving…'; }
  try {
    const r = await fetch('/api/rejected-tickets/clear', { method: 'POST' });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || 'Server error');
    if (nb) { nb.textContent = '0'; nb.style.display = 'none'; }
    // Reload the rejected view (will show "no rejected tickets" empty state)
    loadRejected();
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = '📦 Archive All Rejected'; }
    alert('Could not archive rejected tickets: ' + e.message);
  }
}

async function restoreTicket(ticketId, cardKey) {
  const row = document.getElementById('rrow-' + ticketId);
  const btn = row?.querySelector('.btn-restore');
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    const r = await fetch('/api/tickets/' + encodeURIComponent(ticketId) + '/restore', { method: 'POST' });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || 'Server error');
    if (row) { row.style.opacity = '.35'; row.style.pointerEvents = 'none'; }
    if (btn) btn.textContent = '✓ Restored';
    // Update badge
    const nb = document.getElementById('rejected-mode-badge');
    if (nb) {
      const cur = Math.max(0, parseInt(nb.textContent || '0') - 1);
      nb.textContent = cur; nb.style.display = cur > 0 ? 'inline-block' : 'none';
    }
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = '↩ Restore'; }
    alert('Could not restore ticket: ' + e.message);
  }
}

async function approvePending(gi) {
  const approveBtn = document.getElementById('pa-approve-' + gi);
  const flagBtn    = document.getElementById('pa-flag-'    + gi);
  const status     = document.getElementById('pa-status-'  + gi);
  approveBtn.disabled = true; if (flagBtn) flagBtn.disabled = true;
  status.textContent = 'Saving…'; status.className = 'pending-action-status';
  try {
    const r = await fetch('/api/pending-tickets/approve', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        check_type:         _pendingCts[gi],           // kept for backward compat
        original_area:      _pendingAreas[gi],         // AI's original category
        original_issue_type: _pendingItypes[gi],       // AI's original issue_type
        show_on_community:  document.getElementById('ps-comm-'     + gi)?.checked ? 1 : 0,
        severity:           document.getElementById('ps-severity-' + gi).value,
        category:               (document.getElementById('ps-category-'    + gi)?.value  || '').trim(),
        issue_type:         (document.getElementById('ps-itype-'   + gi)?.value  || '').trim(),
        notes:              document.getElementById('ps-notes-'    + gi).value,
        record_alias:       document.getElementById('ps-alias-'    + gi)?.checked ? 1 : 0,
      })
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || 'Server error');
    status.textContent = '✓ Approved — tickets moved to live';
    status.className = 'pending-action-status done';
    document.getElementById('pcard-' + gi).style.opacity = '.45';
    approveBtn.style.display = 'none'; if (flagBtn) flagBtn.style.display = 'none';
    _decrementPendingBadge();
    // Remove card from DOM and reload live tickets so they appear immediately
    setTimeout(() => {
      _removePendingCard(gi);
      if (typeof loadTickets === 'function') loadTickets();
    }, 1200);
  } catch(err) {
    status.textContent = 'Error: ' + err.message; status.className = 'pending-action-status error';
    approveBtn.disabled = false; if (flagBtn) flagBtn.disabled = false;
  }
}

function toggleFlagPanel(gi) {
  const panel   = document.getElementById('pa-flag-panel-' + gi);
  const flagBtn = document.getElementById('pa-flag-'       + gi);
  const isOpen  = panel.classList.contains('open');
  panel.classList.toggle('open', !isOpen);
  flagBtn.textContent = isOpen ? 'Flag as Incorrect' : 'Cancel';
}

async function submitFlagIncorrect(gi) {
  const notes      = (document.getElementById('pa-flag-notes-' + gi)?.value || '').trim();
  const confirmBtn = document.querySelector('#pa-flag-panel-' + gi + ' .btn-flag-confirm');
  const cancelBtn  = document.querySelector('#pa-flag-panel-' + gi + ' .btn-flag-cancel');
  const status     = document.getElementById('pa-status-' + gi);
  const approveBtn = document.getElementById('pa-approve-' + gi);
  const flagBtn    = document.getElementById('pa-flag-'    + gi);

  if (confirmBtn) confirmBtn.disabled = true;
  if (cancelBtn)  cancelBtn.disabled  = true;
  if (approveBtn) approveBtn.disabled = true;
  status.textContent = 'Saving…'; status.className = 'pending-action-status';

  try {
    const r = await fetch('/api/pending-tickets/flag-incorrect', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        category:       _pendingAreas[gi],
        issue_type: _pendingItypes[gi],
        notes
      })
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || 'Server error');
    status.textContent = '⚑ Flagged as Incorrect — reason saved';
    status.className = 'pending-action-status flagged';
    document.getElementById('pcard-' + gi).style.opacity = '.45';
    if (flagBtn) flagBtn.style.display = 'none';
    if (approveBtn) approveBtn.style.display = 'none';
    document.getElementById('pa-flag-panel-' + gi).classList.remove('open');
    _decrementPendingBadge();
    setTimeout(() => _removePendingCard(gi), 1800);
  } catch(err) {
    status.textContent = 'Error: ' + err.message; status.className = 'pending-action-status error';
    if (confirmBtn) confirmBtn.disabled = false;
    if (cancelBtn)  cancelBtn.disabled  = false;
    if (approveBtn) approveBtn.disabled = false;
  }
}

