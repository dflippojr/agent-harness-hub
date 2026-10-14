// Tiny DOM builder. Text always goes in as text nodes, never parsed as HTML, so daemon data cannot inject markup.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (name === "class") el.className = value;
    else if (name.startsWith("on") && typeof value === "function") el.addEventListener(name.slice(2), value);
    else if (value === true) el.setAttribute(name, "");
    else el.setAttribute(name, String(value));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children) {
    if (child === undefined || child === null || child === false) continue;
    if (Array.isArray(child)) append(el, child);
    else el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function clear(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

export function when(seconds) {
  if (typeof seconds !== "number") return "—";
  return new Date(seconds * 1000).toLocaleString();
}

// Host commands a Host command button opened, by key: held for this page load so a poll's redraw keeps them open.
const openCommands = new Set();
let hostCommandCount = 0;

// A command the owner runs on the daemon host, with a copy button. A step only the host can take always shows.
// { parity: true } marks the CLI equivalent of the Hub action just above it (CLI parity, decision 3 of #65): it stays
// hidden behind a "Host command" button until pressed, or until the owner turns on Show host commands
// (body.show-host-commands). text may then be a list of commands that one button reveals together; key names the
// action when its command text changes with the form (by default, the text itself).
export function command(text, { parity = false, key } = {}) {
  if (!parity) return commandLine(text);
  const lines = [text].flat();
  const id = `host-command-${++hostCommandCount}`;
  const name = key ?? lines.join("\n");
  let open = openCommands.has(name);
  const button = h("button", { type: "button", class: "btn host-command-toggle", "aria-expanded": String(open),
    "aria-controls": id }, lines.length > 1 ? "Host commands" : "Host command");
  const box = h("div", { class: open ? "host-command open" : "host-command" }, button,
    h("div", { class: "host-command-lines", id }, lines.map(commandLine)));
  // Each button toggles what it shows itself: the same command can sit under two controls at once (a card's Deny and
  // its confirmation), and opening one must not make the other's next press a close.
  button.addEventListener("click", () => {
    open = !open;
    if (open) openCommands.add(name);
    else openCommands.delete(name);
    box.className = open ? "host-command open" : "host-command";
    button.setAttribute("aria-expanded", String(open));
  });
  return box;
}

function commandLine(text) {
  const copy = h("button", { type: "button", class: "btn ghost copy", "aria-label": `Copy: ${text}` }, "Copy");
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      copy.textContent = "Copied";
    } catch {
      copy.textContent = "Select and copy";
    }
    setTimeout(() => { copy.textContent = "Copy"; }, 2000);
  });
  return h("div", { class: "command" }, h("code", {}, text), copy);
}

// Daemon disclosure text marks key phrases as **bold**. Only that pair becomes <strong>, built as DOM nodes; every
// other character, markup included, stays text.
export function emphasis(text) {
  return String(text).split(/\*\*([^*]+)\*\*/).map((part, i) => i % 2 ? h("strong", {}, part) : part).filter(Boolean);
}

// Bring a message the owner must read (a refused action) into view and move focus to it, so neither the scroll
// position nor a re-render that disabled the clicked control leaves it unseen with focus on <body>.
export function reveal(el) {
  el.setAttribute("tabindex", "-1");
  el.scrollIntoView({ block: "center" });
  el.focus({ preventScroll: true });
}

// Status labels from the Stables set, with the pill tone each one takes. A label not listed here gets a plain pill.
export const STATUS_TONES = Object.freeze({
  "Needs approval": "warn",
  "Needs confirmation": "warn",
  "Approved": "run",
  "Waiting for the app": "run",
  "Paired": "ok",
  "Active": "ok",
  "Running": "ok",
  "Revoked": "bad",
  "Elevated": "bad",
  "Not answering": "bad",
  "Never used": "muted",
  "Off": "muted",
  "Expired": "muted",
  "Denied": "muted",
});

// A status pill. tone is run, ok, warn, bad or muted; left out, it comes from the Stables label set.
export function pill(label, tone = STATUS_TONES[label], attrs = {}) {
  return h("span", { ...attrs, class: tone ? `pill ${tone}` : "pill" }, label);
}

// An uppercase section header with a count badge. A warn count marks items that need the owner.
export function sectionHead(title, count, { level = 2, tone, id } = {}) {
  return h(`h${level}`, { class: "section-head", id }, title,
    count === undefined || count === null ? null
      : h("span", { class: tone ? `count ${tone}` : "count" }, String(count)));
}

// The banner under a page head. bad is an alert; warn and info are status messages. action is an optional button or
// link node; lead is an optional icon name drawn before the text.
export function banner(tone, title, detail, action, { lead } = {}) {
  return h("div", { class: `banner ${tone}`, role: tone === "bad" ? "alert" : "status" },
    lead ? icon(lead, "icon banner-icon") : null,
    h("span", { class: "banner-text" }, title, detail ? h("span", { class: "banner-detail" }, detail) : null),
    action);
}

