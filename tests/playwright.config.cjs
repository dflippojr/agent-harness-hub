module.exports = {
  testDir: __dirname,
  testMatch: ["apps-security.spec.cjs", "admin-security.spec.cjs", "pairing-security.spec.cjs",
    "marketplace-security.spec.cjs", "shell-browser.spec.cjs"],
  workers: 1,
  outputDir: "../.validation/playwright-results",
  use: { headless: true },
};
