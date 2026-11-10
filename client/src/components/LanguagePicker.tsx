/**
 * The language this device shows the app in (#53).
 *
 * Every served language, each named in itself ("Svenska", not "Swedish"),
 * because the person looking for it may not read the one on screen. Drawn
 * only when there is a choice: with English alone it renders nothing, which
 * is how Profile looked before the first three languages shipped (#58). The
 * pseudo-locale is never offered here.
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
      <span>{t({ message: "Language", comment: "Label of a choice on the language picker" })}</span>
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
