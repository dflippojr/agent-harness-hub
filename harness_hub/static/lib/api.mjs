// Calls to this Hub's own server. The browser talks only to the Hub; the Hub alone talks to the daemon.

import { sentence } from "./dom.mjs";
import { noteRead, noteRestart } from "./status.mjs";

// The Hub's own server did not answer: the browser's bare "Failed to fetch", worded as a page shows it.
export const HUB_DOWN = "The Hub server is not answering. Check that it is still running on the daemon host, then retry.";

// A refused or failed call. Its message is always a sentence; status 0 means the Hub server itself did not answer.
export class ApiError extends Error {
  constructor(status, body) {
    super(sentence((body && body.detail) || (status ? `The Hub answered HTTP ${status}` : HUB_DOWN)));
    this.status = status;
    this.body = body || {};
  }
}

let started = 0; // request start order, so the shell's status ignores a reply older than what it already has

export async function api(path, { method = "GET", body } = {}) {
  const order = ++started;
  const headers = { Accept: "application/json" };
  const binding = sessionStorage.getItem("hub.session");
  if (binding) headers["X-Hub-Session"] = binding;
  if (method !== "GET") headers["X-Hub-Request"] = "1";
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response, text;
  try {
    response = await fetch(`/api/${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
      cache: "no-store",
    });
    text = await response.text();
  } catch {
    // No answer from the Hub at all. This says nothing about the daemon, so the shell's status is left alone.
    throw new ApiError(0, null);
  }
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text.slice(0, 200) };
  }
  // The shell uses the reads the pages make and accepted restarts, even when navigation skips the health poll.
  if (method === "GET" && path !== "session") noteRead(path.split("?")[0], response.status, data, order);
  // Acceptance is new evidence: only health requested after it can settle this restart.
  if (method === "POST" && path === "settings/restart" && response.ok && data?.accepted === true) {
    noteRestart(++started, data.target_revision);
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
