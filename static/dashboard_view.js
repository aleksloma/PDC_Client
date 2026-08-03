/* Dashboard page (/dashboards/{id}) — renders pinned tiles from stored
 * snapshots, GridStack drag/resize grid, per-tile actions, refresh-all,
 * rename/share/delete. Loaded ONLY on dashboard_view.html; deliberately
 * independent from dashboard.js (which is wired to the /lab DOM).
 *
 * Everything here talks to the client's own endpoints only — tile refresh is
 * local re-execution on the server, nothing crosses to the brain.
 */
(function () {
  'use strict';

  const DASH_ID = window.DASH_ID || '';
  const GRID_COLUMNS = 12;

  let dash = null;          // the loaded dashboard doc {name, tiles, is_owner, ...}
  let grid = null;          // GridStack instance
  let isOwner = false;
  let saveTimer = null;
  const chatDfKeys = {};    // chat_id -> {keys: Set, blocked: Set} | null (unknown)

  function _t(key, fallback) {
    if (window.i18n && typeof window.i18n.t === 'function') {
      const v = window.i18n.t(key);
      if (v && v !== key) return v;
    }
    return fallback;
  }

  function showToast(text, isError = false) {
    const t = document.createElement('div');
    t.textContent = text;
    t.style.cssText = `position:fixed;bottom:24px;left:50%;transform:translateX(-50%);padding:12px 24px;border-radius:8px;color:#fff;font-size:14px;z-index:99999;box-shadow:0 4px 12px rgba(0,0,0,0.15);transition:opacity 0.3s;background:${isError ? '#ef4444' : '#10b981'};`;
    document.body.appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, 3000);
  }

  const REFRESH_SVG = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10"/><path d="M20.49 15a9 9 0 0 1-14.85 3.36L1 14"/></svg>';

  // ── cell formatting (adapted copies of dashboard.js helpers) ─────────────
  const IDENT_HEAD_WORDS = new Set(['year', 'yr', 'წელი', 'id', 'ids', 'code', 'codes', 'კოდი', 'იდ', 'zip', 'postal', 'phone', 'account', 'iban', 'invoice', 'order', 'ref']);

  function isIdentifierLabel(name) {
    if (name == null) return false;
    const toks = String(name).match(/\p{L}+/gu);
    if (!toks || !toks.length) return false;
    return IDENT_HEAD_WORDS.has(toks[toks.length - 1].toLowerCase());
  }

  function fmtCellValue(colName, v) {
    if (typeof v === 'number' && isFinite(v) && !isIdentifierLabel(colName)) {
      return Number.isInteger(v)
        ? v.toLocaleString('en-US')
        : v.toLocaleString('en-US', { maximumFractionDigits: 2 });
    }
    return (v === null || v === undefined) ? '' : String(v);
  }

  function makeSortableTable(tableEl) {
    if (!tableEl) return;
    const thead = tableEl.tHead || tableEl.querySelector('thead');
    const tbody = tableEl.tBodies && tableEl.tBodies[0] ? tableEl.tBodies[0] : tableEl.querySelector('tbody');
    if (!thead || !tbody) return;
    const headerRow = thead.rows ? thead.rows[thead.rows.length - 1] : thead.querySelector('tr');
    if (!headerRow) return;
    const ths = Array.from(headerRow.cells || headerRow.children);
    if (!ths.length) return;
    const parseNum = (t) => {
      if (t == null) return NaN;
      const s = String(t).replace(/,/g, '').trim();
      if (s === '') return NaN;
      const cleaned = s.replace(/[^0-9.\-eE+]/g, '');
      if (!/[0-9]/.test(cleaned)) return NaN;
      const n = Number(cleaned);
      return isFinite(n) ? n : NaN;
    };
    let sortCol = -1, sortDir = 1;
    ths.forEach((th, ci) => {
      th.style.cursor = 'pointer';
      th.style.userSelect = 'none';
      th.addEventListener('click', () => {
        if (sortCol === ci) sortDir = -sortDir; else { sortCol = ci; sortDir = 1; }
        const rows = Array.from(tbody.rows);
        if (!rows.length) return;
        let numeric = 0, seen = 0;
        rows.forEach(r => {
          const t = r.cells[ci] ? (r.cells[ci].textContent || '').trim() : '';
          if (t === '') return;
          seen++;
          if (!isNaN(parseNum(t))) numeric++;
        });
        const isNum = seen > 0 && numeric / seen >= 0.6;
        rows.sort((a, b) => {
          const ta = a.cells[ci] ? (a.cells[ci].textContent || '').trim() : '';
          const tb = b.cells[ci] ? (b.cells[ci].textContent || '').trim() : '';
          let cmp;
          if (isNum) {
            const na = parseNum(ta), nb = parseNum(tb);
            cmp = (isNaN(na) ? Infinity : na) - (isNaN(nb) ? Infinity : nb);
          } else {
            cmp = ta.localeCompare(tb, undefined, { numeric: true, sensitivity: 'base' });
          }
          return cmp * sortDir;
        });
        rows.forEach(r => tbody.appendChild(r));
      });
    });
  }

  // ── tile body rendering ──────────────────────────────────────────────────
  function isPlotlySnapshot(tile) {
    const img = (tile.snapshot || {}).image_base64 || '';
    return !!((tile.snapshot || {}).is_plotly) ||
      (typeof img === 'string' && img.trim().startsWith('<'));
  }

  function renderTileBody(bodyEl, tile) {
    bodyEl.innerHTML = '';
    const snap = tile.snapshot || {};
    if (tile.kind === 'chart') {
      const content = snap.image_base64 || '';
      if (isPlotlySnapshot(tile)) {
        const iframe = document.createElement('iframe');
        iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin');
        iframe.srcdoc = content;
        bodyEl.appendChild(iframe);
      } else {
        const img = document.createElement('img');
        img.src = 'data:image/png;base64,' + content;
        img.alt = tile.title || 'chart';
        bodyEl.appendChild(img);
      }
      return;
    }
    // table tile
    const wrap = document.createElement('div');
    wrap.className = 'pdc-tile-tablewrap';
    const table = snap.table || {};
    const cols = Array.isArray(table.columns) ? table.columns : [];
    const rows = Array.isArray(table.rows) ? table.rows : [];
    const tot = typeof table.total_rows === 'number' ? table.total_rows : rows.length;
    if (tot > rows.length) {
      const meta = document.createElement('div');
      meta.style.cssText = 'font-size:11px;color:#6b7280;margin:2px 0 4px;';
      meta.textContent = _t('dash.table_preview', 'Showing {n} of {m} rows')
        .replace('{n}', String(rows.length)).replace('{m}', String(tot));
      wrap.appendChild(meta);
    }
    // Styled table (pandas Styler) — render its HTML so the conditional
    // formatting (colors) survives; adapted from /lab's appendTableTo.
    if (table.styled_html && typeof table.styled_html === 'string') {
      const styledContainer = document.createElement('div');
      styledContainer.innerHTML = table.styled_html;
      const styledTable = styledContainer.querySelector('table');
      if (styledTable) {
        styledTable.style.cssText = 'width:100%;border-collapse:collapse;font-size:13px;';
        styledTable.querySelectorAll('th, td').forEach(cell => {
          cell.style.border = '1px solid #e5e7eb';
          cell.style.padding = '6px 8px';
          cell.style.textAlign = 'left';
        });
        styledTable.querySelectorAll('th').forEach(th => {
          if (!th.style.backgroundColor) th.style.backgroundColor = '#f9fafb';
          th.style.fontWeight = '600';
        });
        wrap.appendChild(styledContainer);
        bodyEl.appendChild(wrap);
        makeSortableTable(styledTable);
        return;
      }
      // Malformed styled_html → fall through to the plain renderer.
    }
    const tbl = document.createElement('table');
    const thead = document.createElement('thead');
    const trh = document.createElement('tr');
    cols.forEach(c => {
      const th = document.createElement('th');
      th.textContent = c;
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    tbl.appendChild(thead);
    const tbody = document.createElement('tbody');
    rows.forEach(row => {
      const tr = document.createElement('tr');
      cols.forEach(c => {
        const td = document.createElement('td');
        let v = row && Object.prototype.hasOwnProperty.call(row, c) ? row[c] : '';
        td.textContent = fmtCellValue(c, v);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
    wrap.appendChild(tbl);
    bodyEl.appendChild(wrap);
    makeSortableTable(tbl);
  }

  function setTileBadge(tileEl, text) {
    let badge = tileEl.querySelector('.pdc-tile-badge');
    if (!text) {
      if (badge) badge.classList.add('hidden');
      return;
    }
    if (!badge) {
      badge = document.createElement('div');
      badge.className = 'pdc-tile-badge';
      tileEl.querySelector('.pdc-tile').appendChild(badge);
    }
    badge.textContent = text;
    badge.title = text;
    badge.classList.remove('hidden');
  }

  function frozenText(reason) {
    if (reason === 'source_deleted') {
      return _t('dash.frozen_source_deleted', 'Source chat was deleted — showing last saved result');
    }
    if (reason === 'access_revoked') {
      return _t('dash.no_access', 'No access to the source data — showing saved result');
    }
    if (reason === 'role_denied') {
      // Caller-specific, never persisted — the tile stays live for viewers
      // whose role covers the table (mirror of access_revoked).
      return _t('dash.frozen_role_denied', "Your role doesn't include this data — showing saved result");
    }
    return _t('dash.stale', 'Saved result shown');
  }

  // ── description popover ──────────────────────────────────────────────────
  function closeAllPopovers() {
    document.querySelectorAll('.pdc-tile-pop, .pdc-tile-pop-backdrop').forEach(el => el.remove());
    document.removeEventListener('keydown', popEscHandler);
  }

  function popEscHandler(e) {
    if (e.key === 'Escape') closeAllPopovers();
  }

  function openDescription(tileCard, tile) {
    closeAllPopovers();
    // Full-viewport transparent backdrop: clicks inside Plotly iframes do NOT
    // bubble to document, so a naive outside-click listener would never fire.
    const backdrop = document.createElement('div');
    backdrop.className = 'pdc-tile-pop-backdrop';
    backdrop.addEventListener('click', closeAllPopovers);
    document.body.appendChild(backdrop);
    const pop = document.createElement('div');
    pop.className = 'pdc-tile-pop';
    pop.textContent = tile.description || '';
    tileCard.appendChild(pop);
    document.addEventListener('keydown', popEscHandler);
  }

  // ── tile actions ─────────────────────────────────────────────────────────
  function actShowData(tile) {
    if (!window.PDCViewers) return;
    if (tile.kind === 'chart') {
      if (tile.chart_data) window.PDCViewers.openData(tile.chart_data);
      return;
    }
    if (tile.full_table_key) {
      const win = window.PDCViewers.openBlankWindow('Table data');
      fetch(`/api/chat/${tile.chat_id}/full_table/${tile.full_table_key}`)
        .then(r => r.ok ? r.json() : Promise.reject(r.status))
        .then(t => window.PDCViewers.renderData(win, t))
        .catch(() => window.PDCViewers.renderData(win, (tile.snapshot || {}).table || null));
      return;
    }
    window.PDCViewers.openData((tile.snapshot || {}).table || null);
  }

  function actShowCode(tile) {
    if (window.PDCViewers && tile.code) window.PDCViewers.openCode(tile.code);
  }

  async function actDownload(tile, btn) {
    const original = btn.innerHTML;
    btn.disabled = true;
    try {
      if (tile.kind === 'table') {
        const timestamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '_');
        const filename = `table_${timestamp}`;
        const snapTable = (tile.snapshot || {}).table || {};
        let res;
        if (tile.full_table_key) {
          res = await fetch(`/api/chat/${tile.chat_id}/download_excel/${tile.full_table_key}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename }),
          });
        }
        if (!res || !res.ok) {
          res = await fetch(`/api/chat/${tile.chat_id}/export_excel`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ columns: snapTable.columns || [], rows: snapTable.rows || [], filename }),
          });
        }
        if (!res.ok) throw new Error('export failed');
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `${filename}.xlsx`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
        return;
      }
      // chart
      let filename = 'chart';
      try {
        const nameRes = await fetch(`/api/chat/${tile.chat_id}/generate_filename`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: tile.description || tile.title || '', type: 'image' }),
        });
        const nameData = await nameRes.json();
        if (nameData && nameData.filename) filename = nameData.filename;
      } catch (_) { /* keep default */ }
      const content = (tile.snapshot || {}).image_base64 || '';
      if (isPlotlySnapshot(tile)) {
        const res = await fetch(`/api/chat/${tile.chat_id}/export_plotly_png`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ html: content, filename, scale: 3 }),
        });
        if (!res.ok) throw new Error('png export failed');
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `${filename}.png`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
      } else {
        const link = document.createElement('a');
        link.href = 'data:image/png;base64,' + content;
        link.download = `${filename}.png`;
        document.body.appendChild(link);
        link.click();
        link.remove();
      }
    } catch (e) {
      console.error('Tile download failed:', e);
      showToast(_t('dash.download_failed', 'Download failed'), true);
    } finally {
      btn.disabled = false;
      btn.innerHTML = original;
    }
  }

  function actViewLarger(tile) {
    const modal = document.createElement('div');
    modal.className = 'dash-larger-modal';
    const closeBtn = document.createElement('button');
    closeBtn.className = 'dash-larger-close';
    closeBtn.textContent = '×';
    const close = () => { modal.remove(); document.removeEventListener('keydown', esc); };
    const esc = (e) => { if (e.key === 'Escape') close(); };
    closeBtn.addEventListener('click', close);
    modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
    document.addEventListener('keydown', esc);

    const snap = tile.snapshot || {};
    if (tile.kind === 'chart' && isPlotlySnapshot(tile)) {
      const iframe = document.createElement('iframe');
      iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin');
      iframe.srcdoc = snap.image_base64 || '';
      modal.appendChild(iframe);
    } else if (tile.kind === 'chart') {
      const img = document.createElement('img');
      img.src = 'data:image/png;base64,' + (snap.image_base64 || '');
      modal.appendChild(img);
    } else {
      const box = document.createElement('div');
      box.className = 'dash-larger-tablebox';
      renderTileBody(box, tile);
      const inner = box.querySelector('.pdc-tile-tablewrap');
      if (inner) inner.style.height = 'auto';
      modal.appendChild(box);
    }
    modal.appendChild(closeBtn);
    document.body.appendChild(modal);
  }

  async function actRefresh(tile, tileEl, btn) {
    if (btn.disabled) return;
    btn.disabled = true;
    btn.classList.add('spinning');
    try {
      const res = await fetch(`/api/dashboards/${DASH_ID}/tiles/${tile.tile_id}/refresh`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      let data = null;
      try { data = await res.json(); } catch (_) { data = null; }
      if (!res.ok || !data || data.ok !== true) {
        if (data && data.frozen) {
          if (data.reason === 'source_deleted') { tile.frozen = true; tile.frozen_reason = 'source_deleted'; }
          setTileBadge(tileEl, frozenText(data.reason));
        } else {
          setTileBadge(tileEl, _t('dash.refresh_failed', 'Refresh failed — showing saved version'));
          setTimeout(() => { if (!tile.frozen) setTileBadge(tileEl, null); }, 5000);
        }
        return false;
      }
      // Success — server already persisted the fresh snapshot; mirror it.
      if (data.tile) {
        Object.assign(tile, data.tile);
      } else if (tile.kind === 'chart') {
        tile.snapshot = { image_base64: data.image_base64, is_plotly: !!data.is_plotly };
      } else {
        tile.snapshot = { table: data.table };
        if (data.full_table_key) tile.full_table_key = data.full_table_key;
      }
      tile.frozen = false;
      tile.frozen_reason = null;
      renderTileBody(tileEl.querySelector('.pdc-tile-body'), tile);
      setTileBadge(tileEl, null);
      return true;
    } catch (e) {
      console.error('Tile refresh failed:', e);
      setTileBadge(tileEl, _t('dash.refresh_failed', 'Refresh failed — showing saved version'));
      return false;
    } finally {
      btn.classList.remove('spinning');
      btn.disabled = false;
    }
  }

  async function actRemove(tile, tileEl) {
    try {
      const res = await fetch(`/api/dashboards/${DASH_ID}/tiles/${tile.tile_id}/remove`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      const data = await res.json().catch(() => null);
      if (!res.ok || !data || !data.ok) throw new Error('remove failed');
      dash.tiles = (dash.tiles || []).filter(t => t.tile_id !== tile.tile_id);
      if (grid) grid.removeWidget(tileEl);
      updateEmptyState();
    } catch (e) {
      console.error('Remove tile failed:', e);
      showToast(_t('dash.remove_failed', 'Could not remove the tile'), true);
    }
  }

  // ── refresh pre-freeze (df keys the tile's code references must exist) ───
  async function loadChatDfKeys(chatId) {
    if (chatId in chatDfKeys) return chatDfKeys[chatId];
    try {
      const res = await fetch(`/api/chat/${chatId}/schema`);
      if (!res.ok) { chatDfKeys[chatId] = null; return null; }
      const data = await res.json();
      chatDfKeys[chatId] = {
        keys: new Set((data.files || []).map(f => f.file_name).filter(Boolean)),
        // DB tables the viewer's role does not cover (rows with allowed=false)
        // — their refresh buttons pre-freeze with the role tooltip.
        blocked: new Set((data.db_tables || [])
          .filter(t => t.allowed === false).map(t => t.df_key).filter(Boolean)),
      };
    } catch (_) {
      chatDfKeys[chatId] = null;
    }
    return chatDfKeys[chatId];
  }

  function extractDfKeys(code) {
    const keys = [];
    const re = /dfs\[\s*['"]([^'"]+)['"]\s*\]/g;
    let m;
    while ((m = re.exec(String(code || ''))) !== null) keys.push(m[1]);
    return keys;
  }

  async function applyRefreshPreFreeze(tile, refreshBtn) {
    if (!refreshBtn || !tile.code) return;
    const info = await loadChatDfKeys(tile.chat_id);
    if (!info) return;   // unknown → leave enabled
    const refs = extractDfKeys(tile.code);
    if (refs.some(k => !info.keys.has(k))) {
      refreshBtn.disabled = true;
      refreshBtn.title = _t('lab.refresh_frozen', "Data structure changed — this chart/table can't be refreshed.");
    } else if (refs.some(k => info.blocked.has(k))) {
      refreshBtn.disabled = true;
      refreshBtn.title = _t('dash.frozen_role_denied',
        "Your role doesn't include this data — showing saved result");
    }
  }

  // ── tile DOM ─────────────────────────────────────────────────────────────
  function toolbarBtn(cls, html, title, handler) {
    const b = document.createElement('button');
    b.className = cls;
    b.innerHTML = html;
    b.title = title;
    b.addEventListener('click', (e) => { e.stopPropagation(); handler(b); });
    return b;
  }

  function buildTileEl(tile) {
    const lay = tile.layout || { x: 0, y: 0, w: 6, h: 5 };
    const item = document.createElement('div');
    item.className = 'grid-stack-item';
    item.setAttribute('gs-id', tile.tile_id);
    item.setAttribute('gs-x', String(lay.x ?? 0));
    item.setAttribute('gs-y', String(lay.y ?? 0));
    item.setAttribute('gs-w', String(lay.w ?? 6));
    item.setAttribute('gs-h', String(lay.h ?? 5));

    const content = document.createElement('div');
    content.className = 'grid-stack-item-content pdc-tile';
    content.dataset.kind = tile.kind;

    const dragStrip = document.createElement('div');
    dragStrip.className = 'pdc-tile-drag';
    const titleSpan = document.createElement('span');
    titleSpan.className = 'pdc-tile-title';
    titleSpan.textContent = tile.title || '';
    dragStrip.appendChild(titleSpan);
    content.appendChild(dragStrip);

    const toolbar = document.createElement('div');
    toolbar.className = 'pdc-tile-toolbar';
    if (tile.description) {
      toolbar.appendChild(toolbarBtn('t-info', 'ℹ', _t('dash.tile_description', 'Show description'),
        () => openDescription(content, tile)));
    }
    const hasData = tile.kind === 'chart'
      ? !!tile.chart_data
      : !!(tile.full_table_key || ((tile.snapshot || {}).table || {}).rows);
    if (hasData) {
      toolbar.appendChild(toolbarBtn('t-data', '📊', _t('dash.show_data', 'Show data'),
        () => actShowData(tile)));
    }
    if (tile.code) {
      toolbar.appendChild(toolbarBtn('t-code', '⟨⟩', _t('dash.show_code', 'Show code'),
        () => actShowCode(tile)));
    }
    toolbar.appendChild(toolbarBtn('t-dl', '📥', _t('dash.download', 'Download'),
      (b) => actDownload(tile, b)));
    toolbar.appendChild(toolbarBtn('t-big', '🔍', _t('dash.view_larger', 'View larger'),
      () => actViewLarger(tile)));
    let refreshBtn = null;
    if (tile.code) {
      refreshBtn = toolbarBtn('t-refresh', REFRESH_SVG, _t('dash.refresh', 'Refresh with current data'),
        (b) => actRefresh(tile, item, b));
      toolbar.appendChild(refreshBtn);
    }
    if (isOwner) {
      toolbar.appendChild(toolbarBtn('t-remove', '✕', _t('dash.remove_tile', 'Remove from dashboard'),
        () => actRemove(tile, item)));
    }
    content.appendChild(toolbar);

    const body = document.createElement('div');
    body.className = 'pdc-tile-body';
    content.appendChild(body);
    item.appendChild(content);

    renderTileBody(body, tile);
    if (tile.frozen) setTileBadge(item, frozenText(tile.frozen_reason));
    if (refreshBtn) applyRefreshPreFreeze(tile, refreshBtn);
    return item;
  }

  // ── grid ─────────────────────────────────────────────────────────────────
  function initGrid() {
    const gridEl = document.getElementById('dashGrid');
    (dash.tiles || []).forEach(tile => gridEl.appendChild(buildTileEl(tile)));

    grid = GridStack.init({
      column: GRID_COLUMNS,
      cellHeight: 80,
      margin: 8,
      float: false,             // gravity packing, tiles never overlap
      animate: true,
      handle: '.pdc-tile-drag', // drag ONLY by the grab strip (iframe-safe)
      resizable: { handles: 'e,se,s,sw,w' },
      columnOpts: { breakpointForWindow: true, breakpoints: [{ w: 768, c: 1 }] },
      disableDrag: !isOwner,
      disableResize: !isOwner,
    }, '#dashGrid');

    if (isOwner) {
      grid.on('change', () => {
        clearTimeout(saveTimer);
        saveTimer = setTimeout(saveLayout, 800);
      });
    }
    // While dragging/resizing, Plotly iframes must not swallow mouse events.
    ['dragstart', 'resizestart'].forEach(ev =>
      grid.on(ev, () => document.body.classList.add('pdc-grid-moving')));
    ['dragstop', 'resizestop'].forEach(ev =>
      grid.on(ev, () => document.body.classList.remove('pdc-grid-moving')));
  }

  async function saveLayout() {
    if (!grid || !isOwner) return;
    // The 1-column narrow mode is presentational — saving it would destroy
    // the desktop layout.
    if (typeof grid.getColumn === 'function' && grid.getColumn() !== GRID_COLUMNS) return;
    let nodes = [];
    try { nodes = grid.save(false) || []; } catch (e) { return; }
    const tiles = nodes
      .filter(n => n && n.id)
      .map(n => ({ tile_id: n.id, x: n.x || 0, y: n.y || 0, w: n.w || 1, h: n.h || 1 }));
    if (!tiles.length) return;
    try {
      await fetch(`/api/dashboards/${DASH_ID}/layout`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tiles }),
      });
    } catch (e) {
      console.warn('Layout save failed:', e);
    }
  }

  function updateEmptyState() {
    const empty = document.getElementById('dashEmptyState');
    const gridScroll = document.querySelector('.dash-grid-scroll');
    const hasTiles = dash && (dash.tiles || []).length > 0;
    empty.classList.toggle('hidden', hasTiles);
    if (gridScroll) gridScroll.style.display = hasTiles ? '' : 'none';
  }

  // ── refresh all ──────────────────────────────────────────────────────────
  async function refreshAll() {
    const btn = document.getElementById('btnRefreshAll');
    const jobs = [];
    document.querySelectorAll('.grid-stack-item').forEach(el => {
      const id = el.getAttribute('gs-id');
      const tile = (dash.tiles || []).find(t => t.tile_id === id);
      const rBtn = el.querySelector('.t-refresh');
      if (tile && tile.code && rBtn && !rBtn.disabled) jobs.push({ tile, el, rBtn });
    });
    if (!jobs.length) return;
    btn.disabled = true;
    let okCount = 0;
    // Concurrency-2 queue: per-tile failures never abort the loop.
    const queue = jobs.slice();
    async function worker() {
      while (queue.length) {
        const job = queue.shift();
        const ok = await actRefresh(job.tile, job.el, job.rBtn);
        if (ok) okCount++;
      }
    }
    await Promise.all([worker(), worker()]);
    btn.disabled = false;
    showToast(_t('dash.refreshed_n', 'Refreshed {n} of {m} tiles')
      .replace('{n}', String(okCount)).replace('{m}', String(jobs.length)),
      okCount < jobs.length);
  }

  // ── top-bar actions ──────────────────────────────────────────────────────
  function openModal(id) { document.getElementById(id).classList.remove('hidden'); }
  function closeModal(id) { document.getElementById(id).classList.add('hidden'); }

  function wireTopBar() {
    document.getElementById('btnRefreshAll').addEventListener('click', refreshAll);

    // Rename (owner)
    const renameBtn = document.getElementById('btnRenameDash');
    if (isOwner) {
      renameBtn.classList.remove('hidden');
      renameBtn.addEventListener('click', () => {
        document.getElementById('renameDashInput').value = dash.name || '';
        openModal('renameDashModal');
        document.getElementById('renameDashInput').focus();
      });
      document.getElementById('btnSaveRenameDash').addEventListener('click', async () => {
        const name = document.getElementById('renameDashInput').value.trim();
        if (!name) return;
        try {
          const res = await fetch(`/api/dashboards/${DASH_ID}/rename`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
          });
          const data = await res.json().catch(() => null);
          if (!res.ok || !data || !data.ok) throw new Error('rename failed');
          dash.name = name;
          document.getElementById('dashTitle').textContent = name;
          document.title = `${name} - PowerDataChat`;
          closeModal('renameDashModal');
        } catch (e) {
          showToast(_t('dash.rename_failed', 'Rename failed'), true);
        }
      });
      document.getElementById('renameDashInput').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') document.getElementById('btnSaveRenameDash').click();
      });
      document.getElementById('closeRenameDash').addEventListener('click', () => closeModal('renameDashModal'));
      document.getElementById('btnCancelRenameDash').addEventListener('click', () => closeModal('renameDashModal'));
    }

    // Share (owner)
    const shareBtn = document.getElementById('btnShareDash');
    if (isOwner) {
      shareBtn.classList.remove('hidden');
      shareBtn.addEventListener('click', () => {
        document.getElementById('shareDashEmails').value = '';
        document.getElementById('shareDashComment').value = '';
        openModal('shareDashModal');
        document.getElementById('shareDashEmails').focus();
      });
      document.getElementById('btnSaveShareDash').addEventListener('click', async () => {
        const emails = document.getElementById('shareDashEmails').value.trim();
        if (!emails) {
          showToast(_t('share.need_email', 'Please enter at least one email address'), true);
          return;
        }
        const saveBtn = document.getElementById('btnSaveShareDash');
        saveBtn.disabled = true;
        try {
          const res = await fetch(`/api/dashboards/${DASH_ID}/share`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ emails, message: document.getElementById('shareDashComment').value.trim() }),
          });
          const data = await res.json().catch(() => null);
          if (!res.ok || !data || !data.ok) {
            throw new Error((data && data.error) || 'share failed');
          }
          closeModal('shareDashModal');
          showToast(_t('dash.shared_ok', 'Dashboard shared'));
        } catch (e) {
          showToast(String(e.message || e), true);
        } finally {
          saveBtn.disabled = false;
        }
      });
      document.getElementById('closeShareDash').addEventListener('click', () => closeModal('shareDashModal'));
      document.getElementById('btnCancelShareDash').addEventListener('click', () => closeModal('shareDashModal'));
    }

    // Delete: owner deletes the dashboard; a recipient removes it from their
    // own list only (the owner's copy is untouched).
    const delBtn = document.getElementById('btnDeleteDash');
    if (!isOwner) {
      delBtn.textContent = '✕ ' + _t('dash.remove_from_list', 'Remove from my list');
      const txt = document.getElementById('deleteDashText');
      txt.removeAttribute('data-i18n');
      txt.textContent = _t('dash.remove_from_list_confirm',
        "Remove this shared dashboard from your list? The owner's dashboard is not affected.");
    }
    delBtn.addEventListener('click', () => openModal('deleteDashModal'));
    document.getElementById('btnConfirmDeleteDash').addEventListener('click', async () => {
      try {
        const res = await fetch(`/api/dashboards/${DASH_ID}/delete`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
        });
        const data = await res.json().catch(() => null);
        if (!res.ok || !data || !data.ok) throw new Error('delete failed');
        window.location.href = '/lab';
      } catch (e) {
        closeModal('deleteDashModal');
        showToast(_t('dash.delete_failed', 'Delete failed'), true);
      }
    });
    document.getElementById('closeDeleteDash').addEventListener('click', () => closeModal('deleteDashModal'));
    document.getElementById('btnCancelDeleteDash').addEventListener('click', () => closeModal('deleteDashModal'));

    // Profile dropdown (minimal wiring — Change Password lives on /lab).
    const profBtn = document.getElementById('btnProfileIcon');
    const profDrop = document.getElementById('profileDropdown');
    if (profBtn && profDrop) {
      profBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        profDrop.classList.toggle('hidden');
      });
      document.addEventListener('click', (e) => {
        if (!e.target.closest('.profile-dropdown-container')) profDrop.classList.add('hidden');
      });
      const changePw = document.getElementById('btnChangePassword');
      if (changePw) changePw.style.display = 'none';
      const logout = document.getElementById('btnLogout');
      if (logout) {
        logout.addEventListener('click', async () => {
          try { await fetch('/auth/logout', { method: 'POST' }); } catch (_) {}
          window.location.href = '/';
        });
      }
    }
  }

  // ── init ─────────────────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', async () => {
    if (window.i18n) window.i18n.init();
    try {
      const authRes = await fetch('/auth/me');
      const authData = await authRes.json();
      if (!authData.authenticated) {
        window.location.href = '/';
        return;
      }
    } catch (_) { /* keep going — the doc fetch will 401 if truly logged out */ }

    let res;
    try {
      res = await fetch(`/api/dashboards/${DASH_ID}`);
    } catch (e) {
      window.location.href = '/lab';
      return;
    }
    if (!res.ok) {
      window.location.href = '/lab';
      return;
    }
    dash = await res.json();
    isOwner = dash.is_owner !== false;

    document.getElementById('dashTitle').textContent = dash.name || '';
    document.title = `${dash.name || 'Dashboard'} - PowerDataChat`;
    if (!isOwner) {
      const badge = document.getElementById('dashSharedBadge');
      badge.textContent = _t('dash.shared_by', 'Shared by') + ' ' + (dash.owner || '');
      badge.classList.remove('hidden');
      document.body.classList.add('dash-readonly');
    }

    wireTopBar();
    updateEmptyState();
    initGrid();
  });
})();
