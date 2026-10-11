module.exports = {
  testDir: __dirname,
  testMatch: "apps-security.spec.cjs",
  workers: 1,
  outputDir: "../.validation/playwright-results",
  use: { headless: true },
};
