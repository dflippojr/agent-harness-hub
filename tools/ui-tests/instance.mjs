// Start and stop disposable Hub UI-testing instances with scripts/ui_instance.py (docs/ui-testing.md).
//
//   HUB_UI_PYTHON        Python 3.12+ with the Hub and the daemon's requirements (default: python)
//   HUB_UI_DAEMON_SRC    agent-harness checkout with fake model mode (required)
//   HUB_UI_INSTANCE_DIR  base directory for instance directories (default: <tmp>/hub-ui-tests)
import { execFileSync, spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
export const python = process.env.HUB_UI_PYTHON || "python";
export const baseDir = path.resolve(repoRoot, process.env.HUB_UI_INSTANCE_DIR || path.join(os.tmpdir(), "hub-ui-tests"));
const launcher = path.join(repoRoot, "scripts", "ui_instance.py");
const START_MS = 180_000;

function run(...args) {
  return execFileSync(python, [launcher, ...args], { cwd: repoRoot, encoding: "utf8", timeout: 120_000 });
}

export function stopInstance(dir) {
  return run("stop", "--dir", dir);
}

// A fresh instance in its own run-<uuid> directory; resolves with its READY fields once seeded.
export function startInstance({ unclaimed = false } = {}) {
  if (!process.env.HUB_UI_DAEMON_SRC) throw new Error("set HUB_UI_DAEMON_SRC to an agent-harness checkout");
  const dir = path.join(baseDir, `run-${randomUUID()}`);
  const args = [launcher, "start", "--dir", dir, "--log-dir", path.join(baseDir, "logs", path.basename(dir))];
  if (unclaimed) args.push("--unclaimed");
  const child = spawn(python, args, { cwd: repoRoot, stdio: ["ignore", "pipe", "pipe"] });
  let output = "";
  let ready = false;
  return new Promise((resolve, reject) => {
    const fail = (reason) => {
      clearTimeout(timer);
      child.kill();
      try { stopInstance(dir); } catch { /* the launcher removes its own directory on a failed start */ }
      reject(new Error(`${reason}\n${output.slice(-3000)}`));
    };
    const timer = setTimeout(() => fail("the instance was not ready in time"), START_MS);
    const read = (chunk) => {
      output += chunk;
      const line = output.split(/\r?\n/).find((l) => l.startsWith("READY "));
      if (line) {
        ready = true;
        clearTimeout(timer);
        child.stdout.off("data", read);
        child.stdout.resume();
        resolve({ ...JSON.parse(line.slice(6)), process: child, stop: () => stopInstance(dir) });
      }
    };
    child.stdout.setEncoding("utf8").on("data", read);
    child.stderr.setEncoding("utf8").on("data", (chunk) => { output += chunk; });
    child.on("exit", (code) => { if (!ready) fail(`the instance exited with code ${code}`); });
  });
}

// A one-time sign-in link: what the owner prints on the daemon host.
export function signinLink(dir) {
  return run("signin-link", "--dir", dir).trim();
}

export function approveClaim(dir, id, match) {
  return run("approve", "--dir", dir, "--id", id, "--match", match);
}
