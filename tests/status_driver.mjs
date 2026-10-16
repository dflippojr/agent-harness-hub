// Drives the shell's real status module (harness_hub/static/lib/status.mjs) for tests/test_state_machine.py. One JSON
// command per stdin line, one JSON answer per stdout line:
//   {"op": "reset"}                                          forget everything, as a fresh page load
//   {"op": "read", "path", "status", "data", "started"}      a Hub GET reply, as api.mjs reports it
//   {"op": "restart", "started", "target"}                   an accepted restart, as api.mjs reports it
// Every answer is {"banner": ..., "chip": ...}, what the shell shows after the command.
import { createInterface } from "node:readline";

import { daemonBanner, daemonChip, noteRead, noteRestart, resetStatus } from "../harness_hub/static/lib/status.mjs";

const lines = createInterface({ input: process.stdin });
lines.on("line", (line) => {
  let answer;
  try {
    const command = JSON.parse(line);
    if (command.op === "reset") resetStatus();
    else if (command.op === "read") noteRead(command.path, command.status, command.data, command.started);
    else if (command.op === "restart") noteRestart(command.started, command.target);
    else throw new Error(`unknown op ${command.op}`);
    answer = { banner: daemonBanner(), chip: daemonChip() };
  } catch (error) {
    answer = { error: String(error && error.stack || error) };
  }
  process.stdout.write(`${JSON.stringify(answer)}\n`);
});
