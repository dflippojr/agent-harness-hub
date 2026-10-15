// Hub shell: sign-in gate, the grouped section rail with its daemon chip, Pairing badge and footer, the phone's bottom
// bar, the banner slot under each page head, and the hash router.

import { api } from "./lib/api.mjs";
import { banner, clear, emptyState, h, hubDownState, icon } from "./lib/dom.mjs";
import { daemonBanner, daemonChip, pairingCount, subscribe } from "./lib/status.mjs";
import { renderOverview } from "./pages/overview.mjs";
import { renderSettings } from "./pages/settings.mjs";
import { renderAdmin } from "./pages/admin.mjs";
import { renderApps } from "./pages/apps.mjs";
import { renderMarketplace } from "./pages/marketplace.mjs";
import { renderModules } from "./pages/modules.mjs";
import { renderPairing } from "./pages/pairing.mjs";
import { renderSpend } from "./pages/spend.mjs";
import { INVALID_LINK, renderSignin, signinFromHash } from "./pages/signin.mjs";

// Overview first, then Access, then Daemon. Routes and labels are the CLI's and the docs' names: they do not change.
const GROUPS = [
  { label: null, sections: [{ id: "overview", label: "Overview", render: renderOverview }] },
  { label: "Access", sections: [
    { id: "pairing", label: "Pairing requests", render: renderPairing },
    { id: "apps", label: "Apps", render: renderApps },
    { id: "spend", label: "Spend", render: renderSpend },
    { id: "marketplace", label: "Marketplace", render: renderMarketplace },
  ] },
  { label: "Daemon", sections: [
    { id: "settings", label: "Settings", render: renderSettings },
    { id: "modules", label: "Modules", render: renderModules },
    { id: "admin", label: "Accounts and maintenance", render: renderAdmin },
  ] },
];
const SECTIONS = GROUPS.flatMap((group) => group.sections);
// Below 900 px a bottom bar carries these three and More, which opens the rest of the rail as a sheet.
const BAR = [
  { id: "overview", label: "Overview" },
  { id: "pairing", label: "Pairing" },
  { id: "apps", label: "Apps" },
];
const phone = matchMedia("(max-width: 899.98px)");

// Remembered per browser; only this on/off preference is stored, never anything from the Hub or the daemon.
const HOST_COMMANDS_KEY = "hub.hostCommands";

const main = document.getElementById("main");
const nav = document.getElementById("nav");
const sections = document.getElementById("nav-sections");
const chip = document.getElementById("daemon-chip");
const railFoot = chip.parentElement;
const hostCommands = document.getElementById("host-commands");
const tabbar = document.getElementById("tabbar");
const signOut = document.getElementById("signout-button");
const slot = h("div", { id: "banner-slot", class: "banner-slot" });
let stopPage = null;
let more = null; // the bottom bar's More button, which opens the sheet
let pairingBadges = []; // the rail's and the bottom bar's

