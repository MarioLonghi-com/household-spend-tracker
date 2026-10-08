/**
 * Lingui: where the messages are, which locales exist, and which are served.
 *
 * `locales` is every catalog that exists, not every language a person can
 * pick. Which ones the app serves is `SERVED_LOCALES` in `src/lib/i18n.ts`,
 * and until #58 that is English alone: the pt-BR, es-ES and sv-SE catalogs
 * hold drafts, every one marked `#, fuzzy`, and none is bundled.
 *
 * `en-XA` is the pseudo-locale: every English message with its letters
 * accented and padded, so an unextracted string stands out as plain ASCII and
 * a layout that only fits English shows it. It is reachable for CI and
 * development only (#53).
 *
 * `npm run extract` rewrites the catalogs from the source. CI runs it and
 * fails on any difference, so a catalog is never behind the code.
 */
import { defineConfig } from "@lingui/conf";
import { formatter } from "@lingui/format-po";

export default defineConfig({
  sourceLocale: "en",
  locales: ["en", "en-XA", "pt-BR", "es-ES", "sv-SE"],
  pseudoLocale: { locale: "en-XA" },
  fallbackLocales: { default: "en" },
  catalogs: [
    {
      path: "<rootDir>/src/locales/{locale}/messages",
      include: ["<rootDir>/src"],
      exclude: ["**/*.test.ts", "**/*.test.tsx", "**/test-setup.ts", "**/test-pseudo.ts"],
    },
  ],
  // Without line numbers, so moving code does not churn every catalog, and
  // ordered by id, so the extraction is the same on every machine.
  format: formatter({ lineNumbers: false }),
  orderBy: "messageId",
});
