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

// A command the owner runs on the daemon host, with a copy button.
export function command(text) {
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
