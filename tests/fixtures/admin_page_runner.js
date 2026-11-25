/* Run the admin page (a rendered templates/admin_data_sources.html) in node and
 * play one scenario (tests/test_mcp_admin_ui_structure.py).
 * argv: page.html, static dir, scenario file, options as JSON.
 *
 * The page's own <script> tags run in their order, so a script the page does
 * not load does not exist here either. The server is a stub that keeps the
 * token list; everything else answers {}. Prints one JSON object. */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const dom = require('./mini_dom.js');

const [page, staticDir, scenarioFile, optionsJson] = process.argv.slice(2);
const options = JSON.parse(optionsJson || '{}');
const errors = [];
process.on('unhandledRejection', (e) => errors.push(String((e && e.stack) || e)));

const doc = dom.loadDocument(fs.readFileSync(page, 'utf8'));

// ── the server ──────────────────────────────────────────────────────────
const TOKENS = '/api/admin/mcp/tokens';
const calls = [];
const server = {
  tokens: options.tokens || [],
  endpoint: options.endpoint === undefined ? 'https://pdc.corp.example/mcp' : options.endpoint,
  raw: 'pdcmcp_' + 'R'.repeat(43),
  create: null, revoke: null, list: null,      // [status, body] overrides
  hold: null,                                  // a promise a create answer waits for
};

function answer(status, data) {
  const body = JSON.stringify(data);
  return { status, ok: status >= 200 && status < 300,
    json: async () => JSON.parse(body), text: async () => body };
}

async function fetchStub(url, opts) {
  const o = opts || {};
  const method = (o.method || 'GET').toUpperCase();
  calls.push({ path: String(url), method, headers: o.headers || {}, body: o.body });
  if (url === TOKENS && method === 'GET') {
    return server.list ? answer(...server.list)
      : answer(200, { tokens: server.tokens, endpoint: server.endpoint });
  }
  if (url === TOKENS && method === 'POST') {
    if (server.hold) await server.hold;
    if (server.create) return answer(...server.create);
    const b = JSON.parse(o.body);
    const row = { id: String(calls.length).padStart(16, '0'), name: b.name, mode: b.mode,
      account: b.mode === 'shared_account' ? b.account : null,
      created_at: '2026-11-25T08:00:00+00:00', created_by: 'ladmin', last_used_at: null,
      revoked_at: null, revoked_by: null, active: true };
    server.tokens = [row, ...server.tokens];
    return answer(200, { token: row, raw_token: server.raw, endpoint: server.endpoint });
  }
  const m = /^\/api\/admin\/mcp\/tokens\/([^/]+)\/revoke$/.exec(url);
  if (m && method === 'POST') {
    if (server.revoke) return answer(...server.revoke);
    server.tokens = server.tokens.map((t) => (t.id === m[1] ? Object.assign({}, t,
      { active: false, revoked_at: '2026-11-25T09:00:00+00:00', revoked_by: 'ladmin' }) : t));
    return answer(200, { token: server.tokens.find((t) => t.id === m[1]) });
  }
  return answer(200, {});
}

// ── the window ──────────────────────────────────────────────────────────
const stored = options.lang ? { pdc_lang: options.lang } : {};
const storageWrites = [];
const storage = {
  getItem: (k) => (k in stored ? stored[k] : null),
  setItem: (k, v) => { storageWrites.push([k, String(v)]); stored[k] = String(v); },
  removeItem: (k) => { storageWrites.push([k, null]); delete stored[k]; },
};
const copied = [];
const forbidden = (name) => () => { throw new Error('window.' + name + ' used'); };
const win = new dom.Node();
Object.assign(win, {
  document: doc, localStorage: storage, sessionStorage: storage, fetch: fetchStub,
  location: { hash: options.hash || '', href: 'https://pdc.corp.example/admin/data_sources',
    pathname: '/admin/data_sources', search: '', origin: 'https://pdc.corp.example' },
  history: { replaceState: (a, b, u) => { win.location.hash = String(u); } },
  navigator: options.noClipboard ? {} : { clipboard: { writeText: async (t) => {
    if (options.clipboardFails) throw new Error('denied');
    copied.push(t);
  } } },
  console: { log: (...a) => errors.push('console.log ' + a), error: (...a) => errors.push('console.error ' + a),
    warn: (...a) => errors.push('console.warn ' + a), info: () => {}, debug: () => {} },
  Event: dom.Event, CustomEvent: dom.Event, AbortController, URL, URLSearchParams,
  // Nothing timed runs: a toast stays, so the test can read it.
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  requestAnimationFrame: () => 0, getComputedStyle: () => ({ fontSize: '16px' }),
  confirm: forbidden('confirm'), alert: forbidden('alert'), prompt: forbidden('prompt'),
});
win.window = win;
win.self = win;
const context = vm.createContext(win);

