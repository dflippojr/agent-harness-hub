// Sign-in: the owner proves they are on the daemon host by opening a one-time link printed there, or, through
// `tailscale serve`, with the tailnet login the daemon lists as the owner's.

import { banner, clear, command, h, icon, sentence } from "../lib/dom.mjs";

export const INVALID_LINK = "that sign-in link is not valid; links work once, for ten minutes";

// `link` is true for any #signin= fragment, so a truncated or mangled link is refused instead of ignored; `code` is
// set only for a well-formed one, the only kind ever sent to the server.
export function signinFromHash(hash) {
  if (!hash.startsWith("#signin=")) return { link: false, code: "" };
  const match = /^#signin=([A-Za-z0-9_-]{20,128})$/.exec(hash);
  return { link: true, code: match ? match[1] : "" };
}

function tailnetSignin(login, onTailnet) {
  if (!login || !onTailnet) return null;
  const button = h("button", { type: "button", class: "btn primary", "data-action": "tailnet-signin" },
    `Continue as ${login}`);
  button.addEventListener("click", () => {
    button.disabled = true;
    onTailnet();
  });
  return h("div", { class: "signin-tailnet" },
    h("p", {}, "You reached the Hub through Tailscale. If this is the daemon owner's tailnet login, sign in with it:"),
    button,
    h("p", { class: "muted" }, "Or use a sign-in link from the daemon host:"));
}

// The server words a refusal as "what happened; what to do", in lower case. Shown as sentences: the first part is the
// banner's title, the rest its detail.
export function sentences(message) {
  const [title, ...rest] = String(message).split(";");
  return { title: sentence(title), detail: sentence(rest.join(";")) };
}

function refusal(error) {
  const { title, detail } = sentences(error.message);
  // The server's refusal of an unknown link arrives as a sentence (api.mjs); the page's own check sends it raw.
  const more = sentence(error.message) === sentence(INVALID_LINK) ? `${detail} Make a new one below.` : detail;
  return banner("bad", title, more, null, { lead: "alert" });
}

function step(number, ...children) {
  return h("li", { class: "signin-step" }, h("span", { class: "step-number", "aria-hidden": "true" }, String(number)),
    h("div", { class: "signin-step-text" }, children));
}

export function renderSignin(main, { session, error, onTailnet } = {}) {
  const run = (session && session.signin_command) || "python -m harness_hub signin-link";
  const login = session && typeof session.tailnet_login === "string" ? session.tailnet_login : "";
  clear(main,
    h("div", { class: "signin" },
      h("p", { class: "signin-brand" }, h("img", { src: "/static/icon.svg", alt: "", width: 44, height: 44 }), "Hub"),
      h("section", { class: "card signin-card", "aria-labelledby": "signin-title" },
        h("h1", { id: "signin-title" }, "Sign in to the Hub"),
        h("p", { class: "muted signin-lede" }, "The Hub administers your daemon, so only its owner can open it."),
        error ? refusal(error) : null,
        tailnetSignin(login, onTailnet),
        h("ol", { class: "signin-steps" },
          step(1, h("p", {}, "On the daemon host, as the account that runs the Hub, run:"), command(run)),
          step(2, h("p", {}, "Open the link it prints in this browser. It signs in this tab only, and works once, ",
            "for ten minutes."))),
        h("details", { class: "signin-docker" },
          h("summary", {}, icon("chevron", "icon chevron"), "The Hub runs in Docker"),
          h("p", {}, "Run the command inside the Hub's container:"),
          command("docker exec <hub container> python -m harness_hub signin-link --state-dir /hub-state")))));
}
