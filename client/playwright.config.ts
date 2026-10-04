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
      use: {
        browserName: "chromium",
        viewport: { width: 1280, height: 800 },
        storageState: "./e2e/.auth/state.json",
      },
      dependencies: ["setup"],
    },
    {
      name: "mobile",
      use: { ...devices["Pixel 5"], storageState: "./e2e/.auth/state.json" },
      dependencies: ["setup"],
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