// A labelled on/off switch: a real checkbox with role=switch, so keyboards and screen readers treat it as one.
export function toggle(label, { checked = false, disabled = false, onchange } = {}) {
  const input = h("input", { type: "checkbox", role: "switch", disabled });
  input.checked = checked;
  if (onchange) input.addEventListener("change", () => onchange(input.checked));
  return h("label", { class: "switch" }, h("span", { class: "switch-label" }, label),
    h("span", { class: "switch-track" }, input, h("i", { "aria-hidden": "true" })));
}

// A table with uppercase column heads, in a box that scrolls sideways on a narrow screen. Each row is an array of
// cells (text or nodes), or a <tr> built by the caller.
export function table(caption, headings, rows) {
  return h("div", { class: "table-wrap" }, h("table", { class: "table" },
    caption ? h("caption", { class: "visually-hidden" }, caption) : null,
    h("thead", {}, h("tr", {}, headings.map((heading) => h("th", { scope: "col" }, heading)))),
    h("tbody", {}, rows.map((row) => Array.isArray(row) ? h("tr", {}, row.map((cell) => h("td", {}, cell))) : row))));
}

// Whole seconds as m:ss, or h:mm:ss from an hour.
export function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const pad = (n) => String(n).padStart(2, "0");
  const hours = Math.floor(s / 3600);
  const minutes = Math.floor(s / 60) % 60;
  return hours ? `${hours}:${pad(minutes)}:${pad(s % 60)}` : `${minutes}:${pad(s % 60)}`;
}

// A chip with the time left on a code or slot, warn in the last minute (or the last warnBelow seconds); none left
// reads Expired. The caller re-renders it, so this adds no timers.
export function countdown(secondsLeft, { warnBelow = 60 } = {}) {
  if (typeof secondsLeft !== "number" || !Number.isFinite(secondsLeft)) return null;
  if (secondsLeft <= 0) return h("span", { class: "countdown" }, icon("clock"), "Expired");
  return h("span", { class: secondsLeft < warnBelow ? "countdown warn" : "countdown" }, icon("clock"),
    `${clock(secondsLeft)} left`);
}

// Irreversible actions, set apart in a red-outlined box under their own heading.
export function dangerZone(title, text, ...actions) {
  return h("section", { class: "danger-zone" }, h("h3", {}, title), text ? h("p", {}, text) : null, actions);
}

// Stroke icons from the design canvas, as [element, attributes] pairs on a 24px grid.
const ICONS = {
  overview: [["path", { d: "M4 15a8 8 0 1 1 16 0" }], ["path", { d: "m12 15 4-5" }], ["path", { d: "M4 19h16" }]],
  pairing: [["path", { d: "M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1" }], ["path", { d: "M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1" }]],
  apps: [4, 14].flatMap((y) => [4, 14].map((x) => ["rect", { x, y, width: 6, height: 6, rx: 1 }])),
  spend: [["path", { d: "M4 20V4M4 20h16" }], ["path", { d: "M8 16v-4M12 16V8M16 16v-6" }]],
  marketplace: [["path", { d: "M5 8h14l-1 12H6z" }], ["path", { d: "M9 8a3 3 0 0 1 6 0" }]],
  settings: [["path", { d: "M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1" }], ["circle", { cx: 15, cy: 6, r: 2 }],
    ["circle", { cx: 9, cy: 12, r: 2 }], ["circle", { cx: 17, cy: 18, r: 2 }]],
  modules: [["path", { d: "M12 3 3 8l9 5 9-5z" }], ["path", { d: "M3 8v8l9 5 9-5V8" }], ["path", { d: "M12 13v8" }]],
  admin: [["circle", { cx: 9, cy: 8, r: 3.5 }], ["path", { d: "M2 20a7 7 0 0 1 14 0" }],
    ["path", { d: "M16 4.5a3.5 3.5 0 0 1 0 7M22 20a7 7 0 0 0-5-6.7" }]],
  refresh: [["path", { d: "M20 11a8 8 0 1 0-2.3 5.7" }], ["path", { d: "M20 4v7h-7" }]],
  check: [["path", { d: "m5 12 5 5 9-10" }]],
  alert: [["path", { d: "M12 3 2 20h20z" }], ["path", { d: "M12 10v4M12 17h.01" }]],
  clock: [["circle", { cx: 12, cy: 12, r: 9 }], ["path", { d: "M12 7v5l3 2" }]],
  chevron: [["path", { d: "m9 6 6 6-6 6" }]],
  shield: [["path", { d: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" }]],
  hub: [["rect", { x: 5, y: 11, width: 14, height: 10, rx: 2 }], ["path", { d: "M8 11V7a4 4 0 0 1 8 0v4" }]],
  daemon: [["rect", { x: 3, y: 4, width: 18, height: 6, rx: 2 }], ["rect", { x: 3, y: 14, width: 18, height: 6, rx: 2 }],
    ["path", { d: "M7 7h.01M7 17h.01" }]],
};

// A stroke icon from ICONS, drawn as SVG nodes (never parsed markup). It is decoration: screen readers skip it.
export function icon(name, className = "icon") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", className);
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  for (const [tag, attrs] of ICONS[name]) {
    const shape = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs)) shape.setAttribute(key, String(value));
    svg.append(shape);
  }
  return svg;
}
