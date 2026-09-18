// A DOM small enough to run the chat panel's own JavaScript.
//
// The panel is the product's face and had no tests at all, so a mistake in it
// was only ever found by opening Fusion and looking. This runs the REAL script
// out of index.html against a stub, so its behaviour can be asserted the way
// everything else here is: by driving it and checking what happened.
//
// Deliberately not a full DOM. It implements what the panel actually touches,
// and throws on anything else, so the stub can never quietly diverge from the
// browser by silently accepting a call the real thing would have refused.

class ClassList {
  constructor() { this.set = new Set(); }
  add(...c) { c.forEach((x) => this.set.add(x)); }
  remove(...c) { c.forEach((x) => this.set.delete(x)); }
  toggle(c, on) { if (on === undefined) { this.set.has(c) ? this.set.delete(c) : this.set.add(c); } else if (on) { this.set.add(c); } else { this.set.delete(c); } }
  contains(c) { return this.set.has(c); }
  get value() { return [...this.set].join(' '); }
}

class El {
  constructor(tag) {
    this.tagName = (tag || 'div').toUpperCase();
    this.children = [];
    this.classList = new ClassList();
    this._text = '';
    this.hidden = false;
    this.disabled = false;
    this.style = {};
    this.dataset = {};
    this.parentElement = null;
    this._listeners = {};
  }
  set className(v) { this.classList.set = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return this.classList.value; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() {
    return this.children.length
      ? this.children.map((c) => (typeof c === 'string' ? c : c.textContent)).join('')
      : this._text;
  }
  set innerHTML(v) { if (v !== '') throw new Error('innerHTML is only used to clear'); this.children = []; this._text = ''; }
  append(...nodes) { nodes.forEach((n) => { if (n instanceof El) n.parentElement = this; this.children.push(n); }); }
  appendChild(n) { this.append(n); return n; }
  remove() { const p = this.parentElement; if (p) p.children = p.children.filter((c) => c !== this); }
  addEventListener(kind, fn) { (this._listeners[kind] ||= []).push(fn); }
  focus() {}
  scrollTo() {}
  get scrollHeight() { return 100; }
  get scrollTop() { return 0; }
  set scrollTop(_v) {}
  get clientHeight() { return 100; }
  // Every element the panel looks up by id is pre-created, so a query for one
  // it never declared is a bug worth surfacing rather than a silent null.
  querySelector() { return null; }
}

function buildDocument(ids, hiddenIds) {
  const byId = new Map();
  ids.forEach((id) => byId.set(id, new El('div')));
  // Elements the markup declares `hidden` start hidden, as they do in a browser.
  // Without this every panel is open at load, and a toggle button tested here
  // reads its own first press as a second one.
  (hiddenIds || []).forEach((id) => { if (byId.has(id)) byId.get(id).hidden = true; });
  return {
    _byId: byId,
    getElementById(id) {
      if (!byId.has(id)) throw new Error(`panel asked for #${id}, which its markup does not declare`);
      return byId.get(id);
    },
    createElement(tag) { return new El(tag); },
    // Recorded, not discarded: drag-and-drop lives entirely on document-level
    // listeners, so a test has to be able to fire them.
    _docListeners: {},
    addEventListener(kind, fn) { (this._docListeners[kind] ||= []).push(fn); },
    body: new El('body'),
    documentElement: new El('html'),
  };
}

// Captures what the panel tried to send, so a test can assert on requests
// without a server.
function makeHarness(ids, hiddenIds) {
  const sent = [];
  const doc = buildDocument(ids, hiddenIds);
  const listeners = {};
  const sse = {
    set onmessage(fn) { listeners.message = fn; },
    set onopen(fn) { listeners.open = fn; },
    set onerror(fn) { listeners.error = fn; },
  };
  // setInterval is stubbed out rather than left undefined: the panel may start
  // a ticker, and a test must not depend on wall-clock time to pass.
  
  const sandbox = {
    document: doc,
    window: { addEventListener() {}, matchMedia: () => ({ matches: false }) },
    EventSource: function () { return sse; },
    fetch: (url, opts) => {
      sent.push({ url, opts });
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
    },
    setTimeout: (fn) => { void fn; return 0; },
    setInterval: (fn) => { void fn; return 0; },
    clearInterval() {},
    clearTimeout() {},
    requestAnimationFrame: (fn) => { fn(); return 0; },
    console,
    FileReader: function () {},
    Image: function () {},
  };
  sandbox.globalThis = sandbox;
  return {
    sandbox, doc, sent,
    deliver: (ev) => listeners.message({ data: JSON.stringify(ev) }),
    // Fire a document-level event and report whether the panel asked the host
    // to stop its default handling — which is the whole game for a drop.
    fire: (kind, event) => {
      let prevented = false;
      const e = { preventDefault: () => { prevented = true; }, ...event };
      (doc._docListeners[kind] || []).forEach((fn) => fn(e));
      return prevented;
    },
    drop: () => listeners.error({}),
    reconnect: () => listeners.open({}),
  };
}

module.exports = { makeHarness, El };
