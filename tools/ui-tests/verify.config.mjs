// The verify skill's drive step (.claude/skills/verify): the main path against an instance the skill already started,
// so there is no global setup here. HUB_UI_URL, HUB_UI_DIR and VERIFY_EVIDENCE_DIR come from verify.py.
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";

const evidence = process.env.VERIFY_EVIDENCE_DIR || "test-results/verify";

export default defineConfig({
  testDir: "./verify",
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"]],
  outputDir: path.join(evidence, "playwright"),
  use: { trace: "retain-on-failure", screenshot: "only-on-failure" },
  // One desktop and one phone size, both Chromium so the skill needs only one browser.
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "phone", use: { ...devices["Pixel 7"] } },
  ],
});
