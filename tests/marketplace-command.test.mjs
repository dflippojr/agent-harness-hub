import assert from "node:assert/strict";
import test from "node:test";
import { armCommand } from "../harness_hub/static/pages/marketplace.mjs";

const scope = (name, tier = "standard", text = "Allows " + name) => ({ scope: name, tier, text });
const entry = (scopes, extra = {}) => ({ catalog_app_id: "test.example.native", scopes, elevated: [], ...extra });

test("the arm command omits --acknowledge-elevated when no acknowledgement is required", () => {
  assert.equal(armCommand(entry([scope("sessions")]), ""),
    "harness pairing-requests arm test.example.native --scopes sessions");
});

test("the arm command adds --acknowledge-elevated for an elevated scope", () => {
  const command = armCommand(entry([scope("sessions", "elevated")], { elevated: ["sessions"] }), "https://app.example");
  assert.equal(command,
    "harness pairing-requests arm test.example.native --scopes sessions --origin https://app.example --acknowledge-elevated");
});

test("the arm command adds --acknowledge-elevated when the daemon requires it for a standard scope", () => {
  const command = armCommand(entry([scope("sessions")], { acknowledgement_required: true }), "");
  assert.ok(command.endsWith(" --acknowledge-elevated"), command);
});

test("the arm command adds --acknowledge-elevated for a scope the Hub has no description for", () => {
  const command = armCommand(entry([scope("sessions"), scope("newer", "standard", "")]), "");
  assert.ok(command.endsWith(" --acknowledge-elevated"), command);
});
