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
  const open = openCommands.has(name);
  const button = h("button", { type: "button", class: "btn host-command-toggle", "aria-expanded": String(open),
    "aria-controls": id }, lines.length > 1 ? "Host commands" : "Host command");
  const box = h("div", { class: open ? "host-command open" : "host-command" }, button,
    h("div", { class: "host-command-lines", id }, lines.map(commandLine)));
  button.addEventListener("click", () => {
    const opening = !openCommands.has(name);
    if (opening) openCommands.add(name);
    else openCommands.delete(name);
    box.className = opening ? "host-command open" : "host-command";
    button.setAttribute("aria-expanded", String(opening));
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
// link node.
export function banner(tone, title, detail, action) {
  return h("div", { class: `banner ${tone}`, role: tone === "bad" ? "alert" : "status" },
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

// A chip with the time left on a code or slot, warn in the last minute; none left reads Expired. The caller
// re-renders it, so this adds no timers.
export function countdown(secondsLeft) {
  if (typeof secondsLeft !== "number" || !Number.isFinite(secondsLeft)) return null;
  if (secondsLeft <= 0) return h("span", { class: "countdown" }, "Expired");
  return h("span", { class: secondsLeft < 60 ? "countdown warn" : "countdown" }, `${clock(secondsLeft)} left`);
}

// Irreversible actions, set apart in a red-outlined box under their own heading.
export function dangerZone(title, text, ...actions) {
  return h("section", { class: "danger-zone" }, h("h3", {}, title), text ? h("p", {}, text) : null, actions);
}
