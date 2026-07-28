/* Admin "Data sources" page (ladmin only).
 *
 * Standalone admin panel: single IIFE, fetch + small render helpers, no
 * framework, no dashboard.js. Talks only to /api/admin/* and /auth/*.
 * Credentials never render — the API returns masked rows.
 *
 * Layout: sidebar nav switches four sections (connections / tables /
 * schedule / audit); state is a full refetch after every mutation.
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
  let wizardConnId = null;
  let wizardStep = 1;

  // ── Sidebar navigation ─────────────────────────────────────────────────
  const SECTIONS = ['connections', 'tables', 'schedule', 'audit'];

  function showSection(name) {
    if (!SECTIONS.includes(name)) name = 'connections';
    SECTIONS.forEach((s) => {
      const sec = $('sec' + s.charAt(0).toUpperCase() + s.slice(1));
      if (sec) sec.hidden = (s !== name);
    });
    document.querySelectorAll('.adm-nav-item').forEach((b) => {
      b.classList.toggle('active', b.dataset.section === name);
    });
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
    renderConnections();
    renderTables();
    renderSchedule(s.data);
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
      toast(`Testing "${c.name}"…`);
      const r = await api('/api/admin/connections/test', {
        method: 'POST', body: JSON.stringify({ connection_id: c.id }) });
      if (r.data.ok) toast(`Connection OK (${r.data.server_version || 'server'} · ${r.data.elapsed_ms}ms)`);
      else toast(r.data.error || 'Connection failed', true);
      loadAll();
    } else if (act === 'browse') {
      openTableWizard(c.id);
    } else if (act === 'refresh') {
      toast('Refreshing all tables on this connection…');
      const r = await api(`/api/admin/connections/${c.id}/refresh`, { method: 'POST', body: '{}' });
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
      toast(`Refreshing "${t.display_name}"…`);
      const r = await api(`/api/admin/tables/${tid}/refresh`, { method: 'POST', body: '{}' });
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
    if (n === 3) renderSummary();
  }

  async function openTableWizard(connId, existing, chooseConn) {
    wizardConnId = connId;
    editingTableId = existing ? existing.id : null;
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

    await loadSchemas(existing);
    if (existing) {
      $('twTable').value = existing.table_name;
      await introspectNow(existing, { advance: false });
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
    (r.data.tables || []).forEach((t) => {
      const o = document.createElement('option');
      o.value = t.name;
      o.textContent = t.name + (t.registered ? ' (registered)' : '') + (t.kind === 'view' ? ' [view]' : '');
      sel.appendChild(o);
    });
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
    const prevByName = {};
    (existing && existing.columns || []).forEach((c) => { prevByName[c.name] = c; });

    $('twDisplayName').value = existing ? existing.display_name
      : $('twTable').value.toLowerCase().replace(/_/g, ' ');
    $('twIsConnector').checked = existing ? !!existing.is_connector : false;
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

    renderRelations(existing ? (existing.relations || []) : seedRelationsFromFks());
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

  // Pre-seed the relations editor from the introspected FKs when the referred
  // table is already registered on the same connection.
  function seedRelationsFromFks() {
    const rels = [];
    (currentIntro.foreign_keys || []).forEach((fk) => {
      const target = TABLES.find((t) => t.connection_id === wizardConnId
        && t.table_name === fk.referred_table
        && (!fk.referred_schema || t.schema === fk.referred_schema));
      if (!target) return;
      const pairs = (fk.constrained_columns || []).map((c, i) =>
        [c, (fk.referred_columns || [])[i] || c]);
      rels.push({ related_table_id: target.id, join_keys: pairs });
    });
    return rels;
  }

  function renderRelations(rels) {
    const box = $('twRelations');
    box.innerHTML = '';
    (rels || []).forEach((rel) => addRelationRow(rel));
  }

  function addRelationRow(rel) {
    const box = $('twRelations');
    const row = document.createElement('div');
    row.className = 'adm-rel-row';
    const options = TABLES
      .filter((t) => t.id !== editingTableId)
      .map((t) => `<option value="${esc(t.id)}" ${rel && rel.related_table_id === t.id ? 'selected' : ''}>${esc(t.display_name)}</option>`)
      .join('');
    const pairs = (rel && rel.join_keys || [['', '']])
      .map((p) => `${p[0]}=${p[1]}`).join(', ');
    row.innerHTML = `
      <span>joins</span>
      <select class="tw-rel-target"><option value="">— pick table —</option>${options}</select>
      <span>on</span>
      <input type="text" class="tw-rel-keys" placeholder="my_col=their_col, …" value="${esc(pairs)}">
      <button type="button" class="adm-icon-btn tw-rel-remove" title="Remove relation">×</button>`;
    row.querySelector('.tw-rel-remove').addEventListener('click', () => row.remove());
    box.appendChild(row);
  }

  function collectRelations() {
    const rels = [];
    document.querySelectorAll('#twRelations .adm-rel-row').forEach((row) => {
      const target = row.querySelector('.tw-rel-target').value;
      if (!target) return;
      const pairs = row.querySelector('.tw-rel-keys').value.split(',')
        .map((s) => s.trim()).filter(Boolean)
        .map((s) => s.split('=').map((x) => x.trim()))
        .filter((p) => p.length === 2 && p[0] && p[1]);
      if (pairs.length) rels.push({ related_table_id: target, join_keys: pairs });
    });
    return rels;
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
    const body = {
      connection_id: wizardConnId,
      schema: $('twSchema').value,
      table_name: $('twTable').value,
      display_name: $('twDisplayName').value.trim(),
      description: $('twDescription').value.trim(),
      columns,
      is_connector: $('twIsConnector').checked,
      relations: collectRelations(),
      confirm: $('twConfirm').checked === true,
    };
    $('btnSaveTable').disabled = true;
    $('btnSaveTable').textContent = 'Snapshotting…';
    const path = editingTableId ? `/api/admin/tables/${editingTableId}` : '/api/admin/tables';
    const r = await api(path, { method: 'POST', body: JSON.stringify(body) });
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
