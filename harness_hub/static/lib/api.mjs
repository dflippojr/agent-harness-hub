// Calls to this Hub's own server. The browser talks only to the Hub; the Hub alone talks to the daemon.

export class ApiError extends Error {
  constructor(status, body) {
    super((body && body.detail) || `HTTP ${status}`);
    this.status = status;
    this.body = body || {};
  }
}

export async function api(path, { method = "GET", body } = {}) {
  const headers = { Accept: "application/json" };
  const binding = sessionStorage.getItem("hub.session");
  if (binding) headers["X-Hub-Session"] = binding;
  if (method !== "GET") headers["X-Hub-Request"] = "1";
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(`/api/${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
    cache: "no-store",
  });
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text.slice(0, 200) };
  }
  if (!response.ok) {
    if (response.status === 401 && path !== "session") window.dispatchEvent(new CustomEvent("hub:signed-out"));
    throw new ApiError(response.status, data);
  }
  if (path === "session" && method === "POST") {
    sessionStorage.setItem("hub.session", response.headers.get("X-Hub-Session"));
  }
  if (path === "session" && method === "DELETE") sessionStorage.removeItem("hub.session");
  return data;
}