function current() {
  const id = location.hash.replace(/^#\/?/, "") || "overview";
  return SECTIONS.find((s) => s.id === id) || SECTIONS[0];
}

const sheetOpen = () => document.body.classList.contains("menu-open");

function setMenu(open) {
  document.body.classList.toggle("menu-open", open);
  if (more) more.setAttribute("aria-expanded", String(open));
}

function buildNav() {
  const onBar = new Set(BAR.map((s) => s.id));
  pairingBadges = [0, 1].map(() => h("span", { class: "nav-badge", hidden: true }));
  // The sheet is the rail without the bar's sections (li.on-bar, hidden below 900 px).
  clear(sections, GROUPS.map((group, i) => {
    const labelId = group.label ? `nav-group-${i}` : null;
    return [
      group.label ? h("p", { class: "nav-group", id: labelId }, group.label) : null,
      h("ul", { "aria-labelledby": labelId }, group.sections.map((s) => h("li", { class: onBar.has(s.id) ? "on-bar" : null },
        h("a", { href: `#/${s.id}`, "data-section": s.id, class: "nav-link" }, icon(s.id, "nav-icon"),
          h("span", { class: "nav-label" }, s.label), s.id === "pairing" ? pairingBadges[0] : null)))),
    ];
  }));
  more = h("button", { type: "button", id: "more-button", class: "tab", "aria-controls": "nav", "aria-expanded": "false",
    onclick: () => setMenu(!sheetOpen()) }, icon("more", "nav-icon"), h("span", { class: "tab-label" }, "More"));
  clear(tabbar, BAR.map((s) => h("a", { href: `#/${s.id}`, "data-section": s.id, class: "tab" }, icon(s.id, "nav-icon"),
    h("span", { class: "tab-label" }, s.label), s.id === "pairing" ? pairingBadges[1] : null)), more);
  tabbar.hidden = false;
  for (const bar of [nav, tabbar]) {
    bar.addEventListener("click", (event) => {
      if (event.target.closest("a")) setMenu(false);
    });
  }
}

function markCurrent(id) {
  for (const link of document.querySelectorAll("#nav a[data-section], #tabbar a[data-section]")) {
    if (link.dataset.section === id) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  // A section from the sheet lights up More, as the bar's own sections light up their tab.
  if (more) more.classList.toggle("current", !BAR.some((s) => s.id === id));
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
  for (const badge of pairingBadges) {
    badge.hidden = !count;
    clear(badge, count ? [String(count), h("span", { class: "visually-hidden" }, " need you")] : null);
  }
  const shown = daemonBanner();
  const bannerKey = shown ? JSON.stringify(shown) : "";
  if (bannerKey !== shownBanner) {
    shownBanner = bannerKey;
    clear(slot, shown ? banner(shown.tone, shown.title, shown.detail, bannerAction(shown.action)) : null);
  }
}

// A banner's link, or its Retry now button, which has the open page repeat its own reads: in place when the page offers
// a retry (Settings, so unsaved edits stay), else by drawing it again.
function bannerAction(action) {
  if (!action) return null;
  if (action.retry) {
    return h("button", { type: "button", class: "btn", onclick: () => (stopPage?.retry ? stopPage.retry() : route()) },
      action.label);
  }
  return h("a", { class: "btn", href: action.href }, action.label);
}

// The slot sits under the page head that every page draws first; the sign-in page has none. Below 900 px the daemon
// chip moves from the rail's footer to beside the page title, and the rail, now the More sheet, is named for it.
function placeShell() {
  const head = main.querySelector(":scope > .page-head");
  if (head) head.after(slot);
  else slot.remove();
  const title = phone.matches && head ? head.querySelector("h1") : null;
  if (title) title.after(chip);
  else if (chip.parentElement !== railFoot) railFoot.prepend(chip);
  nav.setAttribute("aria-label", phone.matches ? "More sections" : "Sections");
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
  placeShell();
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
  signOut.addEventListener("click", async () => {
    await api("session", { method: "DELETE" }).catch(() => {});
    location.hash = "#/overview";
    location.reload();
  });
  window.addEventListener("hub:signed-out", () => location.reload());
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !sheetOpen()) return;
    setMenu(false);
    more.focus();
  });
  // The sheet's backdrop (body.menu-open::after) covers the page above the bar, so a tap or click outside the sheet
  // and the bar closes it.
  document.addEventListener("click", (event) => {
    if (!sheetOpen() || nav.contains(event.target) || tabbar.contains(event.target)) return;
    setMenu(false);
    more.focus();
  });
  // Crossing 900 px swaps the bar and sheet for the rail: close the sheet and put the chip where this layout shows it.
  phone.addEventListener("change", () => {
    setMenu(false);
    placeShell();
  });

  let state;
  try {
    state = await signedIn();
  } catch (error) {
    // Nothing is known yet, not even whether the owner is signed in: the state that says so, with a retry.
    const retry = () => location.reload();
    clear(main, error.status === 0 ? hubDownState(retry) : emptyState("alert", "The Hub could not start this page",
      error.message, h("button", { type: "button", class: "btn", onclick: retry }, "Retry")));
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
