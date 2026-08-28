/* Admin "Data sources" page (ladmin), also served in POWER-USER mode.
 *
 * Standalone admin panel: single IIFE, fetch + small render helpers, no
 * framework, no dashboard.js. Talks only to /api/admin/* and /auth/*.
 * Credentials never render — the API returns masked rows.
 *
 * Power mode (window.__MANAGER_MODE__ === 'power', set by the template for
 * /power/data_sources): the template removes Users/Roles/Audit/global-
 * schedule sections, the connection-mutation buttons and the wizard Access
 * panel; this file mirrors that by trimming SECTIONS, skipping the ladmin-
 * only fetches (they would 403), rendering connections read-only (browse
 * only) and showing table Delete only on tables the power user registered
 * themselves (registered_by === window.__MANAGER_EMAIL__ — the API enforces
 * the same rule). Admin mode behavior is unchanged.
 *
 * Layout: sidebar nav switches five sections (connections / tables /
 * relations / schedule / audit); state is a full refetch after every
 * mutation. The Relations section shows every confirmed relation (Zone A)
 * above the discovery tools (Zone B).
 *
 * Mandatory-confirm (client side): "Save & snapshot" stays disabled until
 * the review checkbox is ticked, and the tick is force-cleared whenever a
 * new AI draft lands or another table is loaded. The server enforces the
 * same gate independently (confirm:true, CONFIRM_REQUIRED).
 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);

  const POWER = window.__MANAGER_MODE__ === 'power';
  const MANAGER_EMAIL = String(window.__MANAGER_EMAIL__ || '').toLowerCase();

  function toast(msg, isErr) {
    const t = document.createElement('div');
    t.className = 'adm-toast' + (isErr ? ' bad' : '');
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 4000);
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
      (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  }

  // Busy overlay for DB round-trips (snapshot / refresh / test) — large
  // tables can take a while and the page must never look dead meanwhile.
  function _busy(msg) {
    $('admBusyMsg').textContent = msg || 'Working…';
    $('admBusy').classList.remove('hidden');
  }
  function _busyDone() {
    $('admBusy').classList.add('hidden');
  }

  // Time budgets for the register-a-recommendation gesture. The server bounds
  // every dependency it owns; these bound the WAIT so the page never shows a
  // spinner nobody can wait out.
  const CLASSIFY_TIMEOUT_MS = 15000;   // type-suggestion probe (optional)
  const ACCEPT_WARN_MS = 20000;        // upgrade the busy message
  const ACCEPT_ABORT_MS = 390000;      // 60s draft + 300s statement + margin

  async function api(path, opts) {
    let res;
    try {
      res = await fetch(path, Object.assign({
        headers: { 'Content-Type': 'application/json' },
      }, opts || {}));
    } catch (e) {
      // A rejected fetch (abort, network drop) used to leave callers
      // dereferencing undefined — an unhandled TypeError and NO message at
      // all. Shaped like a normal reply so every call site's error path runs.
      return { status: 0, ok: false, aborted: (e && e.name === 'AbortError'),
        data: { error: (e && e.name === 'AbortError')
          ? 'Timed out waiting for the server.'
          : 'Network error — the server did not respond.' } };
    }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    return { status: res.status, ok: res.ok, data: data || {} };
  }

  // api() with a hard client-side cap. Aborting only stops OUR wait — the
  // server may still finish, so callers refetch rather than assert failure.
  async function apiTimed(path, opts, ms) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), ms);
    try {
      return await api(path, Object.assign({ signal: ctl.signal }, opts || {}));
    } finally { clearTimeout(timer); }
  }

  let DIALECTS = [];
  let CONNECTIONS = [];
  let TABLES = [];
  let ENCRYPTION_READY = true;
  let editingConnId = null;     // null = creating
  let editingTableId = null;    // null = registering new
  let currentIntro = null;      // last introspection result (register wizard)
  let currentPreview = null;    // last preview sample (feeds wizard suggestions)
  let wizardConnId = null;
  let wizardStep = 1;
  let wizardPrefill = null;     // ghost-hint shortcut: {schema, table, connector}
  let WIZ_SUGGESTIONS = [];     // wizard relation suggestions (step 3)
  let _wizSuggestToken = 0;     // supersedes stale in-flight suggestion loads
  let _wizSuggestLoading = false; // blocks Save mid-load (pre-checked rows!)

  let RELCANDIDATES = [];       // discover-relations proposals (session-local)
  const REL_DISMISSED = new Set(); // dismissed candidate ids (session-local by design)

  let USERS = [];               // admin Users window rows
  let ROLES = [];               // roles registry (Base first, server-sorted)
  let editingRoleId = null;     // null = creating
  let roleDraft = null;         // role-modal working state (Sets, re-rendered per toggle)
  let roleDeleteCtx = null;     // {id, name, member_count} for the delete confirm

  // ── Sidebar navigation ─────────────────────────────────────────────────
  const SECTIONS = POWER
    ? ['connections', 'tables', 'relations']
    : ['connections', 'tables', 'relations', 'users', 'roles', 'schedule', 'audit'];

  function showSection(name) {
    if (!SECTIONS.includes(name)) name = 'connections';
    SECTIONS.forEach((s) => {
      const sec = $('sec' + s.charAt(0).toUpperCase() + s.slice(1));
      if (sec) sec.hidden = (s !== name);
    });
    document.querySelectorAll('.adm-nav-item').forEach((b) => {
      b.classList.toggle('active', b.dataset.section === name);
    });
    if (name !== 'relations' && cyInstance) {   // leak-free view teardown
      cyInstance.destroy();
      cyInstance = null;
    }
    if (name === 'relations' && relView === 'graph') refreshRelGraph();
    if (name === 'users') loadUsers();     // lazy refetch, like the audit tail
    if (name === 'audit') loadAudit();
    if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  }

  // ── Bootstrap ──────────────────────────────────────────────────────────
  async function loadAll() {
    // Power mode never fetches the ladmin-only endpoints (refresh_settings /
    // roles would 403 and pile up admin.denied audit rows); it fetches the
    // caller's HELD roles instead (19f "Share with your roles" panel).
    const [d, c, t, s, ro] = await Promise.all([
      api('/api/admin/dialects'), api('/api/admin/connections'),
      api('/api/admin/tables'),
      POWER ? Promise.resolve(null) : api('/api/admin/refresh_settings'),
      POWER ? api('/api/admin/my_roles') : api('/api/admin/roles'),
    ]);
    DIALECTS = d.data.dialects || [];
    CONNECTIONS = c.data.connections || [];
    TABLES = t.data.tables || [];
    ENCRYPTION_READY = c.data.encryption_ready !== false;
    $('adsKeyWarning').classList.toggle('hidden', ENCRYPTION_READY);
    ['btnAddConnection', 'btnAddConnectionHero'].forEach((id) => {
      const b = $(id);
      if (!b) return;                      // removed in power mode
      b.disabled = !ENCRYPTION_READY;
      b.title = ENCRYPTION_READY ? '' : 'Set CLIENT_ENCRYPTION_KEY first';
    });
    $('navConnCount').textContent = CONNECTIONS.length || '';
    $('navTableCount').textContent = TABLES.length || '';
    $('navRelCount').textContent = confirmedRelCount() || '';
    ROLES = ro.ok ? (ro.data.roles || []) : ROLES;   // keep last good on failure
    if (!POWER) {
      const rc = $('navRoleCount');
      if (rc) rc.textContent = ROLES.length || '';
      renderRoles();
    }
    // The zero-new explainer quotes a confirmed count — hide it once any
    // mutation refetches state, rather than showing a stale number.
    $('relNoNew').classList.add('hidden');
    renderConnections();
    renderTables();
    if (!POWER) renderSchedule(s.data);
    renderRelDialects();
    renderRelOverview();
    loadRelRecommendations();                // persistent Recommended tables
    if (relView === 'graph' && !$('secRelations').hidden) refreshRelGraph();
  }

  // ── Connections ────────────────────────────────────────────────────────
  function connStatusChips(c) {
    const chips = [];
    if (!c.password_set) chips.push('<span class="adm-chip bad">no password</span>');
    else if (!c.password_readable) chips.push('<span class="adm-chip bad">re-enter password</span>');
    if (c.last_test_ok === true) chips.push('<span class="adm-chip ok">test ok</span>');
    else if (c.last_test_ok === false) chips.push('<span class="adm-chip bad">test failed</span>');
    return chips.join(' ') || '<span class="adm-chip">not tested</span>';
  }

  function renderConnections() {
    const box = $('connectionsList');
    const hero = $('connOnboarding');           // removed in power mode
    if (!CONNECTIONS.length) {
      if (hero) hero.classList.remove('hidden');
      box.innerHTML = POWER
        ? '<div class="adm-card adm-muted">No connections are in your granted scope yet — ' +
          'ask your administrator.</div>'
        : '';
      return;
    }
    if (hero) hero.classList.add('hidden');
    const rows = CONNECTIONS.map((c) => {
      const where = c.db_type === 'sqlite' ? '(local file)' :
        `${esc(c.host)}:${esc(c.port ?? '')} / ${esc(c.database || c.service_name || '')}`;
      // Power users: connections are READ-ONLY — registering tables from them
      // is the one action; test/refresh/edit/delete stay ladmin's.
      const actions = POWER
        ? '<button class="adm-btn ghost small" data-act="browse">＋ Register table</button>'
        : `<button class="adm-btn ghost small" data-act="browse">＋ Register table</button>
          <button class="adm-icon-btn" data-act="test" title="Test connection">🔌</button>
          <button class="adm-icon-btn" data-act="refresh" title="Refresh all snapshots on this connection">⟳</button>
          <button class="adm-icon-btn" data-act="edit" title="Edit connection">✎</button>
          <button class="adm-icon-btn" data-act="delete" title="Delete connection">🗑</button>`;
      return `
      <tr data-cid="${esc(c.id)}">
        <td><strong>${esc(c.name)}</strong><div class="adm-cell-sub">${esc(c.user)}</div></td>
        <td><span class="adm-chip">${esc(c.db_type)}</span></td>
        <td class="adm-cell-mono">${where}</td>
        <td>${c.table_count || 0}</td>
        <td>${connStatusChips(c)}</td>
        <td class="adm-actions-cell">${actions}</td>
      </tr>`;
    }).join('');
    box.innerHTML = `<div class="adm-card adm-table-scroll"><table class="adm-table">
      <thead><tr><th>Name</th><th>Type</th><th>Server / database</th>
      <th>Tables</th><th>Status</th><th class="adm-th-actions"></th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
    box.querySelectorAll('button').forEach((b) => {
      const cid = b.closest('tr').dataset.cid;
      b.addEventListener('click', () =>
        connAction(b.dataset.act, CONNECTIONS.find((c) => c.id === cid)));
    });
  }

  async function connAction(act, c) {
    if (!c) return;
    if (act === 'test') {
      _busy(`Testing connection "${c.name}"…`);
      let r;
      try {
        r = await api('/api/admin/connections/test', {
          method: 'POST', body: JSON.stringify({ connection_id: c.id }) });
      } finally { _busyDone(); }
      if (r.data.ok) toast(`Connection OK (${r.data.server_version || 'server'} · ${r.data.elapsed_ms}ms)`);
      else toast(r.data.error || 'Connection failed', true);
      loadAll();
    } else if (act === 'browse') {
      openTableWizard(c.id);
    } else if (act === 'refresh') {
      _busy(`Refreshing all tables on "${c.name}"… large tables can take a while.`);
      let r;
      try {
        r = await api(`/api/admin/connections/${c.id}/refresh`, { method: 'POST', body: '{}' });
      } finally { _busyDone(); }
      const results = r.data.results || [];
      const bad = results.filter((x) => !x.ok);
      toast(bad.length ? `Refreshed with ${bad.length} failure(s)` : `Refreshed ${results.length} table(s)`, !!bad.length);
      loadAll();
    } else if (act === 'edit') {
      openConnModal(c);
    } else if (act === 'delete') {
      if (!window.confirm(`Delete connection "${c.name}"?`)) return;
      let r = await api(`/api/admin/connections/${c.id}/delete`, { method: 'POST', body: '{}' });
      if (r.status === 409) {
        const names = (r.data.tables || []).map((t) => t.display_name).join(', ');
        if (!window.confirm(`Registered tables use this connection (${names}). Delete them too?`)) return;
        r = await api(`/api/admin/connections/${c.id}/delete`,
          { method: 'POST', body: JSON.stringify({ cascade: true }) });
      }
      if (r.ok) toast('Connection deleted'); else toast(r.data.error || 'Delete failed', true);
      loadAll();
    }
  }

  function openConnModal(conn) {
    editingConnId = conn ? conn.id : null;
    $('connModalTitle').textContent = conn ? `Edit connection — ${conn.name}` : 'Add connection';
    const sel = $('connType');
    sel.innerHTML = '';
    DIALECTS.forEach((d) => {
      const o = document.createElement('option');
      o.value = d.key;
      o.textContent = d.label + (d.available ? '' : ` — unavailable: ${d.unavailable_reason}`);
      o.disabled = !d.available;
      sel.appendChild(o);
    });
    if (conn) sel.value = conn.db_type;
    $('connName').value = conn ? conn.name : '';
    $('connHost').value = conn ? (conn.host || '') : '';
    $('connPort').value = conn ? (conn.port || '') : '';
    $('connDatabase').value = conn ? (conn.database || '') : '';
    $('connService').value = conn ? (conn.service_name || '') : '';
    $('connUser').value = conn ? (conn.user || '') : '';
    $('connPassword').value = '';
    $('connPassword').placeholder = conn && conn.password_set ? '(unchanged)' : '';
    $('connSsl').checked = conn ? !!conn.ssl : false;
    $('connTrustCert').checked = conn ? !!conn.trust_server_certificate : false;
    $('connTestResult').classList.add('hidden');
    _prevDialectKey = sel.value;   // fresh memo — a stored port is "custom"
    onDialectChange();
    $('connModal').classList.remove('hidden');
    $('connName').focus();
  }

  let _prevDialectKey = null;   // last dialect the port was prefilled for

  function onDialectChange() {
    const d = DIALECTS.find((x) => x.key === $('connType').value);
    if (!d) return;
    // Re-prefill when the field is empty OR still holds the PREVIOUS
    // dialect's default (switching Type updates the port then) — a custom
    // port is never overwritten.
    const prev = DIALECTS.find((x) => x.key === _prevDialectKey);
    const cur = String($('connPort').value || '').trim();
    if (!cur || (prev && cur === String(prev.default_port || ''))) {
      $('connPort').value = d.default_port || '';
    }
    _prevDialectKey = d.key;
    const needsService = (d.needs || []).includes('service_name');
    $('connServiceWrap').classList.toggle('hidden', !needsService);
    $('connDatabaseWrap').classList.toggle('hidden', needsService);
  }

  function connFormBody() {
    return {
      name: $('connName').value.trim(),
      db_type: $('connType').value,
      host: $('connHost').value.trim(),
      port: parseInt($('connPort').value, 10) || null,
      database: $('connDatabase').value.trim() || null,
      service_name: $('connService').value.trim() || null,
      user: $('connUser').value.trim(),
      password: $('connPassword').value,
      ssl: $('connSsl').checked,
      trust_server_certificate: $('connTrustCert').checked,
    };
  }

  async function testConnDraft() {
    const body = connFormBody();
    if (editingConnId && !body.password) body.connection_id = editingConnId; // stored credential
    const box = $('connTestResult');
    box.classList.remove('hidden', 'ok', 'bad');
    box.textContent = 'Testing…';
    const r = await api('/api/admin/connections/test', { method: 'POST', body: JSON.stringify(body) });
    box.classList.add(r.data.ok ? 'ok' : 'bad');
    box.textContent = r.data.ok
      ? `✓ Connected (${r.data.server_version || 'server'} · ${r.data.elapsed_ms}ms)`
      : `✕ ${r.data.error || 'Connection failed'}`;
  }

  async function saveConn() {
    const body = connFormBody();
    const path = editingConnId ? `/api/admin/connections/${editingConnId}` : '/api/admin/connections';
    const r = await api(path, { method: 'POST', body: JSON.stringify(body) });
    if (r.status === 201 || r.ok) {
      toast('Connection saved');
      $('connModal').classList.add('hidden');
      loadAll();
    } else {
      toast(r.data.error || 'Save failed', true);
    }
  }

  // ── Registered tables ──────────────────────────────────────────────────
  // Schema-drift surfacing (Prompt 13 Part C): a refresh that saw columns
  // added/removed/retyped records last_drift on the table row; the banner +
  // per-row chip stay visible until the admin dismisses (audited) or edits
  // the table. Policy: source truth is applied immediately (snapshot honestly
  // mirrors the source) — surfacing is how the admin learns why.
  function _driftBits(d) {
    const bits = [];
    (d.added || []).forEach((c) => bits.push(`+ ${c}`));
    (d.removed || []).forEach((c) => bits.push(`− ${c}`));
    (d.retyped || []).forEach((r) => bits.push(`${r.col}: ${r.from} → ${r.to}`));
    return bits;
  }

  function renderDriftBanner() {
    const box = $('driftBanner');
    const drifted = TABLES.filter((t) => t.last_drift && !t.last_drift.dismissed);
    if (!drifted.length) { box.innerHTML = ''; return; }
    box.innerHTML = drifted.map((t) => `
      <div class="adm-alert" data-tid="${esc(t.id)}">
        <div><strong>Schema drift — ${esc(t.display_name)}</strong>
          <span class="adm-muted">(${esc(t.last_drift.at || '')})</span><br/>
          ${_driftBits(t.last_drift).map(esc).join(' · ')}<br/>
          <span class="adm-muted">The snapshot and chat schemas already follow the source.
          New columns keep empty descriptions until you edit the table.</span></div>
        <button class="adm-btn ghost" data-act="dismiss-drift">Dismiss</button>
      </div>`).join('');
    box.querySelectorAll('[data-act="dismiss-drift"]').forEach((b) => {
      b.addEventListener('click', async () => {
        const tid = b.closest('.adm-alert').dataset.tid;
        const r = await api(`/api/admin/tables/${tid}/dismiss_drift`, { method: 'POST', body: '{}' });
        if (r.ok) { toast('Drift dismissed'); loadAll(); }
        else toast(r.data.error || 'Dismiss failed', true);
      });
    });
  }

  function renderTables() {
    renderDriftBanner();
    const box = $('tablesList');
    if (!TABLES.length) {
      box.innerHTML = `<div class="adm-empty">
        <div class="adm-empty-ico">📋</div>
        <p>No tables registered yet.</p>
        <p class="adm-muted">${CONNECTIONS.length
          ? 'Use “＋ Register table” to pick a table from one of your connections.'
          : 'Add a database connection first, then register tables from it.'}</p>
      </div>`;
      return;
    }
    const connName = (cid) => (CONNECTIONS.find((c) => c.id === cid) || {}).name || '?';
    // Power users may delete ONLY tables they registered themselves — the API
    // enforces the same rule (NOT_OWNER); ladmin sees Delete everywhere.
    const canDelete = (t) => !POWER ||
      String(t.registered_by || '').toLowerCase() === MANAGER_EMAIL;
    const rows = TABLES.map((t) => `
      <tr data-tid="${esc(t.id)}">
        <td><strong>${esc(t.display_name)}</strong>${t.is_connector
          ? ' <span class="adm-chip conn" title="Hidden from users; auto-included via relations">connector</span>' : ''}${
          t.schedule ? ' <span class="adm-chip conn" title="Refreshes on its own schedule">own schedule</span>'
            : ' <span class="adm-chip" title="Follows the global refresh schedule">inherits global</span>'}</td>
        <td class="adm-cell-mono">${esc(connName(t.connection_id))} · ${esc([t.schema, t.table_name].filter(Boolean).join('.'))}</td>
        <td>${t.row_count != null ? Number(t.row_count).toLocaleString() : '—'}</td>
        <td title="${esc(t.last_refresh_error || '')}">${t.refreshed_at
          ? esc(t.refreshed_at) + (t.last_refresh_error
            ? ' <span class="adm-chip bad">refresh failed</span>' : '')
          : (t.last_refresh_error ? '<span class="adm-chip bad">failed</span>' : '—')}${
          t.last_drift && !t.last_drift.dismissed
            ? ` <span class="adm-chip bad" title="${esc(_driftBits(t.last_drift).join(' · '))}">schema drift</span>` : ''}</td>
        <td class="adm-actions-cell">
          <button class="adm-icon-btn" data-act="refresh" title="Refresh snapshot now (always a full snapshot)">⟳</button>
          <button class="adm-icon-btn" data-act="schedule" title="Refresh schedule for this table">⏱</button>
          <button class="adm-icon-btn" data-act="edit" title="Edit descriptions / relations">✎</button>
          ${canDelete(t)
            ? '<button class="adm-icon-btn" data-act="delete" title="Delete registered table">🗑</button>'
            : ''}
        </td>
      </tr>`).join('');
    box.innerHTML = `<div class="adm-card adm-table-scroll"><table class="adm-table">
      <thead><tr><th>Display name</th><th>Source</th>
      <th>Rows</th><th>Data as of</th><th class="adm-th-actions"></th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
    box.querySelectorAll('button').forEach((b) => {
      const tid = b.closest('tr').dataset.tid;
      b.addEventListener('click', () => tableAction(b.dataset.act, tid));
    });
  }

  async function tableAction(act, tid) {
    const t = TABLES.find((x) => x.id === tid);
    if (!t) return;
    if (act === 'refresh') {
      _busy(`Refreshing "${t.display_name}"… large tables can take a while.`);
      let r;
      try {
        r = await api(`/api/admin/tables/${tid}/refresh`, { method: 'POST', body: '{}' });
      } finally { _busyDone(); }
      if (r.data.ok && r.data.skipped) {
        toast('No changes detected — snapshot already current');
      } else if (r.data.ok) {
        const d = r.data.drift || {};
        const parts = [];
        if ((d.added || []).length) parts.push(`+${d.added.length}`);
        if ((d.removed || []).length) parts.push(`-${d.removed.length}`);
        if ((d.retyped || []).length) parts.push(`~${d.retyped.length} retyped`);
        const driftNote = parts.length
          ? ` (schema drift: ${parts.join('/')} columns — chats re-synced)` : '';
        toast(`Refreshed: ${Number(r.data.rows).toLocaleString()} rows${driftNote}`);
      } else {
        toast(r.data.error || 'Refresh failed', true);
      }
      loadAll();
    } else if (act === 'schedule') {
      openTableScheduleModal(t);
    } else if (act === 'edit') {
      openTableWizard(t.connection_id, t);
    } else if (act === 'delete') {
      if (!window.confirm(`Delete registered table "${t.display_name}"? Chats that use it keep their history but the table stops loading.`)) return;
      const r = await api(`/api/admin/tables/${tid}/delete`, { method: 'POST', body: '{}' });
      if (r.ok) {
        toast('Table deleted');
      } else if (r.data.code === 'NOT_OWNER') {
        toast('Only tables you registered yourself can be deleted.', true);
      } else if (r.data.code === 'OUT_OF_SCOPE') {
        toast('This table is outside your managed scope.', true);
      } else {
        toast(r.data.error || 'Delete failed', true);
      }
      loadAll();
    }
  }

  // "+ Register table" from the Tables section: one connection → straight in;
  // several → quick pick via prompt-free chooser (first connection preselected
  // in a tiny select is overkill — use a confirm-style chooser list).
  function registerTableEntry() {
    if (!CONNECTIONS.length) {
      showSection('connections');
      toast(POWER ? 'No connections are in your granted scope — ask your administrator'
                  : 'Add a database connection first', true);
      return;
    }
    if (CONNECTIONS.length === 1) { openTableWizard(CONNECTIONS[0].id); return; }
    // lightweight chooser: reuse the wizard's step-1 connection field as a select
    openTableWizard(CONNECTIONS[0].id, null, true);
  }

  // ── Register / edit wizard (3 steps) ───────────────────────────────────
  function setWizardStep(n) {
    wizardStep = n;
    [1, 2, 3].forEach((i) => {
      $('twStep' + i).hidden = (i !== n);
      const dot = document.querySelector(`.adm-step-dot[data-step="${i}"]`);
      dot.classList.toggle('active', i === n);
      dot.classList.toggle('done', i < n);
    });
    $('btnTwBack').hidden = (n === 1);
    $('btnTwNext').hidden = (n === 3);
    $('btnSaveTable').hidden = (n !== 3);
    if (n === 3) {
      renderSummary();
      loadWizardSuggestions();   // recomputed on every step-3 open
      renderWizardAccess();      // synchronous — ROLES loaded at page load
    }
  }

  async function openTableWizard(connId, existing, chooseConn, prefill) {
    wizardConnId = connId;
    editingTableId = existing ? existing.id : null;
    // "Register as connector" shortcut state (ghost hints). Cleared on every
    // open; the connector tick is applied inside introspectNow, which would
    // otherwise overwrite it.
    wizardPrefill = prefill || null;
    currentIntro = null;
    $('tableModalTitle').textContent = existing
      ? `Edit table — ${existing.display_name}` : 'Register table';
    $('twConfirm').checked = false;
    $('twDraftBanner').classList.add('hidden');
    updateSaveEnabled();
    setWizardStep(1);
    $('tableModal').classList.remove('hidden');

    // Connection field: fixed label normally; a select when the entry point
    // didn't come from a specific connection row.
    const connField = $('twConnName');
    if (chooseConn) {
      const sel = document.createElement('select');
      sel.id = 'twConnName';
      CONNECTIONS.forEach((c) => {
        const o = document.createElement('option');
        o.value = c.id; o.textContent = c.name;
        sel.appendChild(o);
      });
      sel.value = connId;
      sel.onchange = async () => { wizardConnId = sel.value; await loadSchemas(null); };
      connField.replaceWith(sel);
    } else if (connField.tagName === 'SELECT') {
      const inp = document.createElement('input');
      inp.type = 'text'; inp.id = 'twConnName'; inp.disabled = true;
      connField.replaceWith(inp);
    }
    const cf = $('twConnName');
    if (cf.tagName === 'INPUT') {
      cf.value = (CONNECTIONS.find((c) => c.id === connId) || {}).name || '';
    }

    // Prefill rides the existing loadSchemas path (it reads only `.schema`
    // and its explicit loadTableNames() call does the reload — programmatic
    // .value writes fire no onchange).
    await loadSchemas(existing || (wizardPrefill ? { schema: wizardPrefill.schema } : null));
    if (existing) {
      $('twTable').value = existing.table_name;
      await introspectNow(existing, { advance: false });
    } else if (wizardPrefill && wizardPrefill.table) {
      // Case-insensitive option match (Oracle case-folds catalog names).
      const sel = $('twTable');
      const want = String(wizardPrefill.table).toLowerCase();
      const opt = Array.from(sel.options).find(
        (o) => o.value.toLowerCase() === want && !o.disabled);
      if (opt) {
        sel.value = opt.value;
      } else {
        $('twPickStatus').textContent =
          `Could not preselect "${wizardPrefill.table}" — pick the table manually.`;
      }
    }
  }

  // System schemas are UI-noise for table registration — hidden from the
  // dropdown until "Show system schemas" is ticked. Server responses stay
  // unfiltered; the denylist is fixed and case-insensitive.
  const SYSTEM_SCHEMAS = new Set(['information_schema', 'pg_catalog',
                                  'pg_toast', 'system']);
  let _wizardSchemas = [];   // last fetched (unfiltered) list

  function _fillSchemaSelect(preselect) {
    const sel = $('twSchema');
    const showSys = !!$('twShowSystemSchemas')?.checked;
    let list = showSys ? _wizardSchemas
      : _wizardSchemas.filter((s) => !SYSTEM_SCHEMAS.has(String(s).toLowerCase()));
    // A preselected system schema (edit of an existing table, or the server
    // default) must stay selectable — auto-reveal instead of dropping it.
    if (preselect && !list.includes(preselect) && _wizardSchemas.includes(preselect)) {
      const cb = $('twShowSystemSchemas');
      if (cb) cb.checked = true;
      list = _wizardSchemas;
    }
    sel.innerHTML = '';
    (list.length ? list : ['']).forEach((s) => {
      const o = document.createElement('option');
      o.value = s; o.textContent = s || '(default)';
      sel.appendChild(o);
    });
    if (preselect && list.includes(preselect)) sel.value = preselect;
  }

  async function loadSchemas(existing) {
    $('twPickStatus').textContent = 'Loading schemas…';
    const r = await api(`/api/admin/connections/${wizardConnId}/schemas`);
    _wizardSchemas = r.data.schemas || [];
    const sel = $('twSchema');
    const preselect = (existing && existing.schema)
      || r.data.default_schema || '';
    _fillSchemaSelect(preselect);
    sel.onchange = loadTableNames;
    const sysCb = $('twShowSystemSchemas');
    if (sysCb) {
      sysCb.onchange = async () => {
        // Unchecking while ON a system schema falls back to the first
        // ordinary one (keeping it would just re-check the box).
        const cur = $('twSchema').value;
        const keep = sysCb.checked
          || !SYSTEM_SCHEMAS.has(String(cur).toLowerCase());
        _fillSchemaSelect(keep ? cur : null);
        await loadTableNames();
      };
    }
    await loadTableNames();
    // `r.ok` too, not just the body flag: a transport failure carries no
    // body, and clearing the status there would leave an empty dropdown
    // with no explanation at all.
    $('twPickStatus').textContent = (r.ok && r.data.ok !== false)
      ? '' : (r.data.error || 'Failed to list schemas');
  }

  async function loadTableNames() {
    const r = await api(`/api/admin/connections/${wizardConnId}/tables?schema=${encodeURIComponent($('twSchema').value)}`);
    const sel = $('twTable');
    sel.innerHTML = '';
    // A physical table can be registered only once (the server enforces it
    // too — 400 DUPLICATE_TABLE): registered tables stay visible but
    // disabled, labeled with the registration they belong to. In edit mode
    // the edited table's own row stays selectable.
    const own = editingTableId ? TABLES.find((x) => x.id === editingTableId) : null;
    (r.data.tables || []).forEach((t) => {
      const o = document.createElement('option');
      o.value = t.name;
      const view = t.kind === 'view' ? ' [view]' : '';
      // case-insensitive: the server's physical identity lowercases too
      const isOwn = !!(own && String(own.table_name).toLowerCase() === String(t.name).toLowerCase()
        && String(own.schema || '').toLowerCase() === $('twSchema').value.toLowerCase());
      if (t.registered && !isOwn) {
        o.disabled = true;
        o.textContent = `${t.name}${view} — already registered as '${t.registered_as || '?'}'`;
      } else {
        o.textContent = t.name + view + (isOwn ? ' (this registration)' : '');
      }
      sel.appendChild(o);
    });
    if (sel.selectedIndex >= 0 && sel.options[sel.selectedIndex].disabled) {
      const firstEnabled = Array.from(sel.options).findIndex((o) => !o.disabled);
      sel.selectedIndex = firstEnabled;
      if (firstEnabled === -1) {
        $('twPickStatus').textContent =
          'Every table in this schema is already registered.';
      }
    }
  }

  async function introspectNow(existing, opts) {
    const advance = !opts || opts.advance !== false;
    $('twPickStatus').textContent = 'Loading table structure…';
    $('btnTwNext').disabled = true;
    const r = await api('/api/admin/tables/introspect', {
      method: 'POST',
      body: JSON.stringify({ connection_id: wizardConnId,
        schema: $('twSchema').value, table: $('twTable').value }),
    });
    $('btnTwNext').disabled = false;
    if (!r.data.ok) {
      $('twPickStatus').textContent = r.data.error || 'Introspection failed';
      return false;
    }
    $('twPickStatus').textContent = '';
    currentIntro = r.data.introspection;
    currentPreview = r.data.preview || null;   // feeds the wizard suggestions
    const prevByName = {};
    (existing && existing.columns || []).forEach((c) => { prevByName[c.name] = c; });

    $('twDisplayName').value = existing ? existing.display_name
      : $('twTable').value.toLowerCase().replace(/_/g, ' ');
    // An explicit prefill wins; otherwise the server's suggestion pre-ticks
    // the box (a registered table always keeps its stored type).
    const cls = r.data.classification || {};
    $('twIsConnector').checked = existing ? !!existing.is_connector
      : (wizardPrefill && typeof wizardPrefill.connector === 'boolean')
        ? wizardPrefill.connector
        : cls.suggested_type === 'connector';
    const connLabel = $('twIsConnector').closest('label');
    if (connLabel && !existing && cls.reason) {
      connLabel.title = `Suggested: ${cls.suggested_type === 'connector'
        ? 'connector' : 'normal table'} — ${cls.reason}`;
    }
    $('twDescription').value = existing ? (existing.description || '')
      : (currentIntro.table_comment || '');

    const rc = currentIntro.row_count_estimate;
    const sz = currentIntro.size_bytes_estimate;
    const deg = currentIntro.degraded || [];
    $('twStats').textContent =
      `~${rc != null ? Number(rc).toLocaleString() : 'n/a'} rows · ` +
      `${sz != null ? (sz / (1024 * 1024)).toFixed(1) + ' MB' : 'size n/a'}` +
      (deg.length ? ` · not available from catalog: ${deg.join(', ')} (insufficient privileges is OK)` : '');

    const tbody = $('twColsBody');
    tbody.innerHTML = '';
    currentIntro.columns.forEach((c) => {
      const prev = prevByName[c.name] || {};
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${esc(c.name)}</td>
        <td class="adm-muted">${esc(c.dtype)}</td>
        <td>${c.pk ? '🔑' : ''}</td>
        <td><input type="checkbox" class="tw-col-indexed" ${ (prev.indexed != null ? prev.indexed : c.indexed) ? 'checked' : ''}></td>
        <td><input type="text" class="tw-col-desc" value="${esc(prev.description || '')}"></td>`;
      tr.dataset.name = c.name;
      tr.dataset.dtype = c.dtype;
      tr.dataset.pk = c.pk ? '1' : '';
      tbody.appendChild(tr);
    });

    const prevW = $('twPreview');
    const p = r.data.preview || {};
    if (p.ok && (p.rows || []).length) {
      prevW.innerHTML = `<table class="adm-table"><thead><tr>${
        (p.columns || []).map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${
        p.rows.map((row) => `<tr>${row.map((v) => `<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
    } else {
      prevW.innerHTML = `<div class="adm-muted">${esc(p.error || 'No preview rows.')}</div>`;
    }

    // New tables: FK relations are no longer pre-seeded as manual rows —
    // they arrive as PRE-CHECKED rows in the step-3 suggestion block (same
    // save outcome, now with origin/cardinality + verification shown; seeded
    // rows would dedupe the suggestions away). Edits keep their stored rows.
    renderRelations(existing ? (existing.relations || []) : []);
    $('twConfirm').checked = false;
    $('twDraftBanner').classList.add('hidden');
    updateSaveEnabled();
    if (advance) setWizardStep(2);
    return true;
  }

  function wizardNext() {
    if (wizardStep === 1) {
      introspectNow(editingTableId ? TABLES.find((t) => t.id === editingTableId) : null);
    } else if (wizardStep === 2) {
      if (!$('twDisplayName').value.trim()) {
        toast('Give the table a display name', true);
        $('twDisplayName').focus();
        return;
      }
      setWizardStep(3);
    }
  }

  function renderSummary() {
    if (!currentIntro) { $('twSummary').innerHTML = ''; return; }
    const rc = currentIntro.row_count_estimate;
    const connName = (CONNECTIONS.find((c) => c.id === wizardConnId) || {}).name || '';
    const described = document.querySelectorAll('#twColsBody .tw-col-desc');
    const filled = Array.from(described).filter((i) => i.value.trim()).length;
    $('twSummary').innerHTML = `
      <div class="adm-summary-row"><span>Display name</span><strong>${esc($('twDisplayName').value)}</strong></div>
      <div class="adm-summary-row"><span>Source</span><strong>${esc(connName)} · ${esc($('twSchema').value)}.${esc($('twTable').value)}</strong></div>
      <div class="adm-summary-row"><span>Rows (estimate)</span><strong>${rc != null ? Number(rc).toLocaleString() : 'n/a'}</strong></div>
      <div class="adm-summary-row"><span>Column descriptions</span><strong>${filled} of ${described.length} filled</strong></div>
      <div class="adm-summary-note">Saving takes a local snapshot now — user questions run against
        the snapshot, never against your database.</div>`;
  }

  function renderRelations(rels) {
    const box = $('twRelations');
    box.innerHTML = '';
    (rels || []).forEach((rel) => addRelationRow(rel));
  }

  function wizardLeftCols() {
    const cols = [];
    document.querySelectorAll('#twColsBody tr').forEach((tr) => {
      cols.push({ name: tr.dataset.name, dtype: tr.dataset.dtype || '' });
    });
    return cols;
  }

  function addRelationRow(rel) {
    const box = $('twRelations');
    const row = document.createElement('div');
    row.className = 'adm-rel-row adm-rel-row-block';
    row._rel = rel || null;   // original dict — extra keys (cardinality/origin) survive an unchanged save
    const options = TABLES
      .filter((t) => t.id !== editingTableId)
      .map((t) => `<option value="${esc(t.id)}" ${rel && rel.related_table_id === t.id ? 'selected' : ''}>${esc(t.display_name)}</option>`)
      .join('');
    row.innerHTML = `
      <div class="adm-rel-row-head">
        <span>joins</span>
        <select class="tw-rel-target"><option value="">— pick table —</option>${options}</select>
        <span>on</span>
        <button type="button" class="adm-icon-btn tw-rel-remove" title="Remove relation">×</button>
      </div>`;
    const editor = createPairEditor({
      leftCols: wizardLeftCols(),
      rightCols: tableCols(rel && rel.related_table_id),
      pairs: (rel && rel.join_keys) || [],
    });
    row._pairEditor = editor;
    row.appendChild(editor.el);
    const targetSel = row.querySelector('.tw-rel-target');
    targetSel.addEventListener('change', () => editor.setRightCols(tableCols(targetSel.value)));
    row.querySelector('.tw-rel-remove').addEventListener('click', () => row.remove());
    box.appendChild(row);
  }

  // ── Structured join-key pair editor ────────────────────────────────────
  // Replaces the old free-text "a=b, c=d" input everywhere relations are
  // manually created/edited. Columns come from stored registry metadata
  // only (TABLES / the wizard's introspected columns) — no live DB calls.

  // SQL type string -> family. Mirrors the verification rule in
  // relation_discovery._verify_one (numeric joins numeric; other families
  // must match), applied to the registry's SQLAlchemy type strings.
  const DTYPE_FAMILIES = [
    ['numeric', /^(TINY|SMALL|MEDIUM|BIG)?INT(?!ERVAL)|^(NUMERIC|DECIMAL|NUMBER|FLOAT|REAL|DOUBLE|MONEY|SERIAL)/],
    ['text', /^(N?VAR)?CHAR|^N?TEXT|^CLOB|^STRING|^UUID|^ENUM/],
    ['temporal', /^DATE|^TIME|^DATETIME|^TIMESTAMP|^SMALLDATETIME/],
    ['bool', /^BOOL|^BIT/],
  ];
  function dtypeFamily(dtype) {
    const d = String(dtype || '').trim().toUpperCase();
    if (!d) return null;
    for (const [fam, re] of DTYPE_FAMILIES) { if (re.test(d)) return fam; }
    return null;
  }

  function tableCols(tid) {
    const t = TABLES.find((x) => x.id === tid);
    return ((t && t.columns) || []).map((c) => ({ name: c.name, dtype: c.dtype || '' }));
  }

  function createPairEditor(opts) {
    // opts: {leftCols, rightCols, pairs, onChange}; cols = [{name, dtype}].
    // Returns {el, getPairs(), hasMissing(), setRightCols()}. getPairs()
    // keeps the old parseJoinKeys contract: [[l, r], ...], incomplete rows
    // dropped — and works synchronously right after construction.
    const el = document.createElement('div');
    el.className = 'adm-pair-editor';
    const rowsBox = document.createElement('div');
    el.appendChild(rowsBox);
    const addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'adm-btn ghost small';
    addBtn.textContent = '＋ add column pair';
    el.appendChild(addBtn);
    const warnBox = document.createElement('div');
    warnBox.className = 'adm-pair-warnings';
    el.appendChild(warnBox);

    let rightCols = opts.rightCols || [];
    const leftCols = opts.leftCols || [];

    function colSelect(cols, side, value) {
      const sel = document.createElement('select');
      sel.className = 'adm-pair-col ' + side;
      const empty = document.createElement('option');
      empty.value = '';
      empty.textContent = '— column —';
      sel.appendChild(empty);
      let found = !value;
      cols.forEach((c) => {
        const o = document.createElement('option');
        o.value = c.name;
        o.textContent = c.name + (c.dtype ? ` (${c.dtype})` : '');
        o.dataset.dtype = c.dtype || '';
        if (c.name === value) found = true;
        sel.appendChild(o);
      });
      if (!found && value) {
        // A stored column the registry no longer has: keep it visible and
        // selected, clearly invalid — NEVER silently dropped.
        const o = document.createElement('option');
        o.value = value;
        o.textContent = `${value} (missing)`;
        o.dataset.missing = '1';
        sel.appendChild(o);
      }
      sel.value = value || '';
      sel.addEventListener('change', refresh);
      return sel;
    }

    function addRow(pair) {
      const row = document.createElement('div');
      row.className = 'adm-pair-row';
      const left = colSelect(leftCols, 'left', pair && pair[0]);
      const eq = document.createElement('span');
      eq.textContent = '=';
      const right = colSelect(rightCols, 'right', pair && pair[1]);
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'adm-icon-btn';
      rm.title = 'Remove pair';
      rm.textContent = '×';
      rm.addEventListener('click', () => { row.remove(); refresh(); });
      row.append(left, eq, right, rm);
      rowsBox.appendChild(row);
    }

    function refresh() {
      const notes = [];
      let missing = false;
      rowsBox.querySelectorAll('.adm-pair-row').forEach((row) => {
        const l = row.querySelector('.adm-pair-col.left');
        const r = row.querySelector('.adm-pair-col.right');
        const lo = l.selectedOptions[0];
        const ro = r.selectedOptions[0];
        l.classList.toggle('adm-invalid', !!(lo && lo.dataset.missing));
        r.classList.toggle('adm-invalid', !!(ro && ro.dataset.missing));
        if ((lo && lo.dataset.missing) || (ro && ro.dataset.missing)) missing = true;
        if (l.value && r.value && lo && ro && !lo.dataset.missing && !ro.dataset.missing) {
          const lf = dtypeFamily(lo.dataset.dtype);
          const rf = dtypeFamily(ro.dataset.dtype);
          if (lf && rf && lf !== rf) {
            notes.push(`⚠ ${l.value} and ${r.value} types may be incompatible (${lo.dataset.dtype || '?'} vs ${ro.dataset.dtype || '?'})`);
          }
        }
      });
      if (missing) {
        notes.unshift('⚠ a selected column no longer exists in the registry — pick a current column');
      }
      warnBox.textContent = notes.join('  ·  ');
      // Never during construction: callers capture the returned editor in a
      // const, so a synchronous onChange would hit the temporal dead zone.
      // They set the initial button state themselves right after creation.
      if (ready && typeof opts.onChange === 'function') opts.onChange();
    }

    addBtn.addEventListener('click', () => addRow(null));
    let ready = false;
    ((opts.pairs && opts.pairs.length) ? opts.pairs : [null]).forEach(addRow);
    refresh();
    ready = true;

    return {
      el,
      getPairs() {
        const out = [];
        rowsBox.querySelectorAll('.adm-pair-row').forEach((row) => {
          const l = row.querySelector('.adm-pair-col.left').value;
          const r = row.querySelector('.adm-pair-col.right').value;
          if (l && r) out.push([l, r]);
        });
        return out;
      },
      hasMissing() {
        return !!rowsBox.querySelector('.adm-pair-col option[data-missing]:checked');
      },
      setRightCols(cols) {
        // target-table change: rebuild right selects, reset in-progress picks
        rightCols = cols || [];
        rowsBox.querySelectorAll('.adm-pair-row').forEach((row) => {
          const right = row.querySelector('.adm-pair-col.right');
          right.replaceWith(colSelect(rightCols, 'right', ''));
        });
        refresh();
      },
    };
  }

  function collectRelations() {
    const rels = [];
    document.querySelectorAll('#twRelations .adm-rel-row').forEach((row) => {
      const target = row.querySelector('.tw-rel-target').value;
      if (!target) return;
      const pairs = row._pairEditor ? row._pairEditor.getPairs() : [];
      if (!pairs.length) return;
      // Carry discovered extras (cardinality/origin) through an UNCHANGED
      // manual save — a re-save must not silently strip them. Changed target
      // or keys invalidate the measured extras, so they are dropped then.
      const orig = row._rel;
      const unchanged = orig && orig.related_table_id === target
        && JSON.stringify(orig.join_keys || []) === JSON.stringify(pairs);
      rels.push(Object.assign({}, unchanged ? orig : {},
        { related_table_id: target, join_keys: pairs }));
    });
    return rels;
  }

  // ── Wizard relation suggestions (step 3) ───────────────────────────────
  // Computed purely from wizard-held state (introspected FKs + preview
  // sample + typed descriptions); FK rows arrive pre-checked, similarity
  // rows unchecked. Checked rows ride the NORMAL table save.
  async function loadWizardSuggestions() {
    const box = $('twSuggestions');
    WIZ_SUGGESTIONS = [];
    if (!currentIntro) {
      _wizSuggestToken++;            // invalidate any in-flight load
      _wizSuggestLoading = false;    // (its completion no longer clears this)
      renderWizardSuggestions();
      return;
    }
    const token = ++_wizSuggestToken;
    _wizSuggestLoading = true;
    box.className = 'adm-muted adm-rel-band-empty';
    box.textContent = 'Loading suggestions…';
    const columns = [];
    document.querySelectorAll('#twColsBody tr').forEach((tr) => {
      columns.push({ name: tr.dataset.name, pk: !!tr.dataset.pk,
        description: tr.querySelector('.tw-col-desc').value.trim() });
    });
    const p = currentPreview;
    let r;
    try {
      r = await api('/api/admin/relations/wizard_suggest', {
        method: 'POST',
        body: JSON.stringify({
        editing_tid: editingTableId || '',
        connection_id: wizardConnId || '',
        schema: $('twSchema').value,
        table_name: $('twTable').value,
        display_name: $('twDisplayName').value.trim(),
        columns,
        foreign_keys: currentIntro.foreign_keys || [],
        relations: collectRelations(),
        sample: p && p.ok ? { columns: p.columns, rows: p.rows } : null,
        }),
      });
    } catch (e) {
      // A thrown fetch must not leave the loading flag stuck (Save would
      // refuse forever) nor misreport an error as "no matches".
      if (token === _wizSuggestToken) {
        _wizSuggestLoading = false;
        box.className = 'adm-muted adm-rel-band-empty';
        box.textContent = 'Could not load suggestions — you can still add relations manually below.';
      }
      return;
    }
    if (token !== _wizSuggestToken) return;   // superseded by a newer open
    _wizSuggestLoading = false;
    if (!r.data.ok) {
      box.className = 'adm-muted adm-rel-band-empty';
      box.textContent = 'Could not load suggestions — you can still add relations manually below.';
      return;
    }
    WIZ_SUGGESTIONS = r.data.candidates || [];
    renderWizardSuggestions();
  }

  function renderWizardSuggestions() {
    const box = $('twSuggestions');
    if (!WIZ_SUGGESTIONS.length) {
      // Panel stays visible with the WHY — a collapsed line reads as "the
      // feature doesn't exist" (live-testing finding).
      box.className = 'adm-muted adm-rel-band-empty';
      box.textContent = 'No suggested relations found — suggestions appear when '
        + "this table's foreign keys or column names match another registered table.";
      return;
    }
    box.className = '';
    box.innerHTML = WIZ_SUGGESTIONS.map((c, i) => {
      const pairs = (c.join_keys || []).map((pair) =>
        `<strong>${esc(c.table_label)}</strong>.${esc(pair[0])} → ` +
        `<strong>${esc(c.related_label)}</strong>.${esc(pair[1])}`).join('<br>');
      const chips = [];
      chips.push(cardChip(c.cardinality));
      if (c.verified && c.overlap_pct != null) {
        chips.push(`<span class="adm-chip">≈${esc(c.overlap_pct)}% key match (estimated from sample) · ${esc(Number(c.orphans).toLocaleString())} orphan(s)</span>`);
      } else if (!c.verified) {
        chips.push(`<span class="adm-chip bad" title="${esc(c.unverified_reason || '')}">unverified</span>`);
      }
      (c.sources || []).forEach((s) => chips.push(originChip(s)));
      if (altNote(c)) chips.push(altNote(c));
      return `
        <div class="adm-rel-cand adm-rel-suggestion" data-idx="${i}">
          <input type="checkbox" class="tw-sugg-check" ${c.precheck ? 'checked' : ''}>
          <div class="adm-rel-cand-main">
            <div class="adm-rel-cand-title">${pairs}</div>
            <div class="adm-rel-cand-meta">${chips.join(' ')}</div>
          </div>
        </div>`;
    }).join('');
  }

  function collectCheckedSuggestions() {
    const out = [];
    document.querySelectorAll('#twSuggestions .adm-rel-suggestion').forEach((row) => {
      if (!row.querySelector('.tw-sugg-check').checked) return;
      const c = WIZ_SUGGESTIONS[parseInt(row.dataset.idx, 10)];
      if (!c) return;
      const rel = { related_table_id: c.related_table_id, join_keys: c.join_keys };
      if ((c.sources || []).length) rel.origin = c.sources[0];
      if (c.cardinality) rel.cardinality = c.cardinality;
      out.push(rel);
    });
    return out;
  }

  // ── Relations: confirmed overview (Zone A) ─────────────────────────────
  const REL_CARD_LABEL = { 'N:1': 'many-to-one', '1:1': 'one-to-one',
                           '1:N': 'one-to-many', 'N:M': 'many-to-many' };
  const REL_ORIGINS = ['fk', 'sql', 'name', 'description'];
  const REL_ORIGIN_TIP = {
    fk: 'Declared foreign key in the source database',
    sql: 'Seen in analyzed SQL joins',
    name: 'Matched by column-name similarity',
    description: 'Matched by column-description similarity',
    manual: 'Added manually',
  };
  const REL_CARD_TIP = {
    'N:1': 'Many-to-one: many rows on the left match one row on the right',
    '1:1': 'One-to-one: rows match pairwise',
    '1:N': 'One-to-many: one row on the left matches many rows on the right',
    'N:M': 'Many-to-many: neither side is unique — usually not a real join key',
  };

  function originChip(origin) {
    return `<span class="adm-chip conn" title="${esc(REL_ORIGIN_TIP[origin] || '')}">${esc(origin)}</span>`;
  }
  function cardChip(card) {
    if (!card) return '';
    return `<span class="adm-chip" title="${esc(REL_CARD_TIP[card] || '')}">${esc(REL_CARD_LABEL[card] || card)}</span>`;
  }
  function altNote(c) {
    const alts = c.alternate_targets || [];
    if (!alts.length) return '';
    return `<span class="adm-muted adm-rel-alt" title="This physical table has duplicate registrations; the preferred one was chosen — use Edit to retarget.">also registered as: ${esc(alts.map((a) => a.label).join(', '))}</span>`;
  }

  function _physLabel(t) {
    return t ? `${t.schema ? t.schema + '.' : ''}${t.table_name}` : '';
  }
  function _samePhysicalDocs(a, b) {
    return !!(a && b && a.connection_id && b.connection_id
      && a.connection_id === b.connection_id
      && String(a.schema || '').toLowerCase() === String(b.schema || '').toLowerCase()
      && String(a.table_name || '').toLowerCase() === String(b.table_name || '').toLowerCase());
  }

  function confirmedRelCount() {
    let n = 0;
    TABLES.forEach((t) => (t.relations || []).forEach((r) => {
      if (r && typeof r === 'object') n++;
    }));
    return n;
  }

  function relOverviewEntries() {
    const byId = (tid) => TABLES.find((t) => t.id === tid) || null;
    const entries = [];
    TABLES.forEach((t) => (t.relations || []).forEach((rel) => {
      if (!rel || typeof rel !== 'object') return;
      const rid = rel.related_table_id || rel.related_table || '';
      const parentDoc = byId(rel.related_table_id);
      // Suspicious = both sides are registrations of ONE physical table (the
      // duplicate-registration noise). Legacy name refs flag when the name
      // equals the child's own physical name.
      const suspicious = parentDoc
        ? _samePhysicalDocs(t, parentDoc)
        : (!rel.related_table_id && String(rid).toLowerCase() === _physLabel(t).toLowerCase());
      // Dangling = an id ref whose registration was deleted: the relation
      // resolves to nothing downstream — surface it, don't show a raw hex id.
      const dangling = !!rel.related_table_id && !parentDoc;
      entries.push({
        table_id: t.id,
        child_label: t.display_name,
        child_phys: _physLabel(t),
        related_ref: String(rid),
        related_is_id: !!rel.related_table_id,
        related_label: (parentDoc || {}).display_name
          || (dangling ? '(deleted registration)' : String(rid) || '?'),
        parent_phys: parentDoc ? _physLabel(parentDoc)
          : (dangling ? `was id ${String(rid).slice(0, 8)}…` : String(rid)),
        dangling,
        join_keys: (rel.join_keys || [])
          .filter((p) => Array.isArray(p) && p.length === 2)
          .map((p) => [String(p[0]), String(p[1])]),
        cardinality: rel.cardinality || null,
        origin: rel.origin || 'manual',       // pre-discovery entries had no origin
        suspicious,
      });
    }));
    return entries;
  }

  function renderRelOverview() {
    const list = $('relConfirmedList');
    if (!list) return;
    const entries = relOverviewEntries();
    $('relConfirmedEmpty').classList.toggle('hidden', !!entries.length);
    $('btnRelDeleteFlagged').classList.toggle('hidden',
      !entries.some((e) => e.suspicious));
    // Grouped by table pair: one group card per child→parent, each key-set
    // variant (exact duplicates, subsets, composites) as a sub-row inside it.
    const groups = new Map();
    entries.forEach((e, i) => {
      const k = e.table_id + '|' + e.related_ref;
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(i);
    });
    list.innerHTML = Array.from(groups.values()).map((idxs) => {
      const first = entries[idxs[0]];
      const head = `
        <div class="adm-rel-group-head">
          <span><strong>${esc(first.child_label)}</strong>
            <span class="adm-cell-sub">${esc(first.child_phys)}</span></span>
          <span class="adm-rel-arrow">→</span>
          <span><strong>${esc(first.related_label)}</strong>
            <span class="adm-cell-sub">${esc(first.parent_phys)}</span></span>
        </div>`;
      const rows = idxs.map((i) => {
        const e = entries[i];
        const keys = e.join_keys.map((p) => `${esc(p[0])} = ${esc(p[1])}`).join(', ');
        const chips = [];
        if (e.suspicious) chips.push('<span class="adm-chip bad" title="Both sides are registrations of the same physical source table — this relation joins a table to its own copy and is almost certainly noise. Delete it.">⚠ same physical table</span>');
        if (e.dangling) chips.push('<span class="adm-chip bad" title="The registration this relation points at was deleted — the AI receives no join hint from it. Delete it, or re-create it against the current registration.">⚠ target registration deleted</span>');
        chips.push(cardChip(e.cardinality));
        chips.push(originChip(e.origin));
        return `
          <div class="adm-rel-cand" data-idx="${i}">
            <div class="adm-rel-cand-main">
              <div class="adm-rel-cand-title">${keys || '—'}</div>
              <div class="adm-rel-cand-meta">${chips.filter(Boolean).join(' ')}</div>
            </div>
            <div class="adm-rel-cand-actions">
              <button class="adm-icon-btn" data-act="edit" title="Edit relation">✎</button>
              <button class="adm-icon-btn" data-act="delete" title="Delete relation">🗑</button>
            </div>
          </div>`;
      }).join('');
      return `<div class="adm-rel-group">${head}${rows}</div>`;
    }).join('');
    list.querySelectorAll('.adm-rel-cand').forEach((row) => {
      const entry = entries[parseInt(row.dataset.idx, 10)];
      row.querySelectorAll('button').forEach((b) => {
        b.addEventListener('click', () => (b.dataset.act === 'edit'
          ? relOverviewEdit(entry, row) : relOverviewDelete(entry)));
      });
    });
  }

  async function deleteFlaggedRelations() {
    const flagged = relOverviewEntries().filter((e) => e.suspicious);
    if (!flagged.length) return;
    if (!window.confirm(`Delete ${flagged.length} flagged relation(s)? Each joins a table to its own duplicate registration and carries no information.`)) return;
    _busy('Deleting flagged relations…');
    let failed = 0;
    try {
      for (const e of flagged) {
        const body = { table_id: e.table_id, join_keys: e.join_keys };
        if (e.related_is_id) body.related_table_id = e.related_ref;
        else body.related_table = e.related_ref;
        try {
          const r = await api('/api/admin/relations/delete', {
            method: 'POST', body: JSON.stringify(body) });
          // delete removes ALL exact matches, so an identical duplicate may
          // 404 on the second call — that one is expected, not a failure.
          if (!(r.ok || r.status === 404)) failed++;
        } catch (err) { failed++; }
      }
    } finally { _busyDone(); }
    if (failed) toast(`Deleted with ${failed} failure(s) — check the audit log`, true);
    else toast(`Deleted ${flagged.length} flagged relation(s)`);
    loadAll();
  }

  // ── Overview "+ Add relation" (structured, manual) ─────────────────────
  function relOverviewAdd() {
    const box = $('relAddForm');
    if (!box.classList.contains('hidden')) { box.classList.add('hidden'); box.innerHTML = ''; return; }
    if (TABLES.length < 2) { toast('Register at least two tables first', true); return; }
    box.classList.remove('hidden');
    box.className = 'adm-rel-addform';
    const opts = (skip) => TABLES.filter((t) => t.id !== skip)
      .map((t) => `<option value="${esc(t.id)}">${esc(t.display_name)}</option>`).join('');
    box.innerHTML = `
      <div class="adm-rel-row-head">
        <select class="rel-add-child"><option value="">— table —</option>${opts(null)}</select>
        <span>joins</span>
        <select class="rel-add-target"><option value="">— target table —</option></select>
        <span>on</span>
      </div>`;
    const childSel = box.querySelector('.rel-add-child');
    const targetSel = box.querySelector('.rel-add-target');
    let editor = null;
    const actions = document.createElement('div');
    actions.className = 'adm-rel-cand-actions';
    actions.innerHTML = `
      <button class="adm-btn primary small" data-act="save" disabled>Save relation</button>
      <button class="adm-btn ghost small" data-act="cancel">Cancel</button>`;
    const saveBtn = actions.querySelector('[data-act="save"]');

    function rebuildEditor() {
      if (editor) editor.el.remove();
      editor = createPairEditor({
        leftCols: tableCols(childSel.value),
        rightCols: tableCols(targetSel.value),
        pairs: [],
        onChange: () => {
          saveBtn.disabled = !childSel.value || !targetSel.value
            || !editor.getPairs().length || editor.hasMissing();
        },
      });
      box.insertBefore(editor.el, actions);
      saveBtn.disabled = true;
    }
    childSel.addEventListener('change', () => {
      // child excluded from its own target list
      targetSel.innerHTML = `<option value="">— target table —</option>${opts(childSel.value)}`;
      rebuildEditor();
    });
    targetSel.addEventListener('change', () => {
      editor.setRightCols(tableCols(targetSel.value));
      saveBtn.disabled = true;
    });
    box.appendChild(actions);
    rebuildEditor();
    actions.querySelector('[data-act="cancel"]').addEventListener('click', () => {
      box.classList.add('hidden');
      box.innerHTML = '';
    });
    saveBtn.addEventListener('click', async () => {
      const jk = editor.getPairs();
      if (!childSel.value || !targetSel.value || !jk.length) return;
      const r = await api('/api/admin/relations/accept', {
        method: 'POST',
        body: JSON.stringify({ relations: [{
          table_id: childSel.value, related_table_id: targetSel.value,
          join_keys: jk, cardinality: null, origin: null }] }),
      });
      if (!r.data.ok) { toast(r.data.error || 'Save failed', true); return; }
      toast(r.data.skipped ? 'Not saved — an identical relation already exists'
                           : 'Relation added', !!r.data.skipped);
      box.classList.add('hidden');
      box.innerHTML = '';
      loadAll();
    });
  }

  // ── Recommended tables (persistent — SQL + FK evidence) ────────────────
  let REL_RECS = [];            // server-persisted recommendations
  let relShowDismissed = false;

  async function loadRelRecommendations() {
    const r = await api('/api/admin/relations/recommendations');
    if (r.data && r.data.ok) {
      REL_RECS = r.data.recommendations || [];
      renderRelRecommended();
    }
  }

  function _recPhysLabel(rec) {
    return rec.schema ? `${rec.schema}.${rec.table}` : rec.table;
  }

  // Pasted-SQL evidence warnings (analyze-time invalid_column_refs and
  // scan/accept-time evidence_warnings) — a dedicated box, deliberately NOT
  // #relDegraded (that one is scan-owned and overwritten wholesale).
  function _renderRelWarnings(lines) {
    const box = $('relWarnings');
    if (!box) return;
    box.classList.toggle('hidden', !lines.length);
    box.innerHTML = lines.length
      ? '<strong>Some pasted-SQL join references don’t match the registered '
        + 'tables</strong> — the script may be outdated or wrong; treat its '
        + 'evidence with caution:<br>' + lines.map(esc).join('<br>')
      : '';
  }

  function _warnLinesFromAnalyze(refs) {
    return (refs || []).map((r) =>
      `statement ${r.statement}: “${r.table}” has no column “${r.column}”`);
  }

  function _warnLinesFromReplay(warns) {
    return (warns || []).map((w) =>
      `“${w.table}” has no column “${w.column}” (stored pasted-SQL evidence)`);
  }

  function _recEvidenceLine(rec) {
    const joins = (rec.joins || []).map((j) =>
      `<strong>${esc(j.label)}</strong>${(j.cols || []).length ? ' (' + esc(j.cols.join(', ')) + ')' : ''}`
      + (j.registered === false ? ' <span class="adm-rel-notreg">not registered</span>' : ''));
    const parts = [];
    if (joins.length) parts.push('joins ' + joins.join(' and '));
    if (rec.frequency > 0) parts.push(`seen in ${esc(rec.frequency)} statement(s)`);
    return parts.join(' · ') || 'referenced by your team’s SQL or foreign keys';
  }

  // Locked preview of the relations that WILL be proposed once the blocking
  // table(s) register — rendered from stored evidence, non-interactive.
  function _recPendingRows(rec) {
    return (rec.pending || []).map((p) =>
      `<span class="adm-rel-pending-row" title="Becomes a normal proposed candidate once the blocking table is registered">🔒 ${esc(p.left)} → ${esc(p.right)}`
      + ` <span class="adm-muted">— pending: register ${esc((p.blocked_by || []).join(', '))}</span></span>`).join('');
  }

  function renderRelRecommended() {
    const box = $('relRecommended');
    if (!box) return;
    const open = REL_RECS.filter((r) => r.status === 'open');
    const dismissed = REL_RECS.filter((r) => r.status === 'dismissed');
    box.classList.toggle('hidden', !open.length && !dismissed.length);
    if (!open.length && !dismissed.length) { box.innerHTML = ''; return; }
    const shown = open.concat(relShowDismissed ? dismissed : []);
    const rows = shown.map((rec) => {
      const badges = [
        `<span class="adm-chip ${rec.role === 'bridge' ? 'ok' : ''}" title="${rec.role === 'bridge'
          ? 'Joins two or more registered tables — registering it connects them'
          : 'Referenced by one registered table'}">${esc(rec.role)}</span>`,
      ].concat((rec.sources || []).map(originChip));
      // Session-local: set once the accept dialog has classified this table.
      // Listing recommendations never probes the database on its own.
      if (rec._typeHint) {
        badges.push(`<span class="adm-chip${rec._typeHint === 'connector' ? ' conn' : ''}"
          title="Suggested registration type — you confirm or change it when accepting"
          >${rec._typeHint === 'connector' ? 'connector?' : 'normal?'}</span>`);
      }
      if ((rec.evidence_warnings || []).length) {
        badges.push(`<span class="adm-chip bad" title="${esc((rec.evidence_warnings || [])
          .map((w) => `“${w.table}” has no column “${w.column}”`).join('; '))
          } — the pasted SQL may be outdated or wrong; these pairs are excluded">⚠ evidence</span>`);
      }
      const actions = rec.status === 'dismissed'
        ? '<button class="adm-btn ghost small" data-act="restore">Restore</button>'
        : `<button class="adm-btn primary small" data-act="accept">✔ Accept</button>
           <button class="adm-btn ghost small" data-act="edit"
             title="Review in the register wizard before saving">Edit first</button>
           <button class="adm-icon-btn" data-act="dismiss"
             title="Dismiss — it will not reappear until restored">✕</button>`;
      return `<div class="adm-rel-ghost-row${rec.status === 'dismissed' ? ' adm-rel-rec-dismissed' : ''}"
          data-rid="${esc(rec.id)}">
          <span><strong>${esc(_recPhysLabel(rec))}</strong> ${badges.join(' ')}<br>
            <span class="adm-muted">${_recEvidenceLine(rec)}</span>${_recPendingRows(rec)}</span>
          <span class="adm-rel-rec-actions">${actions}</span>
        </div>`;
    });
    const toggle = dismissed.length
      ? `<button id="relRecShowDismissed" class="adm-btn ghost small">${relShowDismissed
          ? 'Hide' : 'Show'} dismissed (${dismissed.length})</button>`
      : '';
    box.innerHTML = '<div class="adm-subhead"><h4>Recommended tables <span class="adm-label-note">'
      + '(referenced by your SQL or foreign keys but not registered — Accept registers one in a '
      + 'single click)</span></h4></div>' + rows.join('') + toggle;
    box.querySelectorAll('[data-act]').forEach((b) => {
      const rec = REL_RECS.find((r) => r.id === b.closest('.adm-rel-ghost-row').dataset.rid);
      if (!rec) return;
      const act = b.dataset.act;
      if (act === 'accept') b.addEventListener('click', () => acceptRecommendation(rec));
      else if (act === 'edit') b.addEventListener('click', () => registerGhost({
        connection_id: rec.connection_id, schema: rec.schema, table: rec.table }));
      else if (act === 'dismiss') b.addEventListener('click', () => setRecStatus(rec, 'dismissed'));
      else if (act === 'restore') b.addEventListener('click', () => setRecStatus(rec, 'open'));
    });
    const tog = $('relRecShowDismissed');
    if (tog) {
      tog.addEventListener('click', () => {
        relShowDismissed = !relShowDismissed;
        renderRelRecommended();
      });
    }
  }

  async function setRecStatus(rec, status) {
    const r = await api('/api/admin/relations/recommendations/status', {
      method: 'POST', body: JSON.stringify({ id: rec.id, status }) });
    if (!r.data.ok) { toast(r.data.error || 'Update failed', true); return; }
    loadRelRecommendations();
  }

  // ── Accept dialog ────────────────────────────────────────────────────────
  // The type is SUGGESTED (from column names/dtypes, server-side) and always
  // confirmed here: registering a content table as a connector would hide it
  // from users, so this may never be applied silently.
  let recAcceptCtx = null;      // { rec, suggested, dirty }

  function _closeRecAccept() {
    $('recAcceptModal').classList.add('hidden');
    recAcceptCtx = null;
  }

  function _setRecType(type) {
    $(type === 'normal' ? 'recTypeNormal' : 'recTypeConnector').checked = true;
  }

  function acceptRecommendation(rec) {
    const phys = _recPhysLabel(rec);
    recAcceptCtx = { rec, suggested: null, dirty: false };
    $('recAcceptText').innerHTML = `Register <strong>${esc(phys)}</strong>? `
      + 'Descriptions are AI-drafted — you can edit them later in the table’s '
      + 'settings (Registered tables → ✎). A snapshot is taken and its relations '
      + 'are proposed for review right away.';
    // Historical default until the probe answers; never gate the button on it.
    _setRecType('connector');
    $('recAcceptReason').textContent = 'Checking column types…';
    $('btnRecAcceptGo').disabled = false;
    $('recAcceptModal').classList.remove('hidden');
    _classifyRecommendation(rec);
  }

  async function _classifyRecommendation(rec) {
    // One probe per recommendation per session: it costs a live
    // introspection on a 4-worker pool, and reopening the dialog does not
    // make the table's columns any newer.
    if (rec._classification) { _applyClassification(rec, rec._classification); return; }
    const r = await apiTimed('/api/admin/relations/recommendations/classify',
      { method: 'POST', body: JSON.stringify({ id: rec.id }) }, CLASSIFY_TIMEOUT_MS);
    const out = {
      classified: !!(r.data && r.data.classified),
      suggested_type: (r.data && r.data.suggested_type) || 'connector',
      reason: (r.data && r.data.reason)
        || 'could not classify — defaulted to connector',
    };
    if (out.classified) rec._classification = out;   // never cache a failure
    _applyClassification(rec, out);
  }

  function _applyClassification(rec, out) {
    // The admin may have moved on (closed the dialog, or picked a type).
    if (!recAcceptCtx || recAcceptCtx.rec.id !== rec.id) return;
    // Only a REAL classification is reported as a suggestion — in the audit
    // row and as a list chip alike. A fallback means "we could not tell",
    // and recording it as advice the admin overrode would be a lie.
    if (out.classified) {
      recAcceptCtx.suggested = out.suggested_type;
      rec._typeHint = out.suggested_type;
    }
    if (!recAcceptCtx.dirty) _setRecType(out.suggested_type);
    $('recAcceptReason').textContent = out.classified
      ? `Suggested: ${out.suggested_type === 'connector'
          ? 'connector' : 'normal table'} — ${out.reason}`
      : out.reason;
    renderRelRecommended();
  }

  async function _runAcceptRecommendation() {
    if (!recAcceptCtx) return;
    const { rec, suggested } = recAcceptCtx;
    const chosen = $('recTypeNormal').checked ? 'normal' : 'connector';
    const phys = _recPhysLabel(rec);
    _closeRecAccept();
    _busy(`Registering ${phys}… introspecting, drafting descriptions, snapshotting.`);
    // Long DB/AI work is legitimate; silence is not. Say so at 20s.
    const warn = setTimeout(() => {
      $('admBusyMsg').textContent = 'Still working — AI drafting and snapshotting '
        + 'can take a few minutes on large tables…';
    }, ACCEPT_WARN_MS);
    let r;
    try {
      r = await apiTimed('/api/admin/relations/recommendations/accept', {
        method: 'POST',
        body: JSON.stringify({ id: rec.id, chosen_type: chosen,
          suggested_type: suggested }),
      }, ACCEPT_ABORT_MS);
    } finally { clearTimeout(warn); _busyDone(); }
    if (r.aborted) {
      // Our wait ended; the server's work did not. Say exactly that.
      toast('Timed out waiting for the server — it may still finish in the '
        + 'background. Reload the Relations section in a minute.', true);
      loadRelRecommendations();
      return;
    }
    if (!r.data.ok) {
      toast(r.data.error || 'Registration failed — the recommendation stays open', true);
      loadRelRecommendations();
      return;
    }
    toast(r.data.note || `Registered "${phys}" as a ${chosen === 'connector'
      ? 'connector' : 'normal table'} — review its proposed relations below`);
    mergeRelCandidates(r.data.candidates || []);
    renderRelCandidates();
    _renderRelWarnings(_warnLinesFromReplay(r.data.evidence_warnings));
    loadRelRecommendations();
    loadAll();
  }

  function registerGhost(g) {
    // connector: null -> the wizard pre-ticks from the same suggestion
    // instead of assuming every recommended table is a connector.
    openTableWizard(g.connection_id, null, false,
      { schema: g.schema || '', table: g.table, connector: null });
  }

  function renderRelUnknowns(unknown, hints) {
    // Only names that could NOT become recommendations render here: no
    // resolvable connection (plain text), or a resolvable hint without any
    // join evidence (register shortcut). Names already covered by a
    // recommendation row are dropped — one table, one row.
    const box = $('relUnknownList');
    const hintByName = {};
    (hints || []).forEach((h) => { hintByName[h.name] = h; });
    const recKeys = new Set(REL_RECS.map((r) =>
      `${r.connection_id}|${String(r.schema || '').toLowerCase()}|${String(r.table || '').toLowerCase()}`));
    const names = (unknown || []).filter((name) => {
      const h = hintByName[name];
      return !(h && recKeys.has(
        `${h.connection_id}|${String(h.schema || '').toLowerCase()}|${String(h.table || '').toLowerCase()}`));
    });
    const rows = names.map((name) => {
      const h = hintByName[name];
      const btn = h ? '<button class="adm-btn ghost small" data-act="register">＋ Register as connector</button>' : '';
      return `<div class="adm-rel-ghost-row" data-name="${esc(name)}">
        <span><strong>${esc(name)}</strong> appears in the SQL but is not a registered table.</span>${btn}</div>`;
    });
    box.classList.toggle('hidden', !rows.length);
    box.innerHTML = rows.length
      ? '<div class="adm-subhead"><h4>Tables in the SQL that are not registered</h4></div>' + rows.join('')
      : '';
    box.querySelectorAll('[data-act="register"]').forEach((b) => {
      const name = b.closest('.adm-rel-ghost-row').dataset.name;
      const h = hintByName[name];
      b.addEventListener('click', () => registerGhost({
        connection_id: h.connection_id, schema: h.schema, table: h.table }));
    });
  }

  async function relOverviewDelete(e) {
    // Name the join keys — several relations can link the same table pair.
    const keys = e.join_keys.map((p) => `${p[0]}=${p[1]}`).join(', ');
    if (!window.confirm(`Delete the relation ${e.child_label} → ${e.related_label} on ${keys}? The AI stops receiving this join hint.`)) return;
    const body = { table_id: e.table_id, join_keys: e.join_keys };
    if (e.related_is_id) body.related_table_id = e.related_ref;
    else body.related_table = e.related_ref;
    const r = await api('/api/admin/relations/delete', {
      method: 'POST', body: JSON.stringify(body) });
    if (!r.data.ok) { toast(r.data.error || 'Delete failed', true); return; }
    toast('Relation deleted');
    loadAll();
  }

  // Same inline editor as candidates (target select + "a=b, c=d" keys); the
  // save goes through /relations/accept with `replaces` so old→new swaps in
  // one write. An edit colliding with another existing relation is skipped
  // server-side WITH the old entry preserved.
  function relOverviewEdit(e, row) {
    const options = TABLES.filter((t) => t.id !== e.table_id)
      .map((t) => `<option value="${esc(t.id)}" ${e.related_is_id && e.related_ref === t.id ? 'selected' : ''}>${esc(t.display_name)}</option>`)
      .join('');
    const main = row.querySelector('.adm-rel-cand-main');
    main.innerHTML = `
      <div class="adm-rel-row-head">
        <span><strong>${esc(e.child_label)}</strong> joins</span>
        <select class="tw-rel-target"><option value="">— pick table —</option>${options}</select>
        <span>on</span>
      </div>`;
    const actions = row.querySelector('.adm-rel-cand-actions');
    actions.innerHTML = `
      <button class="adm-btn primary small" data-act="save">Save</button>
      <button class="adm-btn ghost small" data-act="cancel">Cancel</button>`;
    const saveBtn = actions.querySelector('[data-act="save"]');
    const editor = createPairEditor({
      leftCols: tableCols(e.table_id),
      rightCols: e.related_is_id ? tableCols(e.related_ref) : [],
      pairs: e.join_keys,
      // accept 400s on columns absent from the registry — gate the save
      onChange: () => { saveBtn.disabled = editor.hasMissing(); },
    });
    main.appendChild(editor.el);
    saveBtn.disabled = editor.hasMissing();
    const targetSel = main.querySelector('.tw-rel-target');
    targetSel.addEventListener('change', () => editor.setRightCols(tableCols(targetSel.value)));
    saveBtn.addEventListener('click', async () => {
      const target = targetSel.value;
      const jk = editor.getPairs();
      if (!target || !jk.length) { toast('Pick a table and at least one column pair', true); return; }
      const unchanged = e.related_is_id && target === e.related_ref
        && JSON.stringify(jk) === JSON.stringify(e.join_keys);
      const item = {
        table_id: e.table_id, related_table_id: target, join_keys: jk,
        // measured cardinality/provenance only hold for the original
        // target+keys — a changed relation reverts to manual
        cardinality: unchanged ? e.cardinality : null,
        origin: unchanged && REL_ORIGINS.includes(e.origin) ? e.origin : null,
        replaces: Object.assign({ join_keys: e.join_keys },
          e.related_is_id ? { related_table_id: e.related_ref }
                          : { related_table: e.related_ref }),
      };
      const r = await api('/api/admin/relations/accept', {
        method: 'POST', body: JSON.stringify({ relations: [item] }) });
      if (!r.data.ok) { toast(r.data.error || 'Save failed', true); return; }
      toast(r.data.skipped ? 'Not saved — an identical relation already exists'
                           : 'Relation updated', !!r.data.skipped);
      loadAll();
    });
    actions.querySelector('[data-act="cancel"]').addEventListener('click', renderRelOverview);
  }

  // ── Relations graph view (vendored Cytoscape, never a CDN) ─────────────
  let cyInstance = null;
  let relView = 'list';
  const REL_COMPONENT_COLORS = ['#1276C2', '#059669', '#9333ea', '#d97706',
    '#0e7490', '#be185d', '#4d7c0f', '#7c3aed', '#0f766e', '#b91c1c'];
  const REL_ISOLATED_COLOR = '#dc2626';
  const REL_GHOST_COLOR = '#94a3b8';

  // ER line-end cardinality text (the server picks "one"/"many" per end).
  const REL_END_TEXT = { one: '1', many: 'N' };
  // Layered left-to-right: text-sized cards are wide, so ranks read as tidy
  // columns and the mostly-horizontal edges suit the horizontal key chips.
  const REL_DAGRE_LAYOUT = { name: 'dagre', rankDir: 'LR', nodeSep: 30,
    rankSep: 110, edgeSep: 20, padding: 30, animate: false };
  const REL_FALLBACK_LAYOUT = { name: 'breadthfirst', directed: true,
    spacingFactor: 1.5, padding: 30, animate: false };

  let _cyLoadPromise = null;
  let _dagreOk = false;          // false -> the built-in fallback layout

  function relLayoutConfig() {
    return _dagreOk ? REL_DAGRE_LAYOUT : REL_FALLBACK_LAYOUT;
  }

  function _runRelLayout() {
    // The layered layout is an enhancement; a throw inside it must never
    // leave the admin staring at an unlaid-out pile (Article IV).
    try {
      cyInstance.layout(relLayoutConfig()).run();
    } catch (e) {
      console.warn('REL_GRAPH layout failed — breadthfirst fallback', e);
      _dagreOk = false;
      cyInstance.layout(REL_FALLBACK_LAYOUT).run();
    }
  }

  function _loadScript(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = src;
      s.onload = resolve;
      s.onerror = () => reject(new Error(`load failed: ${src}`));
      document.head.appendChild(s);
    });
  }

  function ensureCytoscape() {
    if (_cyLoadPromise) return _cyLoadPromise;   // single-flight
    _cyLoadPromise = (async () => {
      if (!window.cytoscape) {
        // Core failure REJECTS — refreshRelGraph falls back to the list view.
        await _loadScript('/static/vendor/cytoscape/cytoscape.min.js');
      }
      // The layered layout is an ENHANCEMENT: losing it must never cost the
      // admin the graph (Article IV), so this branch never rejects. dagre
      // first — cytoscape-dagre reads it off the global.
      try {
        if (!window.dagre) await _loadScript('/static/vendor/dagre/dagre.min.js');
        if (!window.cytoscapeDagre) {
          await _loadScript('/static/vendor/cytoscape-dagre/cytoscape-dagre.js');
        }
        window.cytoscape.use(window.cytoscapeDagre);
        _dagreOk = true;
      } catch (e) {
        // Deliberately sticky for the session: the resolved promise stays
        // cached, so a missing extension is not re-requested on every toggle.
        // A page reload retries it; the fallback layout works meanwhile.
        console.warn('REL_GRAPH dagre unavailable — falling back to breadthfirst', e);
      }
    })().catch((e) => { _cyLoadPromise = null; throw e; });
    return _cyLoadPromise;
  }

  function setRelView(view) {
    relView = view;
    $('btnRelViewList').classList.toggle('active', view === 'list');
    $('btnRelViewGraph').classList.toggle('active', view === 'graph');
    $('relConfirmedCard').classList.toggle('hidden', view !== 'list');
    $('relGraphCard').classList.toggle('hidden', view !== 'graph');
    if (view === 'graph') {
      refreshRelGraph();
    } else if (cyInstance) {
      cyInstance.destroy();
      cyInstance = null;
    }
  }

  async function refreshRelGraph() {
    try {
      await ensureCytoscape();
    } catch (e) {
      toast('Could not load the graph library', true);
      setRelView('list');
      return;
    }
    // Ghosts come from the server-persisted open recommendations now — the
    // body's unregistered_refs param stays accepted server-side (compat).
    const r = await api('/api/admin/relations/graph', {
      method: 'POST', body: '{}' });
    if (!r.data.ok) { toast(r.data.error || 'Could not load the graph', true); return; }
    renderRelGraph(r.data.nodes || [], r.data.edges || []);
  }

  function _hideGraphPopover() {
    $('relGraphPopover').classList.add('hidden');
  }

  function _relGraphZoom(factor) {
    if (!cyInstance) return;
    _hideGraphPopover();
    const box = $('relGraph').getBoundingClientRect();
    // Zoom about the viewport centre so the diagram stays put.
    cyInstance.zoom({ level: cyInstance.zoom() * factor,
      renderedPosition: { x: box.width / 2, y: box.height / 2 } });
  }

  function renderRelGraph(nodes, edges) {
    if (cyInstance) { cyInstance.destroy(); cyInstance = null; }
    _hideGraphPopover();
    const elements = [];
    nodes.forEach((n) => {
      // The cluster color is an ACCENT (border), never a fill — a solid fill
      // made every table in a single-cluster registry one identical blob.
      const accent = n.ghost ? REL_GHOST_COLOR
        : n.isolated ? REL_ISOLATED_COLOR
        : REL_COMPONENT_COLORS[(n.component || 0) % REL_COMPONENT_COLORS.length];
      elements.push({ data: {
        id: n.id,
        display: n.label + (n.connector ? ' ⚙' : '') + '\n' + (n.sub || ''),
        accent,
        connector: n.connector ? 1 : 0,
        ghost: n.ghost ? 1 : 0,
        isolated: n.isolated ? 1 : 0,
        connection_id: n.connection_id || '', schema: n.schema || '',
        table: n.table || '', label: n.label,
      } });
    });
    edges.forEach((e) => {
      // Cardinality is read off the LINE ENDS (ER convention). The server
      // decides the markers; here they only become cytoscape properties.
      // A "one" end fuses the bar into the direction arrow (triangle-tee).
      elements.push({ data: {
        id: e.id, source: e.source, target: e.target,
        elabel: e.label || e.keys_label || '',
        srcLabel: REL_END_TEXT[e.source_marker] || '',
        tgtLabel: e.target_marker === 'many' ? REL_END_TEXT.many : '',
        tgtArrow: e.target_marker === 'one' ? 'triangle-tee' : 'triangle',
        ghost: e.ghost ? 1 : 0,
        suspicious: e.suspicious ? 1 : 0,
        // Everything below feeds the click popover's Edit/Delete — carried
        // forward verbatim.
        keys_label: e.keys_label || '', cardinality: e.cardinality || '',
        origin: e.origin || 'manual', join_keys: e.join_keys || [],
        related_ref: e.related_ref || '', related_is_id: e.related_is_id ? 1 : 0,
      } });
    });
    cyInstance = cytoscape({
      container: $('relGraph'),
      elements,
      minZoom: 0.2,
      maxZoom: 2.5,
      style: [
        // Table CARD: name + schema.table inside the box, sized to the text.
        { selector: 'node', style: {
          shape: 'round-rectangle',
          width: 'label', height: 'label', padding: '10px',
          label: 'data(display)', 'text-wrap': 'wrap',
          'text-valign': 'center', 'text-halign': 'center',
          'font-size': 11, color: '#1e293b',
          'background-color': '#ffffff',
          'border-width': 2, 'border-color': 'data(accent)',
        } },
        { selector: 'node[connector = 1]', style: {
          'border-style': 'double', 'border-width': 4,   // double needs >= 3
        } },
        { selector: 'node[ghost = 1]', style: {
          'border-style': 'dashed', color: '#64748b',
          'background-color': '#f8fafc',
        } },
        { selector: 'edge', style: {
          'curve-style': 'bezier', width: 2, 'line-color': '#94a3b8',
          'target-arrow-shape': 'data(tgtArrow)', 'target-arrow-color': '#94a3b8',
          // Join COLUMNS only, horizontal, on a chip — the old rotated
          // "columns · cardinality" string truncated and was unreadable.
          label: 'data(elabel)', 'font-size': 10, color: '#475569',
          'text-rotation': 'none',
          'text-background-color': '#fff', 'text-background-opacity': 0.9,
          'text-background-padding': 3,
          'text-background-shape': 'round-rectangle',
          'source-label': 'data(srcLabel)', 'target-label': 'data(tgtLabel)',
          'source-text-offset': 20, 'target-text-offset': 24,
          'source-text-margin-y': -8, 'target-text-margin-y': -8,
          'font-weight': 'bold',
        } },
        { selector: 'edge[ghost = 1]', style: { 'line-style': 'dashed' } },
        { selector: 'edge[suspicious = 1]', style: {
          'line-style': 'dashed', 'line-color': REL_ISOLATED_COLOR,
          'target-arrow-color': REL_ISOLATED_COLOR,
        } },
        { selector: '.dim', style: { opacity: 0.15 } },
      ],
      // A REAL layout must run during construction: `preset` with no
      // positions parks every element at (0,0), and the renderer caches that
      // degenerate state — nodes and edges then stay unpainted even after a
      // later layout assigns real positions. `grid` is built-in and cannot
      // throw, so the guarded layered layout below can safely replace it.
      layout: { name: 'grid' },
    });

    cyInstance.on('tap', (evt) => {
      if (evt.target === cyInstance) {           // background tap: clear
        cyInstance.elements().removeClass('dim');
        _hideGraphPopover();
      }
    });
    // The popover is placed in card coordinates, so it would drift away from
    // its element on pan/zoom/drag. Hiding it is the honest fix.
    cyInstance.on('pan zoom', _hideGraphPopover);
    cyInstance.on('drag', 'node', _hideGraphPopover);
    cyInstance.on('tap', 'node', (evt) => {
      const n = evt.target;
      _hideGraphPopover();
      if (Number(n.data('ghost'))) {
        registerGhost({ connection_id: n.data('connection_id'),
          schema: n.data('schema'), table: n.data('table') });
        return;
      }
      cyInstance.elements().addClass('dim');
      n.closedNeighborhood().removeClass('dim');
      if (Number(n.data('isolated'))) {
        _showGraphPopover(evt.renderedPosition,
          `<strong>${esc(n.data('label'))}</strong><div class="adm-muted">Not joined to any
           table — the AI cannot combine it with others.</div>`);
      }
    });
    cyInstance.on('tap', 'edge', (evt) => {
      const e = evt.target;
      if (Number(e.data('ghost'))) return;
      const src = e.source(); const tgt = e.target();
      const card = e.data('cardinality');
      _showGraphPopover(evt.renderedPosition, `
        <strong>${esc(src.data('label'))}</strong> → <strong>${esc(tgt.data('label'))}</strong>
        <div class="adm-rel-cand-meta" style="margin:6px 0">
          <span class="adm-chip">${esc(e.data('keys_label') || '—')}</span>
          ${card ? cardChip(card) : ''} ${originChip(e.data('origin'))}
        </div>
        <div class="adm-rel-cand-actions">
          <button class="adm-btn ghost small" data-act="gedit">✎ Edit</button>
          <button class="adm-btn ghost small" data-act="gdelete">🗑 Delete</button>
        </div>`);
      const pop = $('relGraphPopover');
      pop.querySelector('[data-act="gedit"]').addEventListener('click', () => {
        _hideGraphPopover();
        openOverviewEditorFor(e.data());
      });
      pop.querySelector('[data-act="gdelete"]').addEventListener('click', async () => {
        if (!window.confirm(`Delete the relation ${src.data('label')} → ${tgt.data('label')} on ${e.data('keys_label')}?`)) return;
        const body = { table_id: e.data('source'), join_keys: e.data('join_keys') };
        if (Number(e.data('related_is_id'))) body.related_table_id = e.data('related_ref');
        else body.related_table = e.data('related_ref');
        const r = await api('/api/admin/relations/delete', {
          method: 'POST', body: JSON.stringify(body) });
        _hideGraphPopover();
        if (!r.data.ok) { toast(r.data.error || 'Delete failed', true); return; }
        toast('Relation deleted');
        loadAll();
      });
    });

    $('relGraphLegend').innerHTML = `
      <span class="lg"><span class="sw" style="border:2px solid ${REL_COMPONENT_COLORS[0]}"></span> table card — border color = its cluster of joined tables</span>
      <span class="lg"><span class="sw" style="border:2px solid ${REL_ISOLATED_COLOR}"></span> not joined to any table — the AI cannot combine it</span>
      <span class="lg"><span class="sw" style="border:3px double #0f172a"></span> ⚙ connector table</span>
      <span class="lg"><span class="sw" style="border:2px dashed ${REL_GHOST_COLOR}; background:#f8fafc"></span> referenced but not registered (click to register)</span>
      <span class="lg">line ends = cardinality: <strong>N</strong> = many, a <strong>bar across the arrow</strong> (or <strong>1</strong>) = exactly one; no marks = not measured</span>
      <span class="lg">→ points child → parent · the label on the line = the join columns</span>
      <span class="lg">dashed red line = joins a table to its own duplicate registration</span>`;

    // Last: the constructor's grid layout already painted a valid state, so
    // if the layered layout AND its fallback both fail, the admin is left
    // with a wired, clickable graph and its legend — not a bare canvas.
    _runRelLayout();
  }

  function _showGraphPopover(pos, html) {
    const pop = $('relGraphPopover');
    pop.innerHTML = html;
    pop.classList.remove('hidden');
    const host = $('relGraph').getBoundingClientRect();
    const card = $('relGraphCard').getBoundingClientRect();
    const left = Math.min(pos.x + (host.left - card.left) + 12,
                          card.width - 330);
    pop.style.left = Math.max(8, left) + 'px';
    pop.style.top = (pos.y + (host.top - card.top) + 12) + 'px';
  }

  function openOverviewEditorFor(d) {
    setRelView('list');
    renderRelOverview();
    const entries = relOverviewEntries();
    const idx = entries.findIndex((e) => e.table_id === d.source
      && e.related_ref === d.related_ref
      && JSON.stringify(e.join_keys) === JSON.stringify(d.join_keys));
    if (idx < 0) { toast('Relation not found — refreshing', true); loadAll(); return; }
    const row = $('relConfirmedList').querySelector(`[data-idx="${idx}"]`);
    if (row) {
      relOverviewEdit(entries[idx], row);
      row.scrollIntoView({ block: 'center' });
    }
  }

  // ── Relations: discovery (Zone B) ──────────────────────────────────────

  function renderRelDialects() {
    const sel = $('relSqlDialect');
    if (!sel) return;
    const current = sel.value;
    const types = Array.from(new Set(CONNECTIONS.map((c) => c.db_type))).sort();
    sel.innerHTML = '<option value="">auto</option>' +
      types.map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join('');
    if (types.includes(current)) sel.value = current;
  }

  function mergeRelCandidates(list, opts) {
    const replaceNonSql = !!(opts && opts.replaceNonSql);
    const byId = {};
    RELCANDIDATES.forEach((c) => {
      if (replaceNonSql && !(c.sources || []).includes('sql')) return;
      byId[c.candidate_id] = c;
    });
    (list || []).forEach((c) => {
      if (REL_DISMISSED.has(c.candidate_id)) return;
      const cur = byId[c.candidate_id];
      if (!cur) { byId[c.candidate_id] = c; return; }
      // Prefer the fresher entry but keep the union of evidence.
      const merged = Object.assign({}, cur, c);
      merged.sources = ['fk', 'sql', 'name', 'description']
        .filter((s) => (cur.sources || []).includes(s) || (c.sources || []).includes(s));
      merged.sql_frequency = Math.max(cur.sql_frequency || 0, c.sql_frequency || 0);
      merged.evidence = Object.assign({}, cur.evidence || {}, c.evidence || {});
      byId[c.candidate_id] = merged;
    });
    RELCANDIDATES = Object.values(byId);
  }

  function relCandRow(c) {
    const pairs = (c.join_keys || []).map((p) =>
      `<strong>${esc(c.table_label)}</strong>.${esc(p[0])} → ` +
      `<strong>${esc(c.related_label)}</strong>.${esc(p[1])}`).join('<br>');
    const chips = [];
    chips.push(cardChip(c.cardinality));
    if (c.verified) {
      chips.push(`<span class="adm-chip ${c.overlap_pct >= 95 ? 'ok' : ''}">${esc(c.overlap_pct)}% match · ${esc(Number(c.orphans).toLocaleString())} orphan(s)</span>`);
    } else {
      chips.push(`<span class="adm-chip bad" title="${esc(c.unverified_reason || '')}">unverified</span>`);
    }
    (c.sources || []).forEach((s) => chips.push(originChip(s)));
    if (c.sql_frequency > 0) chips.push(`<span class="adm-chip">×${esc(c.sql_frequency)} statement(s)</span>`);
    if (altNote(c)) chips.push(altNote(c));
    return `
      <div class="adm-rel-cand" data-cid="${esc(c.candidate_id)}">
        <input type="checkbox" class="rel-check" ${c.band === 'confirmed' ? 'checked' : ''}>
        <div class="adm-rel-cand-main">
          <div class="adm-rel-cand-title">${pairs}</div>
          <div class="adm-rel-cand-meta">${chips.join(' ')}</div>
        </div>
        <div class="adm-rel-cand-actions">
          <button class="adm-btn ghost small" data-act="accept">Accept</button>
          <button class="adm-icon-btn" data-act="edit" title="Edit before accepting">✎</button>
          <button class="adm-icon-btn" data-act="dismiss" title="Dismiss candidate">✕</button>
        </div>
      </div>`;
  }

  function updateRelChecked() {
    const n = document.querySelectorAll('#relBands .rel-check:checked').length;
    $('relCheckedCount').textContent = n ? `${n} selected` : '';
  }

  function renderRelCandidates() {
    const bands = { confirmed: [], suggested: [], attention: [] };
    RELCANDIDATES.forEach((c) => (bands[c.band] || bands.attention).push(c));
    // The nav badge counts CONFIRMED relations (set in loadAll), not candidates.
    // #relEmpty (never-scanned state) yields to the explanatory #relNoNew line.
    $('relEmpty').classList.toggle('hidden',
      !!RELCANDIDATES.length || !$('relNoNew').classList.contains('hidden'));
    $('relBands').classList.toggle('hidden', !RELCANDIDATES.length);
    [['relBandConfirmed', 'confirmed'], ['relBandSuggested', 'suggested'],
     ['relBandAttention', 'attention']].forEach(([boxId, band]) => {
      const box = $(boxId);
      box.innerHTML = bands[band].length
        ? bands[band].map(relCandRow).join('')
        : '<div class="adm-muted adm-rel-band-empty">None.</div>';
    });
    document.querySelectorAll('#relBands .adm-rel-cand').forEach((row) => {
      const cand = RELCANDIDATES.find((c) => c.candidate_id === row.dataset.cid);
      row.querySelectorAll('button').forEach((b) => {
        b.addEventListener('click', () => relCandAction(b.dataset.act, cand, row));
      });
      row.querySelector('.rel-check').addEventListener('change', updateRelChecked);
    });
    updateRelChecked();
  }

  async function scanRelations() {
    _busy('Scanning registered tables for relations… reading foreign keys from your databases.');
    let r;
    try {
      r = await api('/api/admin/relations/scan', { method: 'POST', body: '{}' });
    } finally { _busyDone(); }
    if (!r.data.ok) { toast(r.data.error || 'Scan failed', true); return; }
    mergeRelCandidates(r.data.candidates || [], { replaceNonSql: true });
    const deg = r.data.degraded || [];
    const degBox = $('relDegraded');
    degBox.classList.toggle('hidden', !deg.length);
    degBox.innerHTML = deg.length
      ? '<strong>Some sources were unavailable:</strong><br>' + deg.map((d) =>
          `${esc(d.connection)}${d.table ? ' · ' + esc(d.table) : ''} — ${esc(d.error)}`).join('<br>')
      : '';
    // Zero-new must be judged from the RESPONSE (the merged list can retain
    // earlier SQL-derived candidates) and explained via the confirmed count.
    _renderNoNew((r.data.candidates || []).length, r.data.confirmed_count || 0);
    _renderRelWarnings(_warnLinesFromReplay(r.data.evidence_warnings));
    loadRelRecommendations();     // scan persists fk-sourced recommendations
    renderRelCandidates();
    toast(`Scan complete — ${(r.data.candidates || []).length} candidate(s)`);
  }

  function _renderNoNew(freshCount, confirmedCount) {
    const box = $('relNoNew');
    if (freshCount) {
      box.classList.add('hidden');
      box.textContent = '';
      return;
    }
    box.textContent = confirmedCount
      ? `No new candidates found — ${confirmedCount} relation(s) already confirmed (listed above); confirmed relations are excluded from scans.`
      : 'No candidates found.';
    box.classList.remove('hidden');
  }

  async function analyzeRelSql() {
    const sql = $('relSqlInput').value;
    if (!sql.trim()) { toast('Paste one or more SELECT statements first', true); return; }
    _busy('Analyzing SQL…');
    let r;
    try {
      r = await api('/api/admin/relations/analyze_sql', {
        method: 'POST', body: JSON.stringify({ sql, db_type: $('relSqlDialect').value }) });
    } finally { _busyDone(); }
    if (!r.data.ok) { toast(r.data.error || 'SQL analysis failed', true); return; }
    mergeRelCandidates(r.data.candidates || []);
    const st = r.data.stats || {};
    const unknown = st.unknown_tables || [];
    // Refresh the persistent recommendations FIRST — the unknown list only
    // keeps names that did not become a recommendation row.
    await loadRelRecommendations();
    renderRelUnknowns(unknown, r.data.unknown_table_hints || []);
    _renderRelWarnings(_warnLinesFromAnalyze(st.invalid_column_refs));
    if (!(r.data.candidates || []).length && unknown.length) {
      $('relNoNew').classList.add('hidden');
    } else {
      _renderNoNew((r.data.candidates || []).length, r.data.confirmed_count || 0);
    }
    renderRelCandidates();
    const failNote = st.failed ? ` (${st.failed} statement(s) could not be parsed)` : '';
    toast(`Analyzed ${st.statements || 0} statement(s) — ${(r.data.candidates || []).length} candidate(s)${failNote}`, !!st.failed);
  }

  async function acceptRelCandidates(cands) {
    if (!cands.length) { toast('Nothing selected', true); return; }
    const body = { relations: cands.map((c) => ({
      table_id: c.table_id,
      related_table_id: c.related_table_id,
      join_keys: c.join_keys,
      cardinality: c.cardinality || null,
      origin: (c.sources || [])[0] || null,
    })) };
    _busy('Saving relations…');
    let r;
    try {
      r = await api('/api/admin/relations/accept', { method: 'POST', body: JSON.stringify(body) });
    } finally { _busyDone(); }
    if (!r.data.ok) { toast(r.data.error || 'Accept failed', true); return; }
    const ids = new Set(cands.map((c) => c.candidate_id));
    RELCANDIDATES = RELCANDIDATES.filter((c) => !ids.has(c.candidate_id));
    renderRelCandidates();
    const skipNote = r.data.skipped ? ` (${r.data.skipped} already declared)` : '';
    toast(`Accepted ${r.data.accepted} relation(s)${skipNote}`);
    loadAll();
  }

  function relCandAction(act, cand, row) {
    if (!cand) return;
    if (act === 'accept') {
      acceptRelCandidates([cand]);
    } else if (act === 'dismiss') {
      dismissRelCandidate(cand);
    } else if (act === 'edit') {
      editRelCandidate(cand, row);
    }
  }

  async function dismissRelCandidate(cand) {
    if (!window.confirm('Dismiss this candidate? (It may reappear on the next scan.)')) return;
    await api('/api/admin/relations/dismiss', {
      method: 'POST',
      body: JSON.stringify({ table_id: cand.table_id, related_table_id: cand.related_table_id,
        join_keys: cand.join_keys, band: cand.band }),
    });
    REL_DISMISSED.add(cand.candidate_id);
    RELCANDIDATES = RELCANDIDATES.filter((c) => c.candidate_id !== cand.candidate_id);
    renderRelCandidates();
  }

  // Inline editor: the SAME controls as the manual relations editor (target
  // table select + "a=b, c=d" keys text), saved through the lightweight
  // accept endpoint — deliberately NOT the full table wizard, whose save
  // path re-introspects, re-confirms, and re-snapshots.
  function editRelCandidate(cand, row) {
    const options = TABLES
      .filter((t) => t.id !== cand.table_id)
      .map((t) => `<option value="${esc(t.id)}" ${cand.related_table_id === t.id ? 'selected' : ''}>${esc(t.display_name)}</option>`)
      .join('');
    const main = row.querySelector('.adm-rel-cand-main');
    main.innerHTML = `
      <div class="adm-rel-row-head">
        <span><strong>${esc(cand.table_label)}</strong> joins</span>
        <select class="tw-rel-target"><option value="">— pick table —</option>${options}</select>
        <span>on</span>
      </div>`;
    const actions = row.querySelector('.adm-rel-cand-actions');
    actions.innerHTML = `
      <button class="adm-btn primary small" data-act="save">Save</button>
      <button class="adm-btn ghost small" data-act="cancel">Cancel</button>`;
    const saveBtn = actions.querySelector('[data-act="save"]');
    const editor = createPairEditor({
      leftCols: tableCols(cand.table_id),
      rightCols: tableCols(cand.related_table_id),
      pairs: cand.join_keys || [],
      onChange: () => { saveBtn.disabled = editor.hasMissing(); },
    });
    main.appendChild(editor.el);
    saveBtn.disabled = editor.hasMissing();
    const targetSel = main.querySelector('.tw-rel-target');
    targetSel.addEventListener('change', () => editor.setRightCols(tableCols(targetSel.value)));
    saveBtn.addEventListener('click', async () => {
      const target = targetSel.value;
      const jk = editor.getPairs();
      if (!target || !jk.length) { toast('Pick a table and at least one column pair', true); return; }
      const unchanged = target === cand.related_table_id
        && JSON.stringify(jk) === JSON.stringify(cand.join_keys || []);
      await acceptRelCandidates([Object.assign({}, cand, {
        related_table_id: target,
        join_keys: jk,
        // measured cardinality only holds for the measured target+keys
        cardinality: unchanged ? cand.cardinality : null,
      })]);
    });
    actions.querySelector('[data-act="cancel"]').addEventListener('click', renderRelCandidates);
  }

  function acceptCheckedRelCandidates() {
    const ids = Array.from(document.querySelectorAll('#relBands .rel-check:checked'))
      .map((cb) => cb.closest('.adm-rel-cand').dataset.cid);
    const cands = RELCANDIDATES.filter((c) => ids.includes(c.candidate_id));
    acceptRelCandidates(cands);
  }

  async function draftWithAI() {
    if (!currentIntro) return;
    $('btnDraftAI').disabled = true;
    $('btnDraftAI').textContent = '✨ Drafting…';
    const r = await api('/api/admin/tables/draft_descriptions', {
      method: 'POST',
      body: JSON.stringify({ connection_id: wizardConnId,
        schema: $('twSchema').value, table: $('twTable').value }),
    });
    $('btnDraftAI').disabled = false;
    $('btnDraftAI').textContent = '✨ Draft descriptions with AI';
    if (!r.data.ok) { toast(r.data.error || 'AI drafting failed', true); return; }
    const draft = r.data.draft || {};
    if (draft.table_description && !$('twDescription').value.trim()) {
      $('twDescription').value = draft.table_description;
    }
    document.querySelectorAll('#twColsBody tr').forEach((tr) => {
      const d = (draft.columns || {})[tr.dataset.name];
      const input = tr.querySelector('.tw-col-desc');
      if (d && !input.value.trim()) input.value = d;
    });
    // A fresh draft ALWAYS clears the confirmation — the admin must re-review.
    $('twConfirm').checked = false;
    $('twDraftBanner').classList.remove('hidden');
    updateSaveEnabled();
  }

  function updateSaveEnabled() {
    $('btnSaveTable').disabled = !$('twConfirm').checked;
  }

  async function saveTable() {
    if (!currentIntro) return;
    if (_wizSuggestLoading) {
      // Pre-checked suggestion rows would silently NOT be committed if the
      // save raced the (sub-second) suggestions load.
      toast('Relation suggestions are still loading — one moment…', true);
      return;
    }
    const columns = [];
    document.querySelectorAll('#twColsBody tr').forEach((tr) => {
      columns.push({
        name: tr.dataset.name,
        dtype: tr.dataset.dtype,
        pk: !!tr.dataset.pk,
        indexed: tr.querySelector('.tw-col-indexed').checked,
        description: tr.querySelector('.tw-col-desc').value.trim(),
      });
    });
    // Manual rows + checked suggestions, deduped by target + pairs (both are
    // child-first, so a direct compare suffices).
    const rels = collectRelations();
    const relKey = (r) => r.related_table_id + '|' + JSON.stringify(r.join_keys);
    const seenRels = new Set(rels.map(relKey));
    collectCheckedSuggestions().forEach((s) => {
      if (!seenRels.has(relKey(s))) { rels.push(s); seenRels.add(relKey(s)); }
    });
    const body = {
      connection_id: wizardConnId,
      schema: $('twSchema').value,
      table_name: $('twTable').value,
      display_name: $('twDisplayName').value.trim(),
      description: $('twDescription').value.trim(),
      columns,
      is_connector: $('twIsConnector').checked,
      relations: rels,
      confirm: $('twConfirm').checked === true,
    };
    // Access panel → role records. Omitted (not []) when roles were
    // unavailable, so an edit-save can never strip existing grants.
    if (_wizAccessReady) body.access_role_ids = collectCheckedAccessRoles();
    $('btnSaveTable').disabled = true;
    $('btnSaveTable').textContent = 'Snapshotting…';
    _busy(`Saving "${body.display_name}" and taking a snapshot… large tables can take a while.`);
    const path = editingTableId ? `/api/admin/tables/${editingTableId}` : '/api/admin/tables';
    let r;
    try {
      r = await api(path, { method: 'POST', body: JSON.stringify(body) });
    } finally { _busyDone(); }
    $('btnSaveTable').textContent = '💾 Save & snapshot';
    updateSaveEnabled();
    if (r.status === 409) {
      toast(r.data.error || 'Table structure changed — go back and reload the structure.', true);
      return;
    }
    if (!(r.status === 200 || r.status === 201)) {
      toast(r.data.error || 'Save failed', true);
      return;
    }
    const snap = r.data.snapshot || {};
    if (snap.ok) {
      toast(`Saved — snapshot ${Number(snap.rows).toLocaleString()} rows`);
    } else {
      toast(`Saved, but snapshot failed: ${snap.error || 'unknown'} — use Refresh to retry`, true);
    }
    $('tableModal').classList.add('hidden');
    loadAll();
  }

  // ── Schedule ───────────────────────────────────────────────────────────
  // One shared field component serves the global card and the per-table
  // override modal. Presets normalize server-side to 5-field cron; the
  // /schedule_preview endpoint provides the live "Next run" echo.
  const WEEKDAYS = [['1', 'Mon'], ['2', 'Tue'], ['3', 'Wed'], ['4', 'Thu'],
    ['5', 'Fri'], ['6', 'Sat'], ['0', 'Sun']];  // Mon-first, cron values

  function renderScheduleFields(container, sched) {
    const s = sched || { mode: 'daily', time: '00:00' };
    const dayChips = [];
    for (let d = 1; d <= 28; d += 1) {
      const on = (s.monthly_days || []).includes(d);
      dayChips.push(`<button type="button" class="adm-chip sched-day${on ? ' ok' : ''}" data-day="${d}">${d}</button>`);
    }
    const lastOn = (s.monthly_days || []).includes('last');
    container.innerHTML = `
      <label class="adm-time-row"><span>Repeat</span>
        <select class="schedMode">
          <option value="daily">Daily</option>
          <option value="weekly">Weekly</option>
          <option value="monthly">Monthly</option>
          <option value="interval">Every N minutes / hours</option>
          <option value="cron">Custom cron</option>
        </select>
      </label>
      <label class="adm-time-row schedTimeRow"><span>Run at (container-local time)</span>
        <input type="time" class="schedTime" value="${esc(s.time || '00:00')}" />
      </label>
      <div class="schedWeekdaysRow">
        ${WEEKDAYS.map(([v, n]) => `<label class="sched-dow"><input type="checkbox"
          value="${v}" ${(s.weekdays || []).includes(Number(v)) ? 'checked' : ''} /> ${n}</label>`).join(' ')}
      </div>
      <div class="schedMonthDaysRow">
        <div class="sched-days">${dayChips.join('')}
          <button type="button" class="adm-chip sched-day sched-day-last${lastOn ? ' ok' : ''}" data-day="last">Last day</button></div>
        <div class="adm-muted">Days 29–31 are deliberately unavailable — they'd skip
          shorter months. Pick 28 or Last day.</div>
      </div>
      <label class="adm-time-row schedIntervalRow"><span>Every</span>
        <input type="number" class="schedIntervalN" min="1" step="1"
               value="${s.every_minutes && s.every_minutes % 60 === 0 ? s.every_minutes / 60 : (s.every_minutes || 1)}" />
        <select class="schedIntervalUnit">
          <option value="hours">hours</option>
          <option value="minutes">minutes</option>
        </select>
        <span class="adm-muted">Minimum 15 minutes.</span>
      </label>
      <label class="adm-time-row schedCronRow"><span>Cron expression</span>
        <input type="text" class="schedCron" placeholder="*/30 8-18 * * 1-5"
               value="${esc(s.cron || '')}" />
      </label>
      <div class="adm-muted schedCronHelp">Standard 5-field cron
        (minute hour day month weekday), container-local time. Validated on save.</div>`;
    const modeSel = container.querySelector('.schedMode');
    modeSel.value = s.mode || 'daily';
    if (s.mode === 'interval' && s.every_minutes && s.every_minutes % 60 !== 0) {
      container.querySelector('.schedIntervalUnit').value = 'minutes';
    }
    const applyMode = () => {
      const m = modeSel.value;
      container.querySelector('.schedTimeRow').classList.toggle('hidden', !['daily', 'weekly', 'monthly'].includes(m));
      container.querySelector('.schedWeekdaysRow').classList.toggle('hidden', m !== 'weekly');
      container.querySelector('.schedMonthDaysRow').classList.toggle('hidden', m !== 'monthly');
      container.querySelector('.schedIntervalRow').classList.toggle('hidden', m !== 'interval');
      container.querySelector('.schedCronRow').classList.toggle('hidden', m !== 'cron');
      container.querySelector('.schedCronHelp').classList.toggle('hidden', m !== 'cron');
    };
    modeSel.addEventListener('change', () => { applyMode(); container.dispatchEvent(new Event('schedchange')); });
    applyMode();
    container.querySelectorAll('.sched-day').forEach((b) => {
      b.addEventListener('click', () => { b.classList.toggle('ok'); container.dispatchEvent(new Event('schedchange')); });
    });
    container.querySelectorAll('input, select').forEach((el) => {
      el.addEventListener('change', () => container.dispatchEvent(new Event('schedchange')));
    });
  }

  function collectScheduleFields(container, enabled) {
    const mode = container.querySelector('.schedMode').value;
    const out = { mode, enabled: !!enabled };
    if (['daily', 'weekly', 'monthly'].includes(mode)) {
      out.time = container.querySelector('.schedTime').value || '00:00';
    }
    if (mode === 'weekly') {
      out.weekdays = Array.from(container.querySelectorAll('.schedWeekdaysRow input:checked'))
        .map((el) => Number(el.value));
    }
    if (mode === 'monthly') {
      out.monthly_days = Array.from(container.querySelectorAll('.sched-day.ok'))
        .map((b) => (b.dataset.day === 'last' ? 'last' : Number(b.dataset.day)));
    }
    if (mode === 'interval') {
      const n = Number(container.querySelector('.schedIntervalN').value) || 0;
      const unit = container.querySelector('.schedIntervalUnit').value;
      out.every_minutes = unit === 'hours' ? n * 60 : n;
    }
    if (mode === 'cron') out.cron = container.querySelector('.schedCron').value.trim();
    return out;
  }

  function _lastRunLine(s) {
    const bits = [];
    if (s.next_run_at) bits.push(`Next run: ${s.next_run_at}`);
    const sum = s.last_run_summary;
    if (s.last_run_at && sum) {
      const skipped = sum.skipped_count != null ? `, ${sum.skipped_count} unchanged` : '';
      bits.push(`Last run ${s.last_run_at} — ${sum.ok_count ?? '?'} ok, ${sum.failed_count ?? '?'} failed${skipped}`);
    } else if (s.last_run_at) {
      bits.push(`Last run: ${s.last_run_at}`);
    }
    return bits.join(' · ');
  }

  let _previewTimer = null;
  function schedulePreviewInto(container, previewEl, enabled) {
    clearTimeout(_previewTimer);
    _previewTimer = setTimeout(async () => {
      const sched = collectScheduleFields(container, enabled);
      const r = await api('/api/admin/schedule_preview', {
        method: 'POST', body: JSON.stringify({ schedule: sched }) });
      if (r.ok) {
        previewEl.textContent = `${r.data.description} · Next runs: ${
          (r.data.next_runs || []).join(', ') || '—'}`;
        previewEl.classList.remove('adm-bad-text');
      } else {
        previewEl.textContent = r.data.error || 'Invalid schedule';
        previewEl.classList.add('adm-bad-text');
      }
    }, 350);
  }

  function renderSchedule(s) {
    $('refreshEnabled').checked = !!(s.schedule ? s.schedule.enabled : s.refresh_enabled);
    renderScheduleFields($('globalScheduleFields'), s.schedule || {
      mode: 'daily', time: s.refresh_time || '00:00' });
    const line = [s.description, _lastRunLine(s)].filter(Boolean).join(' · ');
    $('schedPreview').textContent = line;
    $('schedPreview').classList.remove('adm-bad-text');
    const gf = $('globalScheduleFields');
    if (!gf.dataset.wired) {
      gf.dataset.wired = '1';
      gf.addEventListener('schedchange', () => {
        schedulePreviewInto(gf, $('schedPreview'), $('refreshEnabled').checked);
      });
    }
  }

  async function saveSchedule() {
    const sched = collectScheduleFields($('globalScheduleFields'),
      $('refreshEnabled').checked);
    const r = await api('/api/admin/refresh_settings', {
      method: 'POST', body: JSON.stringify({ schedule: sched }) });
    if (r.ok) { toast('Schedule saved'); renderSchedule(r.data); }
    else toast(r.data.error || 'Save failed', true);
  }

  // ── Per-table schedule override modal ──────────────────────────────────
  let _tsmTid = null;
  function openTableScheduleModal(t) {
    _tsmTid = t.id;
    $('tsmTitle').textContent = `Schedule — ${t.display_name}`;
    const own = t.schedule && typeof t.schedule === 'object';
    $('tsmInherit').checked = !own;
    $('tsmOwn').checked = !!own;
    renderScheduleFields($('tsmFields'), own ? t.schedule : { mode: 'daily', time: '00:00' });
    $('tsmFields').classList.toggle('hidden', !own);
    $('tsmPreview').textContent = own ? '' : 'This table follows the global schedule.';
    $('tsmPreview').classList.remove('adm-bad-text');
    const tf = $('tsmFields');
    if (!tf.dataset.wired) {
      tf.dataset.wired = '1';
      tf.addEventListener('schedchange', () => {
        if ($('tsmOwn').checked) schedulePreviewInto(tf, $('tsmPreview'), true);
      });
    }
    $('tableScheduleModal').classList.remove('hidden');
  }

  async function saveTableSchedule() {
    const body = $('tsmOwn').checked
      ? { schedule: collectScheduleFields($('tsmFields'), true) }
      : { schedule: null };
    const r = await api(`/api/admin/tables/${_tsmTid}/schedule`, {
      method: 'POST', body: JSON.stringify(body) });
    if (r.ok) {
      toast(body.schedule ? `Table schedule saved (${r.data.description || 'own schedule'})`
        : 'Table now inherits the global schedule');
      $('tableScheduleModal').classList.add('hidden');
      loadAll();
    } else {
      $('tsmPreview').textContent = r.data.error || 'Save failed';
      $('tsmPreview').classList.add('adm-bad-text');
    }
  }

  // ── Audit ──────────────────────────────────────────────────────────────
  async function loadAudit() {
    const box = $('auditList');
    box.innerHTML = '<div class="adm-muted" style="padding:14px">Loading…</div>';
    const r = await api('/api/admin/audit?limit=200');
    const rows = (r.data.rows || []).slice().reverse();
    if (!rows.length) {
      box.innerHTML = '<div class="adm-muted" style="padding:14px">No audit entries yet.</div>';
      return;
    }
    box.innerHTML = `<div class="adm-table-scroll"><table class="adm-table adm-audit-table">
      <thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Target</th><th></th></tr></thead>
      <tbody>${rows.map((row) => `
        <tr class="${row.ok === false ? 'adm-row-bad' : ''}">
          <td class="adm-cell-mono">${esc(row.ts)}</td>
          <td>${esc(row.actor)}</td>
          <td>${esc(row.action)}</td>
          <td class="adm-cell-mono">${esc(row.target || '')}</td>
          <td>${row.ok === false ? '<span class="adm-chip bad">failed</span>' : ''}</td>
        </tr>`).join('')}</tbody></table></div>`;
  }

  // ── Users (role assignment) ────────────────────────────────────────────
  let _userSearchTyped = false;   // set by the input listener — a value the
                                  // user never typed is browser autofill

  async function loadUsers() {
    // Chrome used to autofill the saved login email into the filter box and
    // silently shrink the list to one user — clear any non-user-typed value.
    const search = $('userSearch');
    if (search && search.value && !_userSearchTyped) search.value = '';
    const r = await api('/api/admin/users');
    if (r.ok) USERS = r.data.users || [];
    $('navUserCount').textContent = USERS.length || '';
    renderUsers();
  }

  // 19c multi-role model: the role cell is a button + checkbox panel. A user
  // holds SEVERAL roles (read access = union); checked = held, and every
  // toggle POSTs the full id list immediately (same instant semantics the
  // old single dropdown had). The PERMISSION level (19e: Standard / Power
  // user / Local admin) is a per-row dropdown POSTing set_permission the
  // same way; the picker stays enabled on admin rows too (19g: promoted
  // admins hold roles like anyone — only the unlisted bootstrap account is
  // roleless).
  let _userRolesDocClose = false;   // one document-level close listener

  function _userRolesLabel(u) {
    const names = (u.role_names && u.role_names.length)
      ? u.role_names : [u.role_name || 'Base'];
    return names.join(' + ');
  }

  function closeUserRolePanels() {
    document.querySelectorAll('.adm-user-roles-panel:not(.hidden)')
      .forEach((p) => p.classList.add('hidden'));
  }

  function renderUsers() {
    const box = $('usersList');
    const q = ($('userSearch').value || '').trim().toLowerCase();
    const rows = q ? USERS.filter((u) => (u.email || '').toLowerCase().includes(q)) : USERS;
    if (!rows.length) {
      box.innerHTML = `<div class="adm-card"><div class="adm-empty">
        ${q ? 'No users match the search.' : 'No users yet — users appear after their first sign-in.'}
      </div></div>`;
      return;
    }
    const fmt = (ts) => ts ? esc(String(ts).replace('T', ' ').slice(0, 16)) : '—';
    const panel = (u) => {
      const held = new Set(u.role_ids || [u.role_id]);
      return ROLES.map((ro) => `
        <label class="adm-check adm-user-role-opt">
          <input type="checkbox" class="adm-user-role-check"
            data-rid="${esc(ro.id)}" ${held.has(ro.id) ? 'checked' : ''}>
          <span>${esc(ro.name)}${ro.id === 'base'
            ? ' <span class="adm-label-note">(default)</span>' : ''}</span>
        </label>`).join('');
    };
    const PERMS = [['standard', 'Standard'], ['power', 'Power user'],
                   ['admin', 'Local admin']];
    const permSel = (u) => `
      <select class="adm-user-perm">
        ${PERMS.map(([v, l]) => `<option value="${v}"
          ${(u.permission || 'standard') === v ? 'selected' : ''}>${l}</option>`).join('')}
      </select>`;
    box.innerHTML = `<div class="adm-card adm-table-scroll"><table class="adm-table">
      <thead><tr><th>Email</th><th>Roles</th><th>Permission</th><th>First login</th><th>Last login</th></tr></thead>
      <tbody>${rows.map((u) => `
        <tr data-email="${esc(u.email)}">
          <td>${esc(u.email)}</td>
          <td><div class="adm-user-roles-wrap">
            <button type="button" class="adm-btn ghost small adm-user-roles-btn">
              ${esc(_userRolesLabel(u))} ▾</button>
            <div class="adm-user-roles-panel adm-card hidden">${panel(u)}</div>
          </div></td>
          <td>${permSel(u)}</td>
          <td class="adm-cell-mono">${fmt(u.created_at)}</td>
          <td class="adm-cell-mono">${fmt(u.last_login_at)}</td>
        </tr>`).join('')}</tbody></table></div>`;

    box.querySelectorAll('.adm-user-roles-btn').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const p = btn.parentElement.querySelector('.adm-user-roles-panel');
        const wasHidden = p.classList.contains('hidden');
        closeUserRolePanels();          // one open panel at a time
        p.classList.toggle('hidden', !wasHidden);
      });
    });
    box.querySelectorAll('.adm-user-roles-panel').forEach((p) => {
      p.addEventListener('click', (e) => e.stopPropagation());
    });
    box.querySelectorAll('.adm-user-role-check').forEach((cb) => {
      cb.addEventListener('change', async () => {
        const tr = cb.closest('tr');
        const email = tr.dataset.email;
        const p = cb.closest('.adm-user-roles-panel');
        const role_ids = Array.from(p.querySelectorAll('.adm-user-role-check'))
          .filter((x) => x.checked).map((x) => x.dataset.rid);
        const r = await api('/api/admin/users/set_role', {
          method: 'POST', body: JSON.stringify({ email, role_ids }),
        });
        const u = USERS.find((x) => x.email === email);
        if (r.ok && r.data.user && u) {
          Object.assign(u, r.data.user);
          toast(`Roles updated for ${email}`);
          // Reconcile with the RESOLVED server answer (e.g. everything
          // unchecked ⇒ Base is effective) without closing the panel.
          const held = new Set(u.role_ids || []);
          p.querySelectorAll('.adm-user-role-check').forEach((x) => {
            x.checked = held.has(x.dataset.rid);
          });
          tr.querySelector('.adm-user-roles-btn').textContent =
            `${_userRolesLabel(u)} ▾`;
        } else {
          toast(r.data.error || 'Could not change the roles', true);
          loadUsers();                  // refetch = revert
        }
      });
    });
    box.querySelectorAll('.adm-user-perm').forEach((sel) => {
      sel.addEventListener('change', async () => {
        const email = sel.closest('tr').dataset.email;
        const u = USERS.find((x) => x.email === email);
        const prev = (u && u.permission) || 'standard';
        const permission = sel.value;
        if (permission === 'admin'
            && !window.confirm('This user gets full Data-sources '
              + 'administration. They keep their chats and roles.')) {
          sel.value = prev;             // cancelled — revert the dropdown
          return;
        }
        const r = await api('/api/admin/users/set_permission', {
          method: 'POST', body: JSON.stringify({ email, permission }),
        });
        if (r.ok && r.data.user && u) {
          Object.assign(u, r.data.user);
          toast(`Permission updated for ${email}`);
          renderUsers();                // re-render row (roles picker en/disable)
        } else {
          toast(r.data.error || 'Could not change the permission', true);
          loadUsers();                  // refetch = revert
        }
      });
    });
    if (!_userRolesDocClose) {
      _userRolesDocClose = true;
      document.addEventListener('click', closeUserRolePanels);
    }
  }

  // ── Roles (grants registry) ────────────────────────────────────────────
  function roleGrantSummary(ro) {
    const bits = [];
    const conns = (ro.scope_grants || []).filter((g) => g.schema == null).length;
    const schemas = (ro.scope_grants || []).length - conns;
    if (conns) bits.push(`${conns} connection grant${conns > 1 ? 's' : ''}`);
    if (schemas) bits.push(`${schemas} schema grant${schemas > 1 ? 's' : ''}`);
    if ((ro.table_ids || []).length) bits.push(`${ro.table_ids.length} table${ro.table_ids.length > 1 ? 's' : ''}`);
    const manage = (ro.manage_grants || []).length;
    if (manage) bits.push(`${manage} manage grant${manage > 1 ? 's' : ''}`);
    return bits.join(' · ') || 'No table access';
  }

  function renderRoles() {
    const box = $('rolesList');
    if (!box) return;
    if (!ROLES.length) {
      box.innerHTML = '<div class="adm-card"><div class="adm-empty">Roles unavailable.</div></div>';
      return;
    }
    box.innerHTML = ROLES.map((ro) => `
      <div class="adm-card adm-role-card" data-rid="${esc(ro.id)}">
        <div class="adm-role-main">
          <div class="adm-role-title">${esc(ro.name)}
            ${ro.is_builtin ? '<span class="adm-chip">built-in</span>' : ''}
            <span class="adm-chip">${ro.member_count || 0} member${ro.member_count === 1 ? '' : 's'}</span>
          </div>
          ${ro.description ? `<div class="adm-muted">${esc(ro.description)}</div>` : ''}
          <div class="adm-role-grants">${esc(roleGrantSummary(ro))}</div>
        </div>
        <div class="adm-role-actions">
          <button class="adm-icon-btn" data-act="edit" title="Edit role">✏️</button>
          ${ro.is_builtin ? '' : '<button class="adm-icon-btn" data-act="delete" title="Delete role">🗑️</button>'}
        </div>
      </div>`).join('');
    box.querySelectorAll('button').forEach((b) => {
      b.addEventListener('click', () => {
        const ro = ROLES.find((x) => x.id === b.closest('.adm-role-card').dataset.rid);
        if (!ro) return;
        if (b.dataset.act === 'edit') openRoleModal(ro);
        else if (b.dataset.act === 'delete') openRoleDelete(ro);
      });
    });
  }

  // Role editor — tri-state tree connection → schema → tables, built from the
  // already-loaded CONNECTIONS + TABLES (non-connector). Working state lives
  // in `roleDraft` Sets; every toggle re-renders the (small) tree from it.
  const _schemaKey = (cid, schema) => cid + '|' + String(schema || '').toLowerCase();

  function openRoleModal(role) {
    editingRoleId = role ? role.id : null;
    roleDraft = {
      isBase: !!(role && role.is_base),
      isBuiltin: !!(role && role.is_builtin),
      tableIds: new Set(role ? role.table_ids || [] : []),
      connGrants: new Set((role ? role.scope_grants || [] : [])
        .filter((g) => g.schema == null).map((g) => g.connection_id)),
      schemaGrants: new Map((role ? role.scope_grants || [] : [])
        .filter((g) => g.schema != null)
        .map((g) => [_schemaKey(g.connection_id, g.schema),
                     { connection_id: g.connection_id, schema: g.schema }])),
      // 19f: the SEPARATE management axis (where power-permission members
      // may register tables/relations/schedules).
      manageConn: new Set((role ? role.manage_grants || [] : [])
        .filter((g) => g.schema == null).map((g) => g.connection_id)),
      manageSchema: new Map((role ? role.manage_grants || [] : [])
        .filter((g) => g.schema != null)
        .map((g) => [_schemaKey(g.connection_id, g.schema),
                     { connection_id: g.connection_id, schema: g.schema }])),
    };
    $('roleModalTitle').textContent = role ? `Edit role — ${role.name}` : 'Add role';
    $('roleName').value = role ? role.name : '';
    const builtin = !!(role && role.is_builtin);
    $('roleName').disabled = builtin;   // the built-in Base role is unrenamable
    $('roleName').title = builtin ? 'Built-in roles cannot be renamed' : '';
    $('roleDesc').value = role ? role.description || '' : '';
    $('roleError').classList.add('hidden');
    renderRoleTree();
    $('roleModal').classList.remove('hidden');
    if (!roleDraft.isBase) $('roleName').focus();
  }

  function renderRoleTree() {
    const box = $('roleAccessTree');
    const tables = TABLES.filter((t) => !t.is_connector);
    if (!CONNECTIONS.length || !tables.length) {
      box.innerHTML = '<div class="adm-empty">No registered tables yet — register tables first, '
        + 'or grant a whole connection once one exists.</div>';
      if (!CONNECTIONS.length) return;
    }
    // 19f two-column rows: left = the Chat-access checkbox + name (tri-state
    // as before), right = the Manage checkbox (connection/schema rows only).
    const manageCell = (inner) => `<span class="adm-tree-manage">${inner}</span>`;
    const html = CONNECTIONS.map((c) => {
      const connGranted = roleDraft.connGrants.has(c.id);
      const connManaged = roleDraft.manageConn.has(c.id);
      const connTables = tables.filter((t) => t.connection_id === c.id);
      // Group by lower-cased schema, keep the first raw name as the label.
      const schemas = new Map();
      connTables.forEach((t) => {
        const key = String(t.schema || '').toLowerCase();
        if (!schemas.has(key)) schemas.set(key, { raw: t.schema || '', tables: [] });
        schemas.get(key).tables.push(t);
      });
      const schemaHtml = Array.from(schemas.values()).map((s) => {
        const sKey = _schemaKey(c.id, s.raw);
        const schemaGranted = roleDraft.schemaGrants.has(sKey);
        const schemaManaged = roleDraft.manageSchema.has(sKey);
        const leaves = s.tables.map((t) => {
          const covered = connGranted || schemaGranted;
          const checked = covered || roleDraft.tableIds.has(t.id);
          return `<div class="adm-tree-row">
            <label class="adm-tree-leaf adm-check">
            <input type="checkbox" class="rt-table" data-tid="${esc(t.id)}"
              ${checked ? 'checked' : ''} ${covered ? 'disabled' : ''}>
            <span>${esc(t.display_name || t.table_name)}
              ${covered ? `<span class="adm-label-note">${connGranted ? 'via connection' : 'via schema'}</span>` : ''}
            </span>
            </label>${manageCell('')}
          </div>`;
        }).join('');
        return `<div class="adm-tree-schema-block">
          <div class="adm-tree-row">
          <label class="adm-tree-schema adm-check">
            <input type="checkbox" class="rt-schema" data-cid="${esc(c.id)}"
              data-schema="${esc(s.raw)}" ${connGranted || schemaGranted ? 'checked' : ''}
              ${connGranted ? 'disabled' : ''}>
            <span>${esc(s.raw || '(no schema)')}
              <span class="adm-label-note">${connGranted ? 'via connection'
                : 'schema grant — includes tables registered later'}</span>
            </span>
          </label>${manageCell(`<input type="checkbox" class="rt-schema-m"
            data-cid="${esc(c.id)}" data-schema="${esc(s.raw)}"
            title="Power members may register tables in this schema"
            ${connManaged || schemaManaged ? 'checked' : ''}
            ${connManaged ? 'disabled' : ''}>`)}
          </div>
          <div class="adm-tree-leaves">${leaves}</div>
        </div>`;
      }).join('');
      return `<div class="adm-tree-conn-block">
        <div class="adm-tree-row">
        <label class="adm-tree-conn adm-check">
          <input type="checkbox" class="rt-conn" data-cid="${esc(c.id)}"
            ${connGranted ? 'checked' : ''}>
          <span><strong>${esc(c.name)}</strong>
            <span class="adm-label-note">whole connection — includes tables registered later</span>
          </span>
        </label>${manageCell(`<input type="checkbox" class="rt-conn-m"
          data-cid="${esc(c.id)}"
          title="Power members may register tables anywhere on this connection"
          ${connManaged ? 'checked' : ''}>`)}
        </div>
        <div class="adm-tree-schemas">${schemaHtml
          || '<div class="adm-muted adm-tree-empty">No registered tables on this connection yet.</div>'}</div>
      </div>`;
    }).join('');
    const scroll = box.scrollTop;
    box.innerHTML = html;
    box.scrollTop = scroll;
    // Indeterminate = some (not all) descendants checked and the node itself
    // not granted. Set after render (no HTML attribute exists for it).
    box.querySelectorAll('.adm-tree-conn-block').forEach((cb) => {
      const conn = cb.querySelector('.rt-conn');
      if (conn.checked) return;
      const boxes = Array.from(cb.querySelectorAll('.rt-schema, .rt-table'));
      const n = boxes.filter((x) => x.checked).length;
      conn.indeterminate = n > 0 && n < boxes.length;
    });
    box.querySelectorAll('.adm-tree-schema-block').forEach((sb) => {
      const sch = sb.querySelector('.rt-schema');
      if (sch.checked) return;
      const boxes = Array.from(sb.querySelectorAll('.rt-table'));
      const n = boxes.filter((x) => x.checked).length;
      sch.indeterminate = n > 0 && n < boxes.length;
    });
    box.querySelectorAll('.adm-tree-conn-block').forEach((cb) => {
      const connM = cb.querySelector('.rt-conn-m');
      if (!connM || connM.checked) return;
      const boxes = Array.from(cb.querySelectorAll('.rt-schema-m'));
      const n = boxes.filter((x) => x.checked).length;
      connM.indeterminate = n > 0 && n < boxes.length;
    });
    box.querySelectorAll('input[type="checkbox"]').forEach((el) => {
      el.addEventListener('change', () => {
        if (el.classList.contains('rt-conn')) {
          if (el.checked) roleDraft.connGrants.add(el.dataset.cid);
          else roleDraft.connGrants.delete(el.dataset.cid);
        } else if (el.classList.contains('rt-schema')) {
          const key = _schemaKey(el.dataset.cid, el.dataset.schema);
          if (el.checked) {
            roleDraft.schemaGrants.set(key,
              { connection_id: el.dataset.cid, schema: el.dataset.schema });
          } else roleDraft.schemaGrants.delete(key);
        } else if (el.classList.contains('rt-conn-m')) {
          if (el.checked) roleDraft.manageConn.add(el.dataset.cid);
          else roleDraft.manageConn.delete(el.dataset.cid);
        } else if (el.classList.contains('rt-schema-m')) {
          const key = _schemaKey(el.dataset.cid, el.dataset.schema);
          if (el.checked) {
            roleDraft.manageSchema.set(key,
              { connection_id: el.dataset.cid, schema: el.dataset.schema });
          } else roleDraft.manageSchema.delete(key);
        } else if (el.classList.contains('rt-table')) {
          if (el.checked) roleDraft.tableIds.add(el.dataset.tid);
          else roleDraft.tableIds.delete(el.dataset.tid);
        }
        renderRoleTree();
      });
    });
  }

  async function saveRole() {
    const err = $('roleError');
    err.classList.add('hidden');
    // Redundant grants/ids dropped at save: a schema grant under a granted
    // connection, and explicit table ids a grant already covers.
    const scope_grants = [
      ...Array.from(roleDraft.connGrants).map((cid) => ({ connection_id: cid, schema: null })),
      ...Array.from(roleDraft.schemaGrants.values())
        .filter((g) => !roleDraft.connGrants.has(g.connection_id)),
    ];
    const covered = (t) => roleDraft.connGrants.has(t.connection_id)
      || roleDraft.schemaGrants.has(_schemaKey(t.connection_id, t.schema || ''));
    const tablesById = new Map(TABLES.map((t) => [t.id, t]));
    const table_ids = Array.from(roleDraft.tableIds).filter((tid) => {
      const t = tablesById.get(tid);
      return t && !covered(t);
    });
    const manage_grants = [
      ...Array.from(roleDraft.manageConn).map((cid) => ({ connection_id: cid, schema: null })),
      ...Array.from(roleDraft.manageSchema.values())
        .filter((g) => !roleDraft.manageConn.has(g.connection_id)),
    ];
    const body = { description: $('roleDesc').value.trim(), table_ids,
                   scope_grants, manage_grants };
    // Omit `name` for built-ins — sending it made the rename guard 400
    // every built-in edit, so its description/grants could never be saved
    // from the UI (the 19c bug).
    if (!roleDraft.isBuiltin) body.name = $('roleName').value.trim();
    const path = editingRoleId ? `/api/admin/roles/${editingRoleId}` : '/api/admin/roles';
    const r = await api(path, { method: 'POST', body: JSON.stringify(body) });
    if (!(r.status === 200 || r.status === 201)) {
      err.textContent = r.data.error || 'Save failed';
      err.classList.remove('hidden');
      return;
    }
    toast('Role saved');
    $('roleModal').classList.add('hidden');
    await loadAll();
    if (!$('secUsers').hidden) loadUsers();   // role names feed the dropdown
  }

  function openRoleDelete(ro) {
    roleDeleteCtx = ro;
    const n = ro.member_count || 0;
    $('roleDeleteText').textContent =
      `Delete role "${ro.name}"? ${n} user${n === 1 ? '' : 's'} will revert to the Base role. `
      + 'Existing chats keep their saved data.';
    $('roleDeleteModal').classList.remove('hidden');
  }

  async function runRoleDelete() {
    if (!roleDeleteCtx) return;
    const r = await api(`/api/admin/roles/${roleDeleteCtx.id}/delete`, { method: 'POST' });
    $('roleDeleteModal').classList.add('hidden');
    roleDeleteCtx = null;
    if (r.ok) {
      toast(`Role deleted — ${r.data.reverted_members || 0} member(s) reverted to Base`);
      await loadAll();
      if (!$('secUsers').hidden) loadUsers();
    } else {
      toast(r.data.error || 'Delete failed', true);
    }
  }

  // ── Wizard step-3 Access panel (writes to ROLE records via the save's
  // access_role_ids — never to the table doc) ────────────────────────────
  let _wizAccessReady = false;   // roles rendered → saveTable may send the field

  function renderWizardAccess() {
    const box = $('twAccessRoles');
    _wizAccessReady = false;
    if (!box) return;
    // 19f: in power mode ROLES holds the caller's HELD roles (my_roles) —
    // "Share with your roles", all unchecked by default on a new
    // registration; the server enforces the held-subset rule too.
    if (!ROLES.length) {
      // Roles fetch failed at load — sending [] would strip existing grants
      // on an edit, so the save omits the field entirely.
      box.className = 'adm-access-panel adm-muted';
      // Power mode also lands here when the user holds no shareable roles
      // (Base is excluded — sharing with everyone stays ladmin's).
      box.textContent = POWER
        ? 'You hold no shareable roles — the table stays visible only to '
          + 'you. Ask your administrator to share it wider.'
        : 'Roles unavailable — manage access from the Roles section.';
      return;
    }
    const schema = ($('twSchema').value || '').trim().toLowerCase();
    box.className = 'adm-access-panel';
    box.innerHTML = ROLES.map((ro) => {
      const grant = (ro.scope_grants || []).find((g) =>
        g.connection_id === wizardConnId
        && (g.schema == null || String(g.schema).toLowerCase() === schema));
      const explicit = !!(editingTableId && (ro.table_ids || []).includes(editingTableId));
      const covered = !!grant;
      return `<label class="adm-check tw-access-row">
        <input type="checkbox" class="tw-access-check" data-rid="${esc(ro.id)}"
          ${covered || explicit ? 'checked' : ''} ${covered ? 'disabled' : ''}>
        <span>${esc(ro.name)}${ro.is_base ? ' <span class="adm-chip">built-in</span>' : ''}
          ${covered ? `<span class="adm-label-note">via ${grant.schema == null ? 'connection' : 'schema'} grant — no change needed</span>` : ''}
        </span>
      </label>`;
    }).join('');
    _wizAccessReady = true;
  }

  function collectCheckedAccessRoles() {
    const out = [];
    document.querySelectorAll('#twAccessRoles .tw-access-check').forEach((el) => {
      if (el.checked && !el.disabled) out.push(el.dataset.rid);
    });
    return out;
  }

  // ── Account (sidebar footer) ───────────────────────────────────────────
  async function logout() {
    try { await fetch('/auth/logout', { method: 'POST' }); } catch (e) { /* ignore */ }
    window.location.href = '/';
  }

  function openPwModal() {
    ['pwCurrent', 'pwNew', 'pwNew2'].forEach((id) => { $(id).value = ''; });
    $('pwError').classList.add('hidden');
    $('pwModal').classList.remove('hidden');
    $('pwCurrent').focus();
  }

  async function savePassword() {
    const err = $('pwError');
    err.classList.add('hidden');
    if ($('pwNew').value.length < 4) {
      err.textContent = 'Password must be at least 4 characters';
      err.classList.remove('hidden');
      return;
    }
    if ($('pwNew').value !== $('pwNew2').value) {
      err.textContent = 'Passwords do not match';
      err.classList.remove('hidden');
      return;
    }
    const r = await api('/auth/password', {
      method: 'POST',
      body: JSON.stringify({ current_password: $('pwCurrent').value,
        new_password: $('pwNew').value }),
    });
    if (r.ok) {
      $('pwModal').classList.add('hidden');
      toast('Password changed');
    } else {
      err.textContent = r.data.error || 'Could not change the password';
      err.classList.remove('hidden');
    }
  }

  // ── Wire-up ────────────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.adm-nav-item').forEach((b) => {
      b.addEventListener('click', () => showSection(b.dataset.section));
    });
    showSection((location.hash || '').replace('#', ''));

    // ?. binds: these elements are removed by the template in power mode.
    $('btnAddConnection')?.addEventListener('click', () => openConnModal(null));
    $('btnAddConnectionHero')?.addEventListener('click', () => openConnModal(null));
    $('closeConnModal').addEventListener('click', () => $('connModal').classList.add('hidden'));
    $('btnCancelConn').addEventListener('click', () => $('connModal').classList.add('hidden'));
    $('btnTestConn').addEventListener('click', testConnDraft);
    $('btnSaveConn').addEventListener('click', saveConn);
    $('connType').addEventListener('change', onDialectChange);

    $('btnRegisterTable').addEventListener('click', registerTableEntry);
    $('closeTableModal').addEventListener('click', () => $('tableModal').classList.add('hidden'));
    $('btnCancelTable').addEventListener('click', () => $('tableModal').classList.add('hidden'));
    $('btnTwBack').addEventListener('click', () => setWizardStep(wizardStep - 1));
    $('btnTwNext').addEventListener('click', wizardNext);
    $('btnDraftAI').addEventListener('click', draftWithAI);
    $('btnAddRelation').addEventListener('click', () => addRelationRow(null));
    $('twConfirm').addEventListener('change', updateSaveEnabled);
    $('btnSaveTable').addEventListener('click', saveTable);

    $('btnRelScan').addEventListener('click', scanRelations);
    $('btnRelAnalyzeSql').addEventListener('click', analyzeRelSql);
    $('btnRelAcceptChecked').addEventListener('click', acceptCheckedRelCandidates);
    $('btnRelDeleteFlagged').addEventListener('click', deleteFlaggedRelations);
    $('btnRelAddManual').addEventListener('click', relOverviewAdd);
    $('btnRelViewList').addEventListener('click', () => setRelView('list'));
    $('btnRelViewGraph').addEventListener('click', () => setRelView('graph'));
    // Wired once; the buttons outlive every cy instance (destroy/recreate).
    $('btnRelGraphZoomIn').addEventListener('click', () => _relGraphZoom(1.25));
    $('btnRelGraphZoomOut').addEventListener('click', () => _relGraphZoom(1 / 1.25));
    $('btnRelGraphFit').addEventListener('click', () => {
      if (cyInstance) { _hideGraphPopover(); cyInstance.fit(undefined, 30); }
    });

    $('userSearch')?.addEventListener('input', () => {
      _userSearchTyped = true;   // real typing — never clear it again
      renderUsers();
    });
    $('btnAddRole')?.addEventListener('click', () => openRoleModal(null));
    $('closeRoleModal').addEventListener('click', () => $('roleModal').classList.add('hidden'));
    $('btnCancelRole').addEventListener('click', () => $('roleModal').classList.add('hidden'));
    $('btnSaveRole').addEventListener('click', saveRole);
    $('closeRoleDelete').addEventListener('click', () => $('roleDeleteModal').classList.add('hidden'));
    $('btnRoleDeleteCancel').addEventListener('click', () => $('roleDeleteModal').classList.add('hidden'));
    $('btnRoleDeleteGo').addEventListener('click', runRoleDelete);

    $('btnSaveSchedule')?.addEventListener('click', saveSchedule);
    $('btnTsmSave').addEventListener('click', saveTableSchedule);
    $('btnTsmCancel').addEventListener('click', () => $('tableScheduleModal').classList.add('hidden'));
    $('closeTsmModal').addEventListener('click', () => $('tableScheduleModal').classList.add('hidden'));
    $('tsmInherit').addEventListener('change', () => {
      $('tsmFields').classList.add('hidden');
      $('tsmPreview').textContent = 'This table follows the global schedule.';
      $('tsmPreview').classList.remove('adm-bad-text');
    });
    $('tsmOwn').addEventListener('change', () => {
      $('tsmFields').classList.remove('hidden');
      schedulePreviewInto($('tsmFields'), $('tsmPreview'), true);
    });
    $('btnReloadAudit')?.addEventListener('click', loadAudit);

    $('btnAdmLogout').addEventListener('click', logout);
    $('btnAdmChangePw').addEventListener('click', openPwModal);
    $('closePwModal').addEventListener('click', () => $('pwModal').classList.add('hidden'));
    $('btnCancelPw').addEventListener('click', () => $('pwModal').classList.add('hidden'));
    $('btnSavePw').addEventListener('click', savePassword);

    $('closeRecAccept').addEventListener('click', _closeRecAccept);
    $('btnRecAcceptCancel').addEventListener('click', _closeRecAccept);
    $('btnRecAcceptGo').addEventListener('click', _runAcceptRecommendation);
    // A manual pick wins over a suggestion that lands afterwards.
    document.querySelectorAll('input[name="recAcceptType"]').forEach((el) => {
      el.addEventListener('change', () => {
        if (recAcceptCtx) recAcceptCtx.dirty = true;
      });
    });

    loadAll();
  });
})();
