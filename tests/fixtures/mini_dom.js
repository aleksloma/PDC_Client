/* A small DOM for tests that run a page script in node (no browser, no
 * dependency). Enough for templates/admin_data_sources.html: parsing, the
 * selectors the page uses, events that bubble, form values. Every markup write
 * (innerHTML, outerHTML, insertAdjacentHTML, document.write) is recorded in
 * `markupWrites`. Not a browser: no layout, no CSS.
 */
'use strict';

const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta',
  'source', 'track', 'wbr']);
const RAW = new Set(['script', 'style', 'textarea', 'title']);
const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0',
  middot: '·', mdash: '—', ndash: '–', hellip: '…', times: '×', rarr: '→', larr: '←' };

function decode(s) {
  return s.replace(/&(#x?[0-9a-f]+|[a-z]+);/gi, (m, e) => {
    if (e[0] === '#') {
      const n = /^#x/i.test(e) ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
      return String.fromCodePoint(n);
    }
    return Object.prototype.hasOwnProperty.call(ENTITIES, e) ? ENTITIES[e] : m;
  });
}

function encode(s) {
  return String(s).replace(/[&<>"]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

const markupWrites = [];

class Event {
  constructor(type, init) {
    Object.assign(this, { bubbles: true, defaultPrevented: false }, init || {});
    this.type = type;
  }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() { this._stopped = true; }
  stopImmediatePropagation() { this._stopped = true; }
}

class Node {
  constructor() { this.parentNode = null; this.childNodes = []; this._listeners = {}; }
  get parentElement() { return this.parentNode instanceof Element ? this.parentNode : null; }
  get children() { return this.childNodes.filter((c) => c instanceof Element); }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get firstElementChild() { return this.children[0] || null; }
  get childElementCount() { return this.children.length; }
  get nextSibling() {
    const all = this.parentNode ? this.parentNode.childNodes : [];
    return all[all.indexOf(this) + 1] || null;
  }
  get nextElementSibling() {
    const all = this.parentNode ? this.parentNode.children : [];
    return all[all.indexOf(this) + 1] || null;
  }
  get isConnected() {
    let n = this;
    while (n.parentNode) n = n.parentNode;
    return n instanceof Document;
  }
  _insert(node, at) {
    if (typeof node !== 'object' || node === null) node = new Text(String(node));
    if (node instanceof Fragment) {
      node.childNodes.slice().forEach((c, i) => this._insert(c, at + i));
      return node;
    }
    if (node.parentNode) node.remove();
    node.parentNode = this;
    this.childNodes.splice(at, 0, node);
    return node;
  }
  appendChild(n) { return this._insert(n, this.childNodes.length); }
  append(...ns) { ns.forEach((n) => this.appendChild(n)); }
  prepend(...ns) { ns.forEach((n, i) => this._insert(n, i)); }
  insertBefore(n, ref) {
    return this._insert(n, ref ? this.childNodes.indexOf(ref) : this.childNodes.length);
  }
  before(...ns) { ns.forEach((n) => this.parentNode.insertBefore(n, this)); }
  after(...ns) { ns.forEach((n) => this.parentNode.insertBefore(n, this.nextSibling)); }
  removeChild(n) { n.remove(); return n; }
  remove() {
    if (!this.parentNode) return;
    const i = this.parentNode.childNodes.indexOf(this);
    if (i >= 0) this.parentNode.childNodes.splice(i, 1);
    this.parentNode = null;
  }
  replaceChildren(...ns) {
    this.childNodes.slice().forEach((c) => c.remove());
    this.append(...ns);
  }
  replaceWith(...ns) { this.before(...ns); this.remove(); }
  contains(n) {
    for (; n; n = n.parentNode) if (n === this) return true;
    return false;
  }
  addEventListener(type, fn, opts) {
    (this._listeners[type] = this._listeners[type] || []).push({ fn, once: !!(opts && opts.once) });
  }
  removeEventListener(type, fn) {
    this._listeners[type] = (this._listeners[type] || []).filter((l) => l.fn !== fn);
  }
  dispatchEvent(ev) {
    ev.target = ev.target || this;
    for (let n = this; n && !ev._stopped; n = n.parentNode) {
      ev.currentTarget = n;
      if (typeof n['on' + ev.type] === 'function') n['on' + ev.type](ev);
      for (const l of (n._listeners[ev.type] || []).slice()) {
        if (l.once) n.removeEventListener(ev.type, l.fn);
        l.fn.call(n, ev);
      }
      if (ev.bubbles === false) break;
    }
    return !ev.defaultPrevented;
  }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(''); }
  set textContent(v) {
    this.childNodes.slice().forEach((c) => c.remove());
    if (v != null && String(v) !== '') this.appendChild(new Text(String(v)));
  }
  *walk() {
    for (const c of this.childNodes) {
      if (c instanceof Element) { yield c; yield* c.walk(); }
    }
  }
  querySelectorAll(sel) { return [...this.walk()].filter((e) => matches(e, sel)); }
  querySelector(sel) {
    for (const e of this.walk()) if (matches(e, sel)) return e;
    return null;
  }
  getElementsByClassName(c) { return this.querySelectorAll('.' + c); }
  getElementsByTagName(t) { return this.querySelectorAll(t); }
}

class Text extends Node {
  constructor(s) { super(); this.data = s; this.nodeType = 3; }
  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
  cloneNode() { return new Text(this.data); }
}

class Fragment extends Node {
  constructor() { super(); this.nodeType = 11; }
}

function dash(k) { return 'data-' + String(k).replace(/[A-Z]/g, (c) => '-' + c.toLowerCase()); }

class Element extends Node {
  constructor(tag) {
    super();
    this.nodeType = 1;
    this.localName = tag.toLowerCase();
    this.attrs = {};
    this.style = { setProperty(k, v) { this[k] = v; }, removeProperty(k) { delete this[k]; } };
    const el = this;
    this.dataset = new Proxy({}, {
      get: (_, k) => (typeof k === 'string' && el.hasAttribute(dash(k))
        ? el.getAttribute(dash(k)) : undefined),
      set: (_, k, v) => { el.setAttribute(dash(k), v); return true; },
      has: (_, k) => el.hasAttribute(dash(k)),
      deleteProperty: (_, k) => { el.removeAttribute(dash(k)); return true; },
    });
    const list = () => (el.getAttribute('class') || '').split(/\s+/).filter(Boolean);
    this.classList = {
      contains: (c) => list().includes(c),
      add: (...cs) => el.setAttribute('class', [...new Set([...list(), ...cs])].join(' ')),
      remove: (...cs) => el.setAttribute('class', list().filter((c) => !cs.includes(c)).join(' ')),
      toggle: (c, force) => {
        const want = force === undefined ? !list().includes(c) : !!force;
        if (want) el.classList.add(c); else el.classList.remove(c);
        return want;
      },
    };
  }
  get tagName() { return this.localName.toUpperCase(); }
  getAttribute(k) {
    return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null;
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  removeAttribute(k) { delete this.attrs[k]; }
  hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); }
  toggleAttribute(k, f) {
    if (f === undefined ? !this.hasAttribute(k) : f) this.setAttribute(k, '');
    else this.removeAttribute(k);
  }
  matches(sel) { return matches(this, sel); }
  closest(sel) {
    for (let e = this; e instanceof Element; e = e.parentNode) if (matches(e, sel)) return e;
    return null;
  }
  get innerHTML() { return this.childNodes.map(serialize).join(''); }
  set innerHTML(v) {
    const html = v == null ? '' : String(v);
    markupWrites.push(html);
    this.childNodes.slice().forEach((c) => c.remove());
    parseInto(this, html);
  }
  get outerHTML() { return serialize(this); }
  set outerHTML(v) { markupWrites.push(String(v)); throw new Error('outerHTML write'); }
  insertAdjacentHTML(where, v) {
    markupWrites.push(String(v));
    const f = new Fragment();
    parseInto(f, String(v));
    this.insertAdjacentElement(where, f);
  }
  insertAdjacentElement(where, n) {
    if (where === 'beforebegin') this.parentNode.insertBefore(n, this);
    else if (where === 'afterbegin') this._insert(n, 0);
    else if (where === 'beforeend') this.appendChild(n);
    else this.parentNode.insertBefore(n, this.nextSibling);
    return n;
  }
  get innerText() { return this.textContent; }
  set innerText(v) { this.textContent = v; }
  get options() { return this.querySelectorAll('option'); }
  get selectedIndex() {
    const all = this.options;
    const i = all.findIndex((o) => o._selected);
    if (i >= 0) return i;
    const d = all.findIndex((o) => o.hasAttribute('selected'));
    return d >= 0 ? d : (all.length ? 0 : -1);
  }
  set selectedIndex(i) { this.options.forEach((o, n) => { o._selected = n === i; }); }
  get selected() { return !!this._selected; }
  set selected(v) {
    const sel = this.closest('select');
    if (sel && v) sel.options.forEach((o) => { o._selected = false; });
    this._selected = !!v;
  }
  get value() {
    if (this.localName === 'select') {
      const o = this.options[this.selectedIndex];
      return o ? o.value : '';
    }
    if (this.localName === 'option') {
      return this.hasAttribute('value') ? this.getAttribute('value') : this.textContent;
    }
    if (this._value !== undefined) return this._value;
    return this.localName === 'textarea' ? this.textContent : (this.getAttribute('value') || '');
  }
  set value(v) {
    if (this.localName === 'select') {
      this.options.forEach((o) => { o._selected = o.value === String(v); });
    } else if (this.localName === 'option') {
      this.setAttribute('value', v);
    } else {
      this._value = v == null ? '' : String(v);
    }
  }
  get checked() { return this._checked !== undefined ? this._checked : this.hasAttribute('checked'); }
  set checked(v) {
    this._checked = !!v;
    if (v && this.type === 'radio' && this.name) {
      let root = this;
      while (root.parentNode) root = root.parentNode;
      root.querySelectorAll(`input[name="${this.name}"]`)
        .forEach((o) => { if (o !== this) o._checked = false; });
    }
  }
  click() {
    if (this.disabled) return;
    if (this.localName === 'input' && ['radio', 'checkbox'].includes(this.type)) {
      this.checked = this.type === 'radio' ? true : !this.checked;
      this.dispatchEvent(new Event('change'));
    }
    this.dispatchEvent(new Event('click'));
  }
  focus() {}
  blur() {}
  select() {}
  setSelectionRange() {}
  scrollIntoView() {}
  getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
  get offsetWidth() { return 0; }
  get offsetHeight() { return 0; }
  insertRow() { return this.appendChild(new Element('tr')); }
  insertCell() { return this.appendChild(new Element('td')); }
  cloneNode(deep) {
    const e = new Element(this.localName);
    Object.assign(e.attrs, this.attrs);
    if (deep) this.childNodes.forEach((c) => e.appendChild(c.cloneNode(true)));
    return e;
  }
}

[['id', 'id'], ['className', 'class'], ['title', 'title'], ['placeholder', 'placeholder'],
  ['name', 'name'], ['href', 'href'], ['src', 'src'], ['htmlFor', 'for'], ['type', 'type'],
  ['lang', 'lang'], ['ariaLabel', 'aria-label'], ['colSpan', 'colspan'], ['rel', 'rel'],
  ['target', 'target'], ['role', 'role']].forEach(([prop, attr]) => {
  Object.defineProperty(Element.prototype, prop, {
    get() { return this.getAttribute(attr) || ''; },
    set(v) { this.setAttribute(attr, v); },
  });
});
[['hidden', 'hidden'], ['disabled', 'disabled'], ['readOnly', 'readonly'],
  ['required', 'required']].forEach(([prop, attr]) => {
  Object.defineProperty(Element.prototype, prop, {
    get() { return this.hasAttribute(attr); },
    set(v) { if (v) this.setAttribute(attr, ''); else this.removeAttribute(attr); },
  });
});

class Document extends Node {
  constructor() {
    super();
    this.nodeType = 9;
    this.readyState = 'loading';
    this.title = '';
    this.execCalls = [];
  }
  get documentElement() { return this.children[0]; }
  get body() { return this.querySelector('body'); }
  get head() { return this.querySelector('head'); }
  get activeElement() { return this.body; }
  getElementById(id) {
    for (const e of this.walk()) if (e.getAttribute('id') === id) return e;
    return null;
  }
  createElement(t) { return new Element(t); }
  createTextNode(s) { return new Text(String(s)); }
  createDocumentFragment() { return new Fragment(); }
  write(s) { markupWrites.push(String(s)); }
  execCommand(command) {
    const ta = this.querySelectorAll('textarea').pop();
    this.execCalls.push([command, ta ? ta.value : null]);
    return true;
  }
}

function serialize(n) {
  if (n instanceof Text) return encode(n.data);
  const at = Object.entries(n.attrs).map(([k, v]) => ` ${k}="${encode(v)}"`).join('');
  if (VOID.has(n.localName)) return `<${n.localName}${at}>`;
  return `<${n.localName}${at}>${n.childNodes.map(serialize).join('')}</${n.localName}>`;
}

// One tag at a time: comment, doctype, closing tag, opening tag.
const ATTR = '[^\\s=>\\/"\']+(?:\\s*=\\s*(?:"[^"]*"|\'[^\']*\'|[^\\s>]+))?';
const TAG = '<!--[\\s\\S]*?-->|<!doctype[^>]*>|<\\/([a-zA-Z][\\w-]*)\\s*>'
  + '|<([a-zA-Z][\\w-]*)((?:\\s+' + ATTR + ')*)\\s*\\/?>';

function parseInto(root, html) {
  const re = new RegExp(TAG, 'gi');
  let cur = root;
  let pos = 0;
  let m;
  const text = (s) => { if (s) cur.appendChild(new Text(decode(s))); };
  while ((m = re.exec(html))) {
    text(html.slice(pos, m.index));
    pos = re.lastIndex;
    if (m[1]) {
      const tag = m[1].toLowerCase();
      let e = cur;
      while (e !== root && e.localName !== tag) e = e.parentNode;
      if (e !== root) cur = e.parentNode;
    } else if (m[2]) {
      const tag = m[2].toLowerCase();
      const el = new Element(tag);
      const are = /([^\s=>\/"']+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
      let a;
      while ((a = are.exec(m[3] || ''))) {
        const v = [a[2], a[3], a[4]].find((x) => x !== undefined);
        el.attrs[a[1].toLowerCase()] = decode(v || '');
      }
      cur.appendChild(el);
      if (RAW.has(tag)) {
        const end = html.toLowerCase().indexOf('</' + tag, pos);
        const body = html.slice(pos, end < 0 ? html.length : end);
        const literal = tag === 'script' || tag === 'style';
        if (body) el.appendChild(new Text(literal ? body : decode(body)));
        pos = end < 0 ? html.length : html.indexOf('>', end) + 1;
        re.lastIndex = pos;
      } else if (!VOID.has(tag)) {
        cur = el;
      }
    }
  }
  text(html.slice(pos));
}

// Selectors: tag, #id, .class, [attr], [attr="v"], :checked, :disabled, :not(),
// descendant and child combinators, comma lists. Anything else throws.
function splitTop(sel, sep) {
  const out = [];
  let depth = 0;
  let quote = '';
  let cur = '';
  for (const c of sel) {
    if (quote) {
      if (c === quote) quote = '';
      cur += c;
    } else if (c === '"' || c === "'") {
      quote = c;
      cur += c;
    } else {
      if (c === '(' || c === '[') depth += 1;
      if (c === ')' || c === ']') depth -= 1;
      if (c === sep && depth === 0) { out.push(cur); cur = ''; } else cur += c;
    }
  }
  out.push(cur);
  return out.map((s) => s.trim()).filter(Boolean);
}

function matches(el, sel) {
  return splitTop(sel, ',').some((s) => {
    const parts = splitTop(s.replace(/\s*>\s*/g, ' > '), ' ');
    return matchParts(el, parts, parts.length - 1);
  });
}

function matchParts(el, parts, i) {
  if (!matchCompound(el, parts[i])) return false;
  if (i === 0) return true;
  if (parts[i - 1] === '>') {
    return el.parentNode instanceof Element && matchParts(el.parentNode, parts, i - 2);
  }
  for (let p = el.parentNode; p instanceof Element; p = p.parentNode) {
    if (matchParts(p, parts, i - 1)) return true;
  }
  return false;
}

function matchCompound(el, s) {
  const re = new RegExp('^([a-zA-Z][\\w-]*|\\*)|#([\\w-]+)|\\.([\\w-]+)'
    + '|\\[([\\w-]+)(?:([\\^$*]?=)["\']?([^"\'\\]]*)["\']?)?\\]'
    + '|:not\\(([^)]*)\\)|:(checked|disabled)', 'g');
  let last = 0;
  let ok = true;
  let m;
  while ((m = re.exec(s))) {
    if (m.index !== last) break;
    last = re.lastIndex;
    if (m[1] !== undefined) ok = ok && (m[1] === '*' || m[1].toLowerCase() === el.localName);
    else if (m[2]) ok = ok && el.getAttribute('id') === m[2];
    else if (m[3]) ok = ok && el.classList.contains(m[3]);
    else if (m[4]) {
      const v = el.getAttribute(m[4]);
      ok = ok && v !== null
        && (m[5] === undefined || (m[5] === '=' && v === m[6])
          || (m[5] === '^=' && v.startsWith(m[6])) || (m[5] === '$=' && v.endsWith(m[6]))
          || (m[5] === '*=' && v.includes(m[6])));
    } else if (m[7] !== undefined) ok = ok && !matchCompound(el, m[7].trim());
    else if (m[8] === 'checked') ok = ok && el.checked;
    else if (m[8] === 'disabled') ok = ok && el.disabled;
  }
  if (last !== s.length) throw new Error('mini_dom: selector not supported: ' + s);
  return ok;
}

function isVisible(el) {
  if (!el || !el.isConnected) return false;
  for (let e = el; e instanceof Element; e = e.parentNode) {
    if (e.hidden || e.classList.contains('hidden') || e.style.display === 'none') return false;
  }
  return true;
}

function loadDocument(html) {
  const doc = new Document();
  parseInto(doc, html);
  doc.childNodes.slice().forEach((c) => { if (c instanceof Text) c.remove(); });
  return doc;
}

module.exports = { loadDocument, Node, Element, Text, Event, isVisible, markupWrites };
