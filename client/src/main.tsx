import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "./App";
import { applyAppearance, storedAppearance } from "./lib/appearance";
import { uiLanguage } from "./lib/locale";
import { I18nProvider } from "@lingui/react";
import { i18n, startI18n } from "./lib/i18n";
import "./styles.css";
// Per-screen sheets, loaded after the base so a rule here wins a tie.
import "./styles/accounts.css";
import "./styles/application.css";
import "./styles/register.css";
import "./styles/receipts.css";
import "./styles/categories.css";
import "./styles/report-ie.css";
import "./styles/reimbursements.css";
import "./styles/import.css";
import "./styles/shell.css";
import "./styles/one-time-import.css";

const client = new QueryClient({
  defaultOptions: {
    queries: {
      // A household ledger is not a live feed; refetching on every window focus
      // is noise. Mutations invalidate what they touched.
      refetchOnWindowFocus: false,
      retry: false,
    },
  },
});

// Before the first render, not in an effect: an effect runs after the paint,
// which is exactly long enough to show one frame of the scheme the person
// turned off. The CSS media query has already painted the system's answer by
// now, so this only ever corrects somebody who chose the other one.
applyAppearance(storedAppearance());

// The language the words are in, for screen readers, hyphenation and the
// browser's own translate offer. English is active already, so the first
// render has its words; a stored pseudo-locale swaps in when its catalog
// arrives (`lib/i18n.ts`). The formatting locale is a separate choice.
document.documentElement.lang = uiLanguage();
void startI18n();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <I18nProvider i18n={i18n}>
      <QueryClientProvider client={client}>
        <App />
      </QueryClientProvider>
    </I18nProvider>
  </StrictMode>,
);
