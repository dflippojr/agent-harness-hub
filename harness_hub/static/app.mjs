// Hub shell: sign-in gate, section navigation (left rail on wide screens, a menu on phones) and the hash router.

import { api } from "./lib/api.mjs";
import { clear, h } from "./lib/dom.mjs";
import { renderOverview } from "./pages/overview.mjs";
import { renderSettings } from "./pages/settings.mjs";
import { renderAdmin } from "./pages/admin.mjs";
import { renderApps } from "./pages/apps.mjs";
import { renderModules } from "./pages/modules.mjs";
import { renderSection, SECTIONS } from "./pages/section.mjs";
import { renderSignin, signinCodeFromHash } from "./pages/signin.mjs";

const main = document.getElementById("main");
const nav = document.getElementById("nav");
const menu = document.getElementById("menu-button");
const signOut = document.getElementById("signout-button");
let stopPage = null;

function current() {
  const id = location.hash.replace(/^#\/?/, "") || "overview";
  return SECTIONS.find((s) => s.id === id) ? id : "overview";
}

function setMenu(open) {
  document.body.classList.toggle("menu-open", open);
  menu.setAttribute("aria-expanded", String(open));
}

function buildNav() {
  clear(nav, h("ul", {}, SECTIONS.map((s) => h("li", {},
    h("a", { href: `#/${s.id}`, "data-section": s.id, class: "nav-link" },
      h("span", { class: "nav-label" }, s.label),
      s.ready ? null : h("span", { class: "nav-note" }, "CLI")),
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
  const id = current();
  markCurrent(id);
  const section = SECTIONS.find((s) => s.id === id);
  document.title = `${section.label} · Hub`;
  stopPage = id === "overview" ? renderOverview(main) : id === "settings" ? renderSettings(main) :
    id === "modules" ? renderModules(main) : id === "apps" ? renderApps(main) : id === "admin" ? renderAdmin(main) : renderSection(main, section);
  main.focus({ preventScroll: true });
}

async function signedIn() {
  const code = signinCodeFromHash();
  if (code) {
    history.replaceState(null, "", location.pathname + "#/overview");
    try {
      await api("session", { method: "POST", body: { code } });
    } catch (error) {
      return { signedIn: false, error };
    }
  }
  const session = await api("session");
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

  let state;
  try {
    state = await signedIn();
  } catch (error) {
    clear(main, h("section", { class: "card bad" }, h("h2", {}, "The Hub server did not answer"), h("p", {}, error.message)));
    return;
  }
  if (!state.signedIn) {
    document.body.classList.add("signed-out");
    renderSignin(main, state);
    return;
  }
  document.body.classList.remove("signed-out");
  buildNav();
  window.addEventListener("hashchange", route);
  route();
}

boot();
