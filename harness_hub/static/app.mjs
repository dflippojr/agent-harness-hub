// Hub shell: sign-in gate, section navigation (left rail on wide screens, a menu on phones) and the hash router.

import { api } from "./lib/api.mjs";
import { clear, h } from "./lib/dom.mjs";
import { renderOverview } from "./pages/overview.mjs";
import { renderSettings } from "./pages/settings.mjs";
import { renderAdmin } from "./pages/admin.mjs";
import { renderApps } from "./pages/apps.mjs";
import { renderMarketplace } from "./pages/marketplace.mjs";
import { renderModules } from "./pages/modules.mjs";
import { renderPairing } from "./pages/pairing.mjs";
import { renderSpend } from "./pages/spend.mjs";
import { INVALID_LINK, renderSignin, signinFromHash } from "./pages/signin.mjs";

const SECTIONS = [
  { id: "overview", label: "Overview", render: renderOverview },
  { id: "settings", label: "Settings", render: renderSettings },
  { id: "modules", label: "Modules", render: renderModules },
  { id: "apps", label: "Apps", render: renderApps },
  { id: "spend", label: "Spend", render: renderSpend },
  { id: "pairing", label: "Pairing requests", render: renderPairing },
  { id: "marketplace", label: "Marketplace", render: renderMarketplace },
  { id: "admin", label: "Accounts and maintenance", render: renderAdmin },
];

const main = document.getElementById("main");
const nav = document.getElementById("nav");
const menu = document.getElementById("menu-button");
const signOut = document.getElementById("signout-button");
let stopPage = null;

function current() {
  const id = location.hash.replace(/^#\/?/, "") || "overview";
  return SECTIONS.find((s) => s.id === id) || SECTIONS[0];
}

function setMenu(open) {
  document.body.classList.toggle("menu-open", open);
  menu.setAttribute("aria-expanded", String(open));
}

function buildNav() {
  clear(nav, h("ul", {}, SECTIONS.map((s) => h("li", {},
    h("a", { href: `#/${s.id}`, "data-section": s.id, class: "nav-link" },
      h("span", { class: "nav-label" }, s.label)),
  ))));
  nav.addEventListener("click", (event) => {
    if (event.target.closest("a")) setMenu(false);
  });
}

function markCurrent(id) {
  for (const link of nav.querySelectorAll("a")) {
    if (link.dataset.section === id) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

async function route() {
  if (stopPage) stopPage();
  stopPage = null;
  const section = current();
  markCurrent(section.id);
  document.title = `${section.label} · Hub`;
  stopPage = section.render(main);
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
  window.addEventListener("hashchange", route);
  route();
}

boot();
