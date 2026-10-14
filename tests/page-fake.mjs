// A page mounted on a fake document, fetch, clock and tab visibility, so page timing runs in node without a browser.

export function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

export class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.listeners = new Map();
    this.attrs = new Map();
    this.dataset = {};
    this.text = "";
  }
  append(...children) { this.children.push(...children); }
  replaceChildren() { this.children = []; this.text = ""; }
  setAttribute(name, value) { this.attrs.set(name, value); }
  addEventListener(name, fn) { this.listeners.set(name, fn); }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  focus() {}
  get childElementCount() { return this.children.length; }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return this.text + this.children.map((child) => child.textContent).join(""); }
}

export const response = (data, status = 200) => new Response(JSON.stringify(data), { status });
// A turn of the event loop drains promise continuations; no wall-clock waits or real timers.
export const settle = () => new Promise(setImmediate);

export function mount(t, render, fetcher) {
  const names = ["document", "Node", "sessionStorage", "fetch", "setTimeout", "clearTimeout", "setInterval", "clearInterval"];
  const originals = names.map((name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)]);
  // Tear the page down while the fakes are still installed, then restore the globals.
  let stop = () => {};
  t.after(() => {
    stop();
    for (const [name, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  });
  const timers = new Map();
  const intervals = new Map();
  const listeners = new Set();
  const calls = [];
  let next = 0, clock = 0;
  const document = {
    visibilityState: "visible",
    createElement: (tag) => new Element(tag),
    createElementNS: (namespace, tag) => new Element(tag),
    createTextNode: (text) => Object.assign(new Element("text"), { text }),
    addEventListener: (name, fn) => { listeners.add([name, fn]); },
    removeEventListener: (name, fn) => {
      for (const entry of listeners) if (entry[0] === name && entry[1] === fn) listeners.delete(entry);
    },
  };
  Object.assign(globalThis, {
    Node: Element,
    document,
    sessionStorage: { getItem: () => null },
    fetch: (url, options) => { calls.push({ url, method: options.method }); return fetcher(url, options); },
    setTimeout: (callback, delay) => { const id = ++next; timers.set(id, { callback, delay, at: clock + delay }); return id; },
    clearTimeout: (id) => timers.delete(id),
    setInterval: (callback, delay) => { const id = ++next; intervals.set(id, { callback, delay }); return id; },
    clearInterval: (id) => intervals.delete(id),
  });
  const main = new Element("main");
  stop = render(main);

  // Fire every timer that comes due within ms, in order, letting each callback's work finish first.
  async function advance(ms) {
    const end = clock + ms;
    for (;;) {
      await settle();
      const due = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      timers.delete(due[0]);
      clock = due[1].at;
      await due[1].callback();
    }
    clock = end;
    await settle();
  }
  async function visibility(state) {
    document.visibilityState = state;
    for (const [name, fn] of [...listeners]) if (name === "visibilitychange") fn({ type: name });
    await settle();
  }
  return { main, stop, timers, intervals, listeners, calls, advance, visibility };
}
