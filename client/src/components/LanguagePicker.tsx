/**
 * The language this device shows the app in (#53).
 *
 * **Hidden until a second language is served.** With English alone in
 * `SERVED_LOCALES` this renders nothing, so Profile → Appearance looks exactly
 * as it did; #58 makes it appear by serving a reviewed catalog. The
 * pseudo-locale is never offered here.
 *
 * Each language is named in itself ("Svenska", not "Swedish"), because the
 * person looking for it may not read the one on screen.
 */
import { t } from "@lingui/core/macro";
import { useLingui } from "@lingui/react";
import { useState } from "react";
import { activate, SERVED_LOCALES, storeLocale } from "../lib/i18n";

function ownName(locale: string): string {
  try {
    return new Intl.DisplayNames([locale], { type: "language" }).of(locale) ?? locale;
  } catch {
    return locale;
  }
}

export function LanguagePicker({ served = SERVED_LOCALES }: { served?: readonly string[] }) {
  const { i18n } = useLingui();
  const [busy, setBusy] = useState(false);
  if (served.length < 2) return null;

  const choose = async (locale: string) => {
    setBusy(true);
    storeLocale(locale);
    await activate(locale);
    setBusy(false);
  };

  return (
    <label className="field">
      <span>{t`Language`}</span>
      <select value={i18n.locale} disabled={busy} onChange={(e) => void choose(e.target.value)}>
        {served.map((locale) => (
          <option key={locale} value={locale} lang={locale}>
            {ownName(locale)}
          </option>
        ))}
      </select>
    </label>
  );
}
