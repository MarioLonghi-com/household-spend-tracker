import { defineConfig, devices } from "@playwright/test";

/**
 * End to end, against the built client and a real server.
 *
 * Two projects, because half of what this batch changed only exists at one
 * width: the nav is a drawer on a phone and a sidebar on a desktop, and a
 * suite that only ever looks at one of them would have passed throughout the
 * period when the phone layout was unusable.
 *
 * `webServer` seeds a throwaway instance on its own port -- see e2e/serve.sh.
 * The client must be built first, which is why the `e2e` script builds.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  reporter: process.env.CI ? "list" : [["list"]],
  timeout: 30_000,
  use: {
    baseURL: "http://127.0.0.1:8850",
    trace: "retain-on-failure",
    // Pinned, so what a spec reads -- "€1,234.56", a date, a sort order -- does
    // not depend on the machine that runs it. The en-XA pass in #53 is the one
    // place another locale is meant to show. UTC, because the server under test
    // decides "today" in its own zone, and a browser a day ahead of it would
    // have a date refused as in the future.
    locale: "en-US",
    timezoneId: "UTC",
  },
  // Both projects are Chromium, deliberately. `Desktop Chrome` wants a real
  // Chrome install and `iPhone 13` wants WebKit, so the pair needed three
  // browser downloads to test one layout question. What is under test here is
  // the layout at a width -- the drawer, the sheets, the wrapping header --
  // and that is decided by viewport, touch and user agent, all of which the
  // Pixel 5 descriptor emulates on the engine already present.
  projects: [
    // Runs first and signs in once; see e2e/auth.setup.ts for why it has to.
    { name: "setup", testMatch: /auth\.setup\.ts/ },
    {
      name: "desktop",
      testIgnore: /(passkeys|pseudo-locale)\.spec\.ts/,
      use: {
        browserName: "chromium",
        viewport: { width: 1280, height: 800 },
        storageState: "./e2e/.auth/state.json",
      },
      dependencies: ["setup"],
    },
    {
      name: "mobile",
      testIgnore: /(passkeys|pseudo-locale)\.spec\.ts/,
      use: { ...devices["Pixel 5"], storageState: "./e2e/.auth/state.json" },
      dependencies: ["setup"],
    },
    // The en-XA pseudo-locale (#53): one pass, at the phone's width, where a
    // longer word is likeliest to break the layout. Every other project stays
    // pinned to English.
    {
      name: "pseudo-locale",
      testMatch: /pseudo-locale\.spec\.ts/,
      use: { ...devices["Pixel 5"], storageState: "./e2e/.auth/state.json" },
      dependencies: ["setup"],
    },
    // Passkeys (#121): registered once, after the sign-in above has spent its
    // code window, then signed in with at both widths -- signed out, so these
    // projects carry no stored session. Their own projects, so a passkey
    // failure never skips the rest of the suite.
    { name: "passkey-setup", testMatch: /passkey\.setup\.ts/, dependencies: ["setup"] },
    {
      name: "passkeys-desktop",
      testMatch: /passkeys\.spec\.ts/,
      use: { browserName: "chromium", viewport: { width: 1280, height: 800 } },
      dependencies: ["passkey-setup"],
    },
    {
      name: "passkeys-mobile",
      testMatch: /passkeys\.spec\.ts/,
      use: { ...devices["Pixel 5"] },
      dependencies: ["passkey-setup"],
    },
  ],
  webServer: {
    command: "./e2e/serve.sh",
    url: "http://127.0.0.1:8850/api/health",
    reuseExistingServer: false,
    timeout: 120_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
