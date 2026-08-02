/* Admin "Data sources" page (ladmin only).
 *
 * Standalone admin panel: single IIFE, fetch + small render helpers, no
 * framework, no dashboard.js. Talks only to /api/admin/* and /auth/*.
 * Credentials never render — the API returns masked rows.
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

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({
      headers: { 'Content-Type': 'application/json' },
    }, opts || {}));
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    return { status: res.status, ok: res.ok, data: data || {} };
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

  // ── Sidebar navigation ─────────────────────────────────────────────────
  const SECTIONS = ['connections', 'tables', 'relations', 'schedule', 'audit'];

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
    if (name === 'audit') loadAudit();
    if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  }

  // ── Bootstrap ──────────────────────────────────────────────────────────
  async function loadAll() {
    const [d, c, t, s] = await Promise.all([
      api('/api/admin/dialects'), api('/api/admin/connections'),
      api('/api/admin/tables'), api('/api/admin/refresh_settings'),
    ]);
    DIALECTS = d.data.dialects || [];
    CONNECTIONS = c.data.connections || [];
    TABLES = t.data.tables || [];
    ENCRYPTION_READY = c.data.encryption_ready !== false;
    $('adsKeyWarning').classList.toggle('hidden', ENCRYPTION_READY);
    ['btnAddConnection', 'btnAddConnectionHero'].forEach((id) => {
      const b = $(id);
      b.disabled = !ENCRYPTION_READY;
      b.title = ENCRYPTION_READY ? '' : 'Set CLIENT_ENCRYPTION_KEY first';
    });
    $('navConnCount').textContent = CONNECTIONS.length || '';
    $('navTableCount').textContent = TABLES.length || '';
    $('navRelCount').textContent = confirmedRelCount() || '';
    // The zero-new explainer quotes a confirmed count — hide it once any
    // mutation refetches state, rather than showing a stale number.
    $('relNoNew').classList.add('hidden');
    renderConnections();
    renderTables();
    renderSchedule(s.data);
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
    const hero = $('connOnboarding');
    if (!CONNECTIONS.length) {
      hero.classList.remove('hidden');
      box.innerHTML = '';
      return;
    }
    hero.classList.add('hidden');
    const rows = CONNECTIONS.map((c) => {
      const where = c.db_type === 'sqlite' ? '(local file)' :
        `${esc(c.host)}:${esc(c.port ?? '')} / ${esc(c.database || c.service_name || '')}`;
      return `
      <tr data-cid="${esc(c.id)}">
        <td><strong>${esc(c.name)}</strong><div class="adm-cell-sub">${esc(c.user)}</div></td>
        <td><span class="adm-chip">${esc(c.db_type)}</span></td>
        <td class="adm-cell-mono">${where}</td>
        <td>${c.table_count || 0}</td>
        <td>${connStatusChips(c)}</td>
        <td class="adm-actions-cell">
          <button class="adm-btn ghost small" data-act="browse">＋ Register table</button>
          <button class="adm-icon-btn" data-act="test" title="Test connection">🔌</button>
          <button class="adm-icon-btn" data-act="refresh" title="Refresh all snapshots on this connection">⟳</button>
          <button class="adm-icon-btn" data-act="edit" title="Edit connection">✎</button>
          <button class="adm-icon-btn" data-act="delete" title="Delete connection">🗑</button>
        </td>
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
    onDialectChange();
    $('connModal').classList.remove('hidden');
    $('connName').focus();
  }

  function onDialectChange() {
    const d = DIALECTS.find((x) => x.key === $('connType').value);
    if (!d) return;
    if (!$('connPort').value) $('connPort').value = d.default_port || '';
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
  function renderTables() {
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
    const rows = TABLES.map((t) => `
      <tr data-tid="${esc(t.id)}">
        <td><strong>${esc(t.display_name)}</strong>${t.is_connector
          ? ' <span class="adm-chip conn" title="Hidden from users; auto-included via relations">connector</span>' : ''}</td>
        <td class="adm-cell-mono">${esc(connName(t.connection_id))} · ${esc([t.schema, t.table_name].filter(Boolean).join('.'))}</td>
        <td>${t.row_count != null ? Number(t.row_count).toLocaleString() : '—'}</td>
        <td title="${esc(t.last_refresh_error || '')}">${t.refreshed_at ? esc(t.refreshed_at)
          : (t.last_refresh_error ? '<span class="adm-chip bad">failed</span>' : '—')}</td>
        <td class="adm-actions-cell">
          <button class="adm-icon-btn" data-act="refresh" title="Refresh snapshot now">⟳</button>
          <button class="adm-icon-btn" data-act="edit" title="Edit descriptions / relations">✎</button>
          <button class="adm-icon-btn" data-act="delete" title="Delete registered table">🗑</button>
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
      if (r.data.ok) {
        const d = r.data.drift || {};
        const driftNote = (d.added || []).length || (d.removed || []).length
          ? ` (schema drift: +${(d.added || []).length}/-${(d.removed || []).length} columns — chats re-synced)` : '';
        toast(`Refreshed: ${Number(r.data.rows).toLocaleString()} rows${driftNote}`);
      } else {
        toast(r.data.error || 'Refresh failed', true);
      }
      loadAll();
    } else if (act === 'edit') {
      openTableWizard(t.connection_id, t);
    } else if (act === 'delete') {
      if (!window.confirm(`Delete registered table "${t.display_name}"? Chats that use it keep their history but the table stops loading.`)) return;
      const r = await api(`/api/admin/tables/${tid}/delete`, { method: 'POST', body: '{}' });
      if (r.ok) toast('Table deleted'); else toast(r.data.error || 'Delete failed', true);
      loadAll();
    }
  }

  // "+ Register table" from the Tables section: one connection → straight in;
  // several → quick pick via prompt-free chooser (first connection preselected
  // in a tiny select is overkill — use a confirm-style chooser list).
  function registerTableEntry() {
    if (!CONNECTIONS.length) {
      showSection('connections');
      toast('Add a database connection first', true);
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

  async function loadSchemas(existing) {
    $('twPickStatus').textContent = 'Loading schemas…';
    const r = await api(`/api/admin/connections/${wizardConnId}/schemas`);
    const schemas = r.data.schemas || [];
    const sel = $('twSchema');
    sel.innerHTML = '';
    (schemas.length ? schemas : ['']).forEach((s) => {
      const o = document.createElement('option');
      o.value = s; o.textContent = s || '(default)';
      sel.appendChild(o);
    });
    if (existing && existing.schema) sel.value = existing.schema;
    else if (r.data.default_schema) sel.value = r.data.default_schema;
    sel.onchange = loadTableNames;
    await loadTableNames();
    $('twPickStatus').textContent = r.data.ok === false ? (r.data.error || 'Failed to list schemas') : '';
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
    $('twIsConnector').checked = existing ? !!existing.is_connector
      : !!(wizardPrefill && wizardPrefill.connector);
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

  async function acceptRecommendation(rec) {
    const phys = _recPhysLabel(rec);
    if (!window.confirm(`Register "${phys}" as a connector table now?\n\n`
      + 'Descriptions are AI-drafted — you can edit them later in the table’s settings '
      + '(Registered tables → ✎). A snapshot of the table is taken, and its relations are '
      + 'proposed for review right away.')) return;
    _busy(`Registering ${phys}… introspecting, drafting descriptions, snapshotting.`);
    let r;
    try {
      r = await api('/api/admin/relations/recommendations/accept', {
        method: 'POST', body: JSON.stringify({ id: rec.id }) });
    } finally { _busyDone(); }
    if (!r.data.ok) {
      toast(r.data.error || 'Registration failed — the recommendation stays open', true);
      loadRelRecommendations();
      return;
    }
    toast(r.data.note
      || `Registered "${phys}" as a connector — review its proposed relations below`);
    mergeRelCandidates(r.data.candidates || []);
    renderRelCandidates();
    _renderRelWarnings(_warnLinesFromReplay(r.data.evidence_warnings));
    loadRelRecommendations();
    loadAll();
  }

  function registerGhost(g) {
    openTableWizard(g.connection_id, null, false,
      { schema: g.schema || '', table: g.table, connector: true });
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

  let _cyLoadPromise = null;
  function ensureCytoscape() {
    if (window.cytoscape) return Promise.resolve();
    if (_cyLoadPromise) return _cyLoadPromise;   // single-flight
    _cyLoadPromise = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = '/static/vendor/cytoscape/cytoscape.min.js';
      s.onload = resolve;
      s.onerror = () => { _cyLoadPromise = null; reject(new Error('cytoscape load failed')); };
      document.head.appendChild(s);
    });
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

  function renderRelGraph(nodes, edges) {
    if (cyInstance) { cyInstance.destroy(); cyInstance = null; }
    _hideGraphPopover();
    const elements = [];
    nodes.forEach((n) => {
      const color = n.ghost ? REL_GHOST_COLOR
        : n.isolated ? REL_ISOLATED_COLOR
        : REL_COMPONENT_COLORS[(n.component || 0) % REL_COMPONENT_COLORS.length];
      elements.push({ data: {
        id: n.id,
        display: n.label + (n.connector ? ' ⚙' : '') + '\n' + (n.sub || ''),
        color,
        size: 34 + Math.min(26, (n.relation_count || 0) * 6),
        connector: n.connector ? 1 : 0,
        ghost: n.ghost ? 1 : 0,
        isolated: n.isolated ? 1 : 0,
        connection_id: n.connection_id || '', schema: n.schema || '',
        table: n.table || '', label: n.label,
      } });
    });
    edges.forEach((e) => {
      elements.push({ data: {
        id: e.id, source: e.source, target: e.target,
        elabel: (e.keys_label || '')
          + (e.cardinality ? ` · ${REL_CARD_LABEL[e.cardinality] || e.cardinality}` : ''),
        ghost: e.ghost ? 1 : 0,
        suspicious: e.suspicious ? 1 : 0,
        keys_label: e.keys_label || '', cardinality: e.cardinality || '',
        origin: e.origin || 'manual', join_keys: e.join_keys || [],
        related_ref: e.related_ref || '', related_is_id: e.related_is_id ? 1 : 0,
      } });
    });
    cyInstance = cytoscape({
      container: $('relGraph'),
      elements,
      style: [
        { selector: 'node', style: {
          label: 'data(display)', 'text-wrap': 'wrap', 'text-valign': 'bottom',
          'text-margin-y': 6, 'font-size': 10, color: '#334155',
          width: 'data(size)', height: 'data(size)',
          'background-color': 'data(color)',
        } },
        { selector: 'node[connector = 1]', style: {
          shape: 'round-rectangle', 'border-width': 3, 'border-color': '#0f172a',
        } },
        { selector: 'node[ghost = 1]', style: {
          'background-opacity': 0.15, 'border-width': 2,
          'border-style': 'dashed', 'border-color': REL_GHOST_COLOR,
        } },
        { selector: 'edge', style: {
          'curve-style': 'bezier', width: 2, 'line-color': '#94a3b8',
          'target-arrow-shape': 'triangle', 'target-arrow-color': '#94a3b8',
          label: 'data(elabel)', 'font-size': 9, color: '#64748b',
          'text-rotation': 'autorotate', 'text-background-color': '#fff',
          'text-background-opacity': 0.85, 'text-background-padding': 2,
        } },
        { selector: 'edge[ghost = 1]', style: { 'line-style': 'dashed' } },
        { selector: 'edge[suspicious = 1]', style: {
          'line-style': 'dashed', 'line-color': REL_ISOLATED_COLOR,
          'target-arrow-color': REL_ISOLATED_COLOR,
        } },
        { selector: '.dim', style: { opacity: 0.15 } },
      ],
      layout: { name: 'cose', animate: false, padding: 30 },
    });

    cyInstance.on('tap', (evt) => {
      if (evt.target === cyInstance) {           // background tap: clear
        cyInstance.elements().removeClass('dim');
        _hideGraphPopover();
      }
    });
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
      <span class="lg"><span class="sw" style="background:${REL_COMPONENT_COLORS[0]}"></span> cluster (one color per group of joined tables)</span>
      <span class="lg"><span class="sw" style="background:${REL_ISOLATED_COLOR}"></span> not joined to any table — the AI cannot combine it</span>
      <span class="lg"><span class="sw" style="border:2px solid #0f172a; background:#fff"></span> ⚙ connector table</span>
      <span class="lg"><span class="sw" style="border:2px dashed ${REL_GHOST_COLOR}; background:#fff"></span> referenced but not registered (click to register)</span>
      <span class="lg">— arrow points child → parent; dashed red = same physical table</span>`;
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
  function renderSchedule(s) {
    $('refreshEnabled').checked = !!s.refresh_enabled;
    $('refreshTime').value = s.refresh_time || '00:00';
    const bits = [];
    if (s.next_run_at) bits.push(`Next run: ${s.next_run_at}`);
    if (s.last_run_at) bits.push(`Last run: ${s.last_run_at}`);
    $('scheduleInfo').textContent = bits.join(' · ');
  }

  async function saveSchedule() {
    const r = await api('/api/admin/refresh_settings', {
      method: 'POST',
      body: JSON.stringify({ refresh_enabled: $('refreshEnabled').checked,
        refresh_time: $('refreshTime').value }),
    });
    if (r.ok) { toast('Schedule saved'); renderSchedule(r.data); }
    else toast(r.data.error || 'Save failed', true);
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

    $('btnAddConnection').addEventListener('click', () => openConnModal(null));
    $('btnAddConnectionHero').addEventListener('click', () => openConnModal(null));
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

    $('btnSaveSchedule').addEventListener('click', saveSchedule);
    $('btnReloadAudit').addEventListener('click', loadAudit);

    $('btnAdmLogout').addEventListener('click', logout);
    $('btnAdmChangePw').addEventListener('click', openPwModal);
    $('closePwModal').addEventListener('click', () => $('pwModal').classList.add('hidden'));
    $('btnCancelPw').addEventListener('click', () => $('pwModal').classList.add('hidden'));
    $('btnSavePw').addEventListener('click', savePassword);

    loadAll();
  });
})();
