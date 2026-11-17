import assert from "node:assert/strict";
import test from "node:test";
import { commandWords, COMMANDS, hostCommand } from "../harness_hub/static/lib/commands.mjs";

test("a host command fills the placeholders it is given and leaves the rest for the owner", () => {
  assert.equal(hostCommand("hubApprove", { id: "req-1" }), "harness hub approve req-1 --match <code>");
  assert.equal(hostCommand("keysRevoke"), "harness keys revoke <key_id>");
  assert.equal(hostCommand("configSet", { key: "memory_library.enabled", value: true }),
    "harness config set memory_library.enabled=true");
});

test("an optional part is printed only for a truthy flag, with its leading space", () => {
  const values = { catalog_app_id: "test.example.notes", scopes: "sessions models" };
  assert.equal(hostCommand("pairingArm", values),
    "harness pairing-requests arm test.example.notes --scopes sessions models");
  assert.equal(hostCommand("pairingArm", { ...values, origin: "https://app.example", elevated: true }),
    "harness pairing-requests arm test.example.notes --scopes sessions models --origin https://app.example " +
    "--acknowledge-elevated");
  assert.equal(hostCommand("pairingApprove", { id: "r", elevated: false }),
    "harness pairing-requests approve r --match <code>");
});

test("a filled-in value is printed as it is, never read as a placeholder", () => {
  assert.equal(hostCommand("hubDeny", { id: "<code>" }), "harness hub deny <code>");
  assert.equal(hostCommand("pairingConfirm", { id: "$&" }), "harness pairing-requests confirm $& --match <code>");
});

test("command words name the command alone, for prose", () => {
  assert.equal(commandWords("hubApprove"), "harness hub approve");
  assert.equal(commandWords("pairingArm"), "harness pairing-requests arm");
  assert.equal(commandWords("hubEntriesInit"), "harness hub entries init");
});

test("an unknown command name throws instead of printing nothing", () => {
  assert.throws(() => hostCommand("hubStatuss"), /No host command named hubStatuss/);
  assert.throws(() => commandWords("toString"), /No host command named toString/);
});

test("the table is frozen and every entry is a harness command", () => {
  assert.ok(Object.isFrozen(COMMANDS));
  for (const [name, line] of Object.entries(COMMANDS)) assert.ok(line.startsWith("harness "), name);
});
