/**
 * How import works (#73), in the reader's language when it has been written
 * in it.
 *
 * The guide is a document, not a screen: whole paragraphs whose order and
 * emphasis are the writer's, and a few hundred catalog entries would cut it
 * into sentences no translator could read as one text. So each language gets
 * its own file in `importGuide/`, written whole, and this picks one (#56).
 *
 * A language without a document yet gets the English one, marked `lang="en"`
 * so a screen reader reads it in English, under one line saying why. English
 * itself renders exactly as it always has.
 *
 * `tests/test_import_guide.py` reads the English document; a document in
 * another language follows it.
 */

import type { ComponentType } from "react";
import { useLingui } from "@lingui/react";
import { Trans } from "@lingui/react/macro";
import { PSEUDO_LOCALE } from "../lib/i18n";
import { ImportGuideDocument as English } from "./importGuide/en";

/** Every language the guide has been written in, by its locale or language. */
const DOCUMENTS: Record<string, ComponentType> = {
  en: English,
};

/** The document for this language, if one has been written. */
export function guideFor(language: string): ComponentType | undefined {
  // The pseudo-locale is English underneath, but it stands for a language
  // that has no document, which is the case it exists to show.
  if (language === PSEUDO_LOCALE) return undefined;
  return DOCUMENTS[language] ?? DOCUMENTS[language.split("-")[0]];
}

export function ImportGuide() {
  // From the hook rather than read once, so the page follows a change of language.
  const { i18n } = useLingui();
  const Written = guideFor(i18n.locale);
  if (Written) return <Written />;
  return (
    <>
      <p className="banner info">
        <Trans>This guide has not been written in your language yet, so here it is in English.</Trans>
      </p>
      <div lang="en">
        <English />
      </div>
    </>
  );
}
