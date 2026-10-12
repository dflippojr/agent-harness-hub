// Sign-in: the owner proves they are on the daemon host by opening a one-time link printed there, or, through
// `tailscale serve`, with the tailnet login the daemon lists as the owner's.

import { clear, command, h } from "../lib/dom.mjs";

export function signinCodeFromHash() {
  const match = /^#signin=([A-Za-z0-9_-]{20,128})$/.exec(location.hash);
  return match ? match[1] : "";
}

function tailnetSignin(login, onTailnet) {
  if (!login || !onTailnet) return null;
  const button = h("button", { type: "button", class: "btn primary", "data-action": "tailnet-signin" },
    `Continue as ${login}`);
  button.addEventListener("click", () => {
    button.disabled = true;
    onTailnet();
  });
  return [
    h("p", {}, "You reached the Hub through Tailscale. If this is the daemon owner's tailnet login, sign in with it:"),
    button,
    h("p", { class: "muted" }, "Or use a sign-in link from the daemon host:"),
  ];
}

export function renderSignin(main, { session, error, onTailnet } = {}) {
  const run = (session && session.signin_command) || "python -m harness_hub signin-link";
  const login = session && typeof session.tailnet_login === "string" ? session.tailnet_login : "";
  clear(main,
    h("section", { class: "card signin", "aria-labelledby": "signin-title" },
      h("h2", { id: "signin-title" }, "Sign in to the Hub"),
      error ? h("p", { class: "notice bad", role: "alert" }, error.message) : null,
      tailnetSignin(login, onTailnet),
      h("p", {}, "The Hub administers your daemon, so only the owner can open it. On the daemon host, as the account that runs the Hub, run:"),
      command(run),
      h("p", { class: "muted" }, "Open the link it prints in this browser. Each link works once, for ten minutes."),
      h("details", {},
        h("summary", {}, "The Hub runs in Docker"),
        h("p", {}, "Run the command inside the Hub's container:"),
        command("docker exec <hub container> python -m harness_hub signin-link --state-dir /hub-state")),
    ));
}
