/**
 * The shell's words, in the language the window shows (#62).
 *
 * The messages are in the client's catalogs: `client/lingui.config.ts`
 * extracts from `desktop/src` as well, so `npm run extract` and its CI gate
 * cover these too, and a translator sees the menu beside the screen it opens.
 *
 * The window says which language it activated (`lib/desktop.ts` in the
 * client, through the preload), and only a language the window may show
 * arrives: drafts only in review mode or a QA build, as there. The shell keeps
 * the last one in its settings, so the splash and an early dialog are in it
 * before the window has loaded.
 */

import { i18n, type MessageDescriptor, type Messages } from "@lingui/core";
import { messages as english } from "../../client/src/locales/en/messages.po";
import { messages as pseudo } from "../../client/src/locales/en-XA/messages.po";
import { messages as portuguese } from "../../client/src/locales/pt-BR/messages.po";
import { messages as spanish } from "../../client/src/locales/es-ES/messages.po";
import { messages as swedish } from "../../client/src/locales/sv-SE/messages.po";

export const SOURCE = "en";

const CATALOGS: Record<string, Messages> = {
  en: english,
  "en-XA": pseudo,
  "pt-BR": portuguese,
  "es-ES": spanish,
  "sv-SE": swedish,
};

for (const [locale, messages] of Object.entries(CATALOGS)) i18n.load(locale, messages);
i18n.activate(SOURCE);

/** Switch to `locale` if the shell has its catalog, else English. Returns the one now active. */
export function useLanguage(locale: string): string {
  const chosen = Object.hasOwn(CATALOGS, locale) ? locale : SOURCE;
  if (i18n.locale !== chosen) i18n.activate(chosen);
  return chosen;
}

export function language(): string {
  return i18n.locale;
}

/** One message, in the active language. */
export function say(message: MessageDescriptor, values?: Record<string, unknown>): string {
  return i18n._(message.id, values ?? message.values, { message: message.message });
}
