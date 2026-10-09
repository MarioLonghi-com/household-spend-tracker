/**
 * The language this device shows the app in (#53).
 *
 * **Hidden until a second language is served** -- or until an owner turns on
 * review mode on this device (#272), when the draft languages are offered
 * beside English, each labelled as a machine-translated preview under review.
 * With English alone and review mode off this renders nothing, so Profile →
 * Appearance looks exactly as it did. The pseudo-locale is never offered here.
 *
 * Each language is named in itself ("Svenska", not "Swedish"), because the
 * person looking for it may not read the one on screen.
 */
import { t } from "@lingui/core/macro";
import { useLingui } from "@lingui/react";
import { useState, useSyncExternalStore } from "react";
import { activate, DRAFT_LOCALES, onReviewingChange, reviewing, SERVED_LOCALES, storeLocale } from "../lib/i18n";

function ownName(locale: string): string {
  try {
    return new Intl.DisplayNames([locale], { type: "language" }).of(locale) ?? locale;
  } catch {
    return locale;
  }
}

export function LanguagePicker({
  served = SERVED_LOCALES,
  previews,
}: {
  served?: readonly string[];
  /** The drafts offered as previews; by default, the drafts while this device is reviewing. */
  previews?: readonly string[];
}) {
  const { i18n } = useLingui();
  const [busy, setBusy] = useState(false);
  const review = useSyncExternalStore(onReviewingChange, reviewing);
  const drafts = previews ?? (review ? DRAFT_LOCALES : []);
  if (served.length + drafts.length < 2) return null;

  const choose = async (locale: string) => {
    setBusy(true);
    storeLocale(locale);
    await activate(locale);
    setBusy(false);
  };

  const preview = t({
    message: "Preview — machine translated, under review",
    comment: "Beside a language's name in the language picker: its words are a draft, being reviewed",
  });

  return (
    <label className="field">
      <span>{t({ message: "Language", comment: "Label of a choice on the language picker" })}</span>
      <select value={i18n.locale} disabled={busy} onChange={(e) => void choose(e.target.value)}>
        {served.map((locale) => (
          <option key={locale} value={locale} lang={locale}>
            {ownName(locale)}
          </option>
        ))}
        {drafts.map((locale) => (
          <option key={locale} value={locale}>
            {`${ownName(locale)} — ${preview}`}
          </option>
        ))}
      </select>
    </label>
  );
}
