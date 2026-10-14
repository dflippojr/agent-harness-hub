// Hub shell: sign-in gate, the grouped section rail (a drawer on phones) with its daemon chip, Pairing badge and footer,
// the banner slot under each page head, and the hash router.

import { api } from "./lib/api.mjs";
import { banner, clear, h } from "./lib/dom.mjs";
import { daemonBanner, daemonChip, pairingCount, subscribe } from "./lib/status.mjs";
import { renderOverview } from "./pages/overview.mjs";
import { renderSettings } from "./pages/settings.mjs";
import { renderAdmin } from "./pages/admin.mjs";
import { renderApps } from "./pages/apps.mjs";
import { renderMarketplace } from "./pages/marketplace.mjs";
import { renderModules } from "./pages/modules.mjs";
import { renderPairing } from "./pages/pairing.mjs";
import { INVALID_LINK, renderSignin, signinFromHash } from "./pages/signin.mjs";

// Stroke icons from the design canvas, as [element, attributes] pairs on a 24px grid.
const ICONS = {
  overview: [["path", { d: "M4 15a8 8 0 1 1 16 0" }], ["path", { d: "m12 15 4-5" }], ["path", { d: "M4 19h16" }]],
  pairing: [["path", { d: "M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1" }], ["path", { d: "M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1" }]],
  apps: [4, 14].flatMap((y) => [4, 14].map((x) => ["rect", { x, y, width: 6, height: 6, rx: 1 }])),
  marketplace: [["path", { d: "M5 8h14l-1 12H6z" }], ["path", { d: "M9 8a3 3 0 0 1 6 0" }]],
  settings: [["path", { d: "M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1" }], ["circle", { cx: 15, cy: 6, r: 2 }],
    ["circle", { cx: 9, cy: 12, r: 2 }], ["circle", { cx: 17, cy: 18, r: 2 }]],
  modules: [["path", { d: "M12 3 3 8l9 5 9-5z" }], ["path", { d: "M3 8v8l9 5 9-5V8" }], ["path", { d: "M12 13v8" }]],
  admin: [["circle", { cx: 9, cy: 8, r: 3.5 }], ["path", { d: "M2 20a7 7 0 0 1 14 0" }],
    ["path", { d: "M16 4.5a3.5 3.5 0 0 1 0 7M22 20a7 7 0 0 0-5-6.7" }]],
};

// Overview first, then Access, then Daemon. Routes and labels are the CLI's and the docs' names: they do not change.
const GROUPS = [
  { label: null, sections: [{ id: "overview", label: "Overview", render: renderOverview }] },
  { label: "Access", sections: [
    { id: "pairing", label: "Pairing requests", render: renderPairing },
    { id: "apps", label: "Apps", render: renderApps },
    { id: "marketplace", label: "Marketplace", render: renderMarketplace },
  ] },
  { label: "Daemon", sections: [
    { id: "settings", label: "Settings", render: renderSettings },
    { id: "modules", label: "Modules", render: renderModules },
    { id: "admin", label: "Accounts and maintenance", render: renderAdmin },
  ] },
];
const SECTIONS = GROUPS.flatMap((group) => group.sections);

// Remembered per browser; only this on/off preference is stored, never anything from the Hub or the daemon.
const HOST_COMMANDS_KEY = "hub.hostCommands";

const main = document.getElementById("main");
const nav = document.getElementById("nav");
const sections = document.getElementById("nav-sections");
const chip = document.getElementById("daemon-chip");
const hostCommands = document.getElementById("host-commands");
const menu = document.getElementById("menu-button");
const signOut = document.getElementById("signout-button");
const slot = h("div", { id: "banner-slot", class: "banner-slot" });
let stopPage = null;
let pairingBadge = null;

function current() {
  const id = location.hash.replace(/^#\/?/, "") || "overview";
  return SECTIONS.find((s) => s.id === id) || SECTIONS[0];
}

function setMenu(open) {
  document.body.classList.toggle("menu-open", open);
  menu.setAttribute("aria-expanded", String(open));
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "nav-icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  for (const [tag, attrs] of ICONS[name]) {
    const shape = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs)) shape.setAttribute(key, String(value));
    svg.append(shape);
  }
  return svg;
}

function buildNav() {
  pairingBadge = h("span", { class: "nav-badge", hidden: true });
  clear(sections, GROUPS.map((group, i) => {
    const labelId = group.label ? `nav-group-${i}` : null;
    return [
      group.label ? h("p", { class: "nav-group", id: labelId }, group.label) : null,
      h("ul", { "aria-labelledby": labelId }, group.sections.map((s) => h("li", {},
        h("a", { href: `#/${s.id}`, "data-section": s.id, class: "nav-link" }, icon(s.id),
          h("span", { class: "nav-label" }, s.label), s.id === "pairing" ? pairingBadge : null)))),
    ];
  }));
  nav.addEventListener("click", (event) => {
    if (event.target.closest("a")) setMenu(false);
  });
}

