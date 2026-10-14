import { defineConfig } from "@playwright/test";

/**
 * The packaged app, driven end to end (#61): `npm run smoke` after
 * `python -m scripts.desktop_build mac|win`.
 *
 * Electron support in Playwright is "experimental", so this is a smoke test
 * and nothing more: it proves the packaged pieces fit -- the shell, the frozen
 * backend, the bundled client, the ledger's folder, the backups folder, a
 * relaunch -- once in English and once in the pseudo-locale, where every word
 * the shell or the window shows is accented. The screens themselves are the
 * client's own end-to-end suite's.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 180_000,
  reporter: process.env.CI ? "list" : [["list"]],
  projects: [
    { name: "en", metadata: { locale: "en" } },
    { name: "en-XA", metadata: { locale: "en-XA" } },
  ],
});