const loaded = [];
for (const s of doc.querySelectorAll('script')) {
  const src = s.getAttribute('src');
  if (!src) {
    vm.runInContext(s.textContent, context, { filename: 'inline.js' });
  } else {
    const clean = src.split('?')[0];
    if (!clean.startsWith('/static/')) throw new Error('foreign script ' + src);
    loaded.push(clean);
    vm.runInContext(fs.readFileSync(path.join(staticDir, clean.slice('/static/'.length)), 'utf8'),
      context, { filename: clean });
  }
}

// ── what a scenario works with ──────────────────────────────────────────
const h = {
  doc, win, calls, server, copied, errors, markupWrites: dom.markupWrites,
  $: (id) => {
    const e = doc.getElementById(id);
    if (!e) throw new Error('the page has no #' + id);
    return e;
  },
  text: (id) => h.$(id).textContent.trim(),
  visible: (x) => dom.isVisible(typeof x === 'string' ? h.$(x) : x),
  flush: async () => { for (let i = 0; i < 40; i += 1) await new Promise((r) => setImmediate(r)); },
  mcpCalls: () => calls.filter((c) => c.path.startsWith('/api/admin/mcp')),
  posts: () => h.mcpCalls().filter((c) => c.method === 'POST'),
  open: async (section) => {
    const b = doc.querySelector(`.adm-nav-item[data-section="${section}"]`);
    if (!b) throw new Error('no navigation button for ' + section);
    b.click();
    await h.flush();
  },
  set: (id, value) => {
    const e = h.$(id);
    e.value = value;
    e.dispatchEvent(new dom.Event('input'));
    e.dispatchEvent(new dom.Event('change'));
  },
  click: async (x) => { (typeof x === 'string' ? h.$(x) : x).click(); await h.flush(); },
  create: async (name, mode, account) => {
    h.set('mcpName', name);
    h.set('mcpMode', mode);
    if (account !== undefined) h.set('mcpAccount', account);
    await h.click('btnMcpCreate');
  },
  revokeButtons: () => h.$('mcpTokensList').querySelectorAll('.mcp-revoke'),
  // Everything on the page that could hold a value.
  everything: () => {
    const bits = [doc.documentElement.outerHTML];
    for (const e of doc.walk()) if (['input', 'textarea'].includes(e.localName)) bits.push(e.value);
    return bits.join('\n');
  },
  // The visible texts of an element, its title / placeholder / aria-label texts included.
  texts: (el) => {
    const out = [];
    (function go(n) {
      for (const a of ['title', 'placeholder', 'aria-label']) if (n.getAttribute(a)) out.push(n.getAttribute(a));
      for (const c of n.childNodes) {
        if (c instanceof dom.Text) { if (c.data.trim()) out.push(c.data.trim()); } else if (dom.isVisible(c)) go(c);
      }
    }(el));
    return out;
  },
  toasts: () => doc.querySelectorAll('.adm-toast').map((t) => t.textContent),
};

(async () => {
  let result = null;
  let failed = null;
  try {
    doc.readyState = 'interactive';
    doc.dispatchEvent(new dom.Event('DOMContentLoaded'));
    await h.flush();
    const scenario = vm.runInThisContext(
      '(async (h) => {' + fs.readFileSync(scenarioFile, 'utf8') + '\n})', { filename: 'scenario.js' });
    result = await scenario(h);
    await h.flush();
  } catch (e) {
    failed = String((e && e.stack) || e);
  }
  const out = JSON.stringify({ result: result === undefined ? null : result, failed, errors, loaded,
    storageWrites, markupWrites: dom.markupWrites, docLang: doc.documentElement.lang });
  process.stdout.write(out, () => process.exit(0));
})();
