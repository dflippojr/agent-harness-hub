// Hub shell: sign-in gate, the grouped section rail (a drawer on phones) with its daemon chip, Pairing badge and footer,
// the banner slot under each page head, and the hash router.

import { api } from "./lib/api.mjs";
import { banner, clear, h, icon } from "./lib/dom.mjs";
import { daemonBanner, daemonChip, pairingCount, subscribe } from "./lib/status.mjs";
import { renderOverview } from "./pages/overview.mjs";
import { renderSettings } from "./pages/settings.mjs";
import { renderAdmin } from "./pages/admin.mjs";
import { renderApps } from "./pages/apps.mjs";
import { renderMarketplace } from "./pages/marketplace.mjs";
import { renderModules } from "./pages/modules.mjs";
import { renderPairing } from "./pages/pairing.mjs";
import { INVALID_LINK, renderSignin, signinFromHash } from "./pages/signin.mjs";

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

function buildNav() {
  pairingBadge = h("span", { class: "nav-badge", hidden: true });
  clear(sections, GROUPS.map((group, i) => {
    const labelId = group.label ? `nav-group-${i}` : null;
    return [
      group.label ? h("p", { class: "nav-group", id: labelId }, group.label) : null,
      h("ul", { "aria-labelledby": labelId }, group.sections.map((s) => h("li", {},
        h("a", { href: `#/${s.id}`, "data-section": s.id, class: "nav-link" }, icon(s.id, "nav-icon"),
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
