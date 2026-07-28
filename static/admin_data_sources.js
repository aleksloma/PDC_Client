/* Admin "Data sources" page (ladmin only).
 *
 * Deliberately independent from dashboard.js (the dashboard_view.js pattern):
 * a single IIFE, fetch + small render helpers, no framework. Talks only to
 * /api/admin/*. Credentials never render — the API returns masked rows.
 *
 * Mandatory-confirm (client side): the "Save & snapshot" button stays
 * disabled until the review checkbox is ticked, and the tick is force-cleared
 * whenever a new AI draft lands or another table is loaded. The server
 * enforces the same gate independently (confirm:true, CONFIRM_REQUIRED).
 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);

  function toast(msg, isErr) {
    // Minimal inline toast (dashboard.js's showToast lives on the /lab page).
    const t = document.createElement('div');
    t.textContent = msg;
    t.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);' +
      'background:' + (isErr ? '#991b1b' : '#0f172a') + ';color:#fff;padding:10px 18px;' +
      'border-radius:8px;font-size:14px;z-index:9999;box-shadow:0 4px 14px rgba(0,0,0,.25)';
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
  let editingConnId = null;     // null = creating
  let editingTableId = null;    // null = registering new
  let currentIntro = null;      // last introspection result (register wizard)
  let wizardConnId = null;

  // ── Bootstrap ────────────────────────────────────────────────────────────
  async function loadAll() {
    const [d, c, t, s] = await Promise.all([
      api('/api/admin/dialects'), api('/api/admin/connections'),
      api('/api/admin/tables'), api('/api/admin/refresh_settings'),
    ]);
    DIALECTS = d.data.dialects || [];
    CONNECTIONS = c.data.connections || [];
    TABLES = t.data.tables || [];
    $('adsKeyWarning').classList.toggle('hidden', c.data.encryption_ready !== false);
    renderConnections();
    renderTables();
    renderSchedule(s.data);
  }

  // ── Connections ──────────────────────────────────────────────────────────
  function renderConnections() {
    const box = $('connectionsList');
    box.innerHTML = '';
    if (!CONNECTIONS.length) {
      box.innerHTML = '<div class="ads-muted">No connections yet. Add one to register database tables.</div>';
      return;
    }
    CONNECTIONS.forEach((c) => {
      const card = document.createElement('div');
      card.className = 'ads-card';
      const where = c.db_type === 'sqlite' ? '(local file)' :
        `${esc(c.user)}@${esc(c.host)}:${esc(c.port ?? '')}/${esc(c.database || c.service_name || '')}`;
      const pwChip = !c.password_set ? '<span class="ads-chip bad">no password</span>'
        : (c.password_readable ? '' : '<span class="ads-chip bad">re-enter password</span>');
      const testChip = c.last_test_ok == null ? ''
        : (c.last_test_ok ? '<span class="ads-chip ok">test ok</span>' : '<span class="ads-chip bad">test failed</span>');
      card.innerHTML = `
        <div class="ads-card-title">${esc(c.name)} <span class="ads-chip">${esc(c.db_type)}</span> ${pwChip} ${testChip}</div>
        <div class="ads-card-sub">${where} · ${c.table_count || 0} table(s)</div>
        <div class="ads-card-actions">
          <button class="ghost small" data-act="test">🔌 Test</button>
          <button class="ghost small" data-act="browse">📋 Register table</button>
          <button class="ghost small" data-act="refresh">⟳ Refresh all</button>
          <button class="ghost small" data-act="edit">✎ Edit</button>
          <button class="ghost small" data-act="delete">🗑 Delete</button>
        </div>`;
      card.querySelectorAll('button').forEach((b) => {
        b.addEventListener('click', () => connAction(b.dataset.act, c));
      });
      box.appendChild(card);
    });
  }

  async function connAction(act, c) {
    if (act === 'test') {
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

  // ── Registered tables ────────────────────────────────────────────────────
  function renderTables() {
    const box = $('tablesList');
    if (!TABLES.length) {
      box.innerHTML = '<div class="ads-muted">No tables registered yet. Use "Register table" on a connection.</div>';
      return;
    }
    const connName = (cid) => (CONNECTIONS.find((c) => c.id === cid) || {}).name || '?';
    const rows = TABLES.map((t) => `
      <tr data-tid="${esc(t.id)}">
        <td><strong>${esc(t.display_name)}</strong>${t.is_connector ? ' <span class="ads-chip">connector</span>' : ''}</td>
        <td>${esc(connName(t.connection_id))}</td>
        <td>${esc([t.schema, t.table_name].filter(Boolean).join('.'))}</td>
        <td>${t.row_count != null ? Number(t.row_count).toLocaleString() : '—'}</td>
        <td title="${esc(t.last_refresh_error || '')}">${t.refreshed_at ? esc(t.refreshed_at) : (t.last_refresh_error ? '<span class="ads-chip bad">failed</span>' : '—')}</td>
        <td>
          <button class="ghost small" data-act="refresh">⟳</button>
          <button class="ghost small" data-act="edit">✎</button>
          <button class="ghost small" data-act="delete">🗑</button>
        </td>
      </tr>`).join('');
    box.innerHTML = `<table class="ads-table">
      <thead><tr><th>Display name</th><th>Connection</th><th>Source table</th>
      <th>Rows</th><th>Data as of</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table>`;
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

  // ── Register / edit wizard ───────────────────────────────────────────────
  async function openTableWizard(connId, existing) {
    wizardConnId = connId;
    editingTableId = existing ? existing.id : null;
    currentIntro = null;
    $('tableModalTitle').textContent = existing
      ? `Edit table — ${existing.display_name}` : 'Register table';
    $('twDetail').classList.add('hidden');
    $('twConfirm').checked = false;
    $('twDraftBanner').classList.add('hidden');
    updateSaveEnabled();
    $('twPickStatus').textContent = 'Loading schemas…';
    $('tableModal').classList.remove('hidden');

    const r = await api(`/api/admin/connections/${connId}/schemas`);
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
    if (existing) {
      $('twTable').value = existing.table_name;
      await introspectNow(existing);
    }
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

  async function introspectNow(existing) {
    $('twPickStatus').textContent = 'Introspecting…';
    const r = await api('/api/admin/tables/introspect', {
      method: 'POST',
      body: JSON.stringify({ connection_id: wizardConnId,
        schema: $('twSchema').value, table: $('twTable').value }),
    });
    if (!r.data.ok) {
      $('twPickStatus').textContent = r.data.error || 'Introspection failed';
      return;
    }
    $('twPickStatus').textContent = '';
    currentIntro = r.data.introspection;
    const prevByName = {};
    (existing && existing.columns || []).forEach((c) => { prevByName[c.name] = c; });

    $('twDetail').classList.remove('hidden');
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
        <td class="ads-muted">${esc(c.dtype)}</td>
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
      prevW.innerHTML = `<table class="ads-preview-table"><thead><tr>${
        (p.columns || []).map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${
        p.rows.map((row) => `<tr>${row.map((v) => `<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
    } else {
      prevW.innerHTML = `<div class="ads-muted">${esc(p.error || 'No preview rows.')}</div>`;
    }

    renderRelations(existing ? (existing.relations || []) : seedRelationsFromFks());
    $('twConfirm').checked = false;
    $('twDraftBanner').classList.add('hidden');
    updateSaveEnabled();
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
    row.className = 'ads-rel-row';
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
      <button type="button" class="ghost small tw-rel-remove">×</button>`;
    row.querySelector('.tw-rel-remove').addEventListener('click', () => row.remove());
    box.appendChild(row);
  }

  function collectRelations() {
    const rels = [];
    document.querySelectorAll('#twRelations .ads-rel-row').forEach((row) => {
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
    $('btnSaveTable').textContent = 'Save & snapshot';
    updateSaveEnabled();
    if (r.status === 409) {
      toast(r.data.error || 'Table structure changed — re-run introspect.', true);
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

  // ── Schedule + audit ─────────────────────────────────────────────────────
  function renderSchedule(s) {
    $('refreshEnabled').checked = !!s.refresh_enabled;
    $('refreshTime').value = s.refresh_time || '00:00';
    const bits = [];
    if (s.next_run_at) bits.push(`next run: ${s.next_run_at}`);
    if (s.last_run_at) bits.push(`last run: ${s.last_run_at}`);
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

  async function toggleAudit() {
    const box = $('auditList');
    const showing = !box.classList.contains('hidden');
    if (showing) { box.classList.add('hidden'); $('btnToggleAudit').textContent = 'Show'; return; }
    const r = await api('/api/admin/audit?limit=200');
    box.innerHTML = (r.data.rows || []).map((row) =>
      `<div class="row ${row.ok === false ? 'bad' : ''}">${esc(row.ts)} · ${esc(row.actor)} · ${esc(row.action)}${row.target ? ' · ' + esc(row.target) : ''}</div>`
    ).join('') || '<div class="row">No audit entries yet.</div>';
    box.classList.remove('hidden');
    $('btnToggleAudit').textContent = 'Hide';
  }

  // ── Wire-up ──────────────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', () => {
    if (window.i18n && window.i18n.init) { try { window.i18n.init(); } catch (e) {} }
    $('btnAddConnection').addEventListener('click', () => openConnModal(null));
    $('closeConnModal').addEventListener('click', () => $('connModal').classList.add('hidden'));
    $('btnCancelConn').addEventListener('click', () => $('connModal').classList.add('hidden'));
    $('btnTestConn').addEventListener('click', testConnDraft);
    $('btnSaveConn').addEventListener('click', saveConn);
    $('connType').addEventListener('change', onDialectChange);

    $('closeTableModal').addEventListener('click', () => $('tableModal').classList.add('hidden'));
    $('btnCancelTable').addEventListener('click', () => $('tableModal').classList.add('hidden'));
    $('btnIntrospect').addEventListener('click', () => introspectNow(null));
    $('btnDraftAI').addEventListener('click', draftWithAI);
    $('btnAddRelation').addEventListener('click', () => addRelationRow(null));
    $('twConfirm').addEventListener('change', updateSaveEnabled);
    $('btnSaveTable').addEventListener('click', saveTable);

    $('btnSaveSchedule').addEventListener('click', saveSchedule);
    $('btnToggleAudit').addEventListener('click', toggleAudit);

    loadAll();
  });
})();
