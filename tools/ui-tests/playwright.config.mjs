// Hub UI tests against a disposable instance (docs/ui-testing.md): a fake model daemon plus a Hub, both on loopback
// with synthetic data. global-setup.mjs starts it; nothing here may point at a live daemon or Hub.
import { defineConfig, devices } from "@playwright/test";

const desktop = { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } };
// The installed "iPhone SE" descriptor is the 320x568 first generation; the pass uses the 375x667 one, keeping its
// WebKit, touch, user agent and device scale factor.
const iphone = { ...devices["iPhone SE"], viewport: { width: 375, height: 667 } };

// The instance projects walk ./tests; keep them off the intercepted specs wherever those live.
const testIgnore = "**/intercepted/**";

export default defineConfig({
  testDir: "./tests",
  // One shared instance whose settings, modules and pairing state the journeys change: never in parallel.
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  globalSetup: "./global-setup.mjs",
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  outputDir: "test-results",
  use: { trace: "retain-on-failure", screenshot: "only-on-failure" },
  projects: [
    { name: "desktop-light", testIgnore, use: { ...desktop, colorScheme: "light" } },
    { name: "desktop-dark", testIgnore, use: { ...desktop, colorScheme: "dark" } },
    { name: "phone-light", testIgnore, use: { ...iphone, colorScheme: "light" } },
    { name: "phone-dark", testIgnore, use: { ...iphone, colorScheme: "dark" } },
    { name: "pixel-dark", testIgnore, use: { ...devices["Pixel 7"], colorScheme: "dark" } },
    // Specs over the real static files with every request intercepted: no instance, no daemon. Only for states the real
    // daemon cannot be made to produce (forced errors, withheld or held replies, markup, drift); see docs/ui-testing.md.
    { name: "intercepted", testDir: "./intercepted", testMatch: "*.spec.cjs", use: { ...desktop, colorScheme: "light" } },
  ],
});