function markCurrent(id) {
  for (const link of nav.querySelectorAll("a[data-section]")) {
    if (link.dataset.section === id) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

// Each part is redrawn only when its text changes, so a 30 s Overview refresh does not repeat an announcement.
let shownChip = "", shownBanner = "";
function drawStatus() {
  const daemon = daemonChip();
  const chipKey = daemon ? `${daemon.tone}:${daemon.text}` : "";
  if (chipKey !== shownChip) {
    shownChip = chipKey;
    chip.hidden = !daemon;
    chip.className = daemon ? `daemon-chip ${daemon.tone}` : "daemon-chip";
    chip.textContent = daemon ? daemon.text : "";
  }
  const count = pairingCount();
  if (pairingBadge) {
    pairingBadge.hidden = !count;
    clear(pairingBadge, count ? [String(count), h("span", { class: "visually-hidden" }, " need you")] : null);
  }
  const shown = daemonBanner();
  const bannerKey = shown ? JSON.stringify(shown) : "";
  if (bannerKey !== shownBanner) {
    shownBanner = bannerKey;
    clear(slot, shown ? banner(shown.tone, shown.title, shown.detail,
      shown.action ? h("a", { class: "btn", href: shown.action.href }, shown.action.label) : null) : null);
  }
}

// The slot sits under the page head that every page draws first; the sign-in page has none.
function placeSlot() {
  const head = main.querySelector(":scope > .page-head");
  if (head) head.after(slot);
  else slot.remove();
}

function readHostCommands() {
  try {
    return localStorage.getItem(HOST_COMMANDS_KEY) === "on";
  } catch {
    return false;
  }
}

function setHostCommands(on) {
  document.body.classList.toggle("show-host-commands", on);
  try {
    if (on) localStorage.setItem(HOST_COMMANDS_KEY, "on");
    else localStorage.removeItem(HOST_COMMANDS_KEY);
  } catch {
    // Storage is blocked (a private window): the switch still works for this page.
  }
}

async function route() {
  if (stopPage) stopPage();
  stopPage = null;
  const section = current();
  markCurrent(section.id);
  document.title = `${section.label} · Hub`;
  stopPage = section.render(main);
  placeSlot();
  main.focus({ preventScroll: true });
}

async function signedIn() {
  const { link, code } = signinFromHash(location.hash);
  let error = null;
  if (link) {
    history.replaceState(null, "", location.pathname + "#/overview");
    if (!code) error = new Error(INVALID_LINK);
    else {
      try {
        await api("session", { method: "POST", body: { code } });
      } catch (refused) {
        return { signedIn: false, error: refused };
      }
    }
  }
  const session = await api("session");
  if (error && !session.signed_in) return { signedIn: false, session, error };
  return { signedIn: session.signed_in, session };
}

async function boot() {
  menu.addEventListener("click", () => setMenu(!document.body.classList.contains("menu-open")));
  signOut.addEventListener("click", async () => {
    await api("session", { method: "DELETE" }).catch(() => {});
    location.hash = "#/overview";
    location.reload();
  });
  window.addEventListener("hub:signed-out", () => location.reload());
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setMenu(false);
  });
  // The drawer's backdrop (body.menu-open::after) blocks the page, so a tap or click outside the drawer closes it.
  document.addEventListener("click", (event) => {
    if (!document.body.classList.contains("menu-open") || nav.contains(event.target) || menu.contains(event.target)) return;
    setMenu(false);
    menu.focus();
  });

  let state;
  try {
    state = await signedIn();
  } catch (error) {
    clear(main, h("section", { class: "card bad" }, h("h2", {}, "The Hub server did not answer"), h("p", {}, error.message)));
    return;
  }
  if (!state.signedIn) {
    document.body.classList.add("signed-out");
    const onTailnet = async () => {
      try {
        await api("session", { method: "POST", body: { method: "tailnet" } });
        location.reload();
      } catch (error) {
        renderSignin(main, { ...state, error, onTailnet });
      }
    };
    renderSignin(main, { ...state, onTailnet });
    return;
  }
  document.body.classList.remove("signed-out");
  buildNav();
  hostCommands.checked = readHostCommands();
  setHostCommands(hostCommands.checked);
  hostCommands.addEventListener("change", () => setHostCommands(hostCommands.checked));
  subscribe(drawStatus);
  drawStatus();
  window.addEventListener("hashchange", route);
  route();
}

boot();
