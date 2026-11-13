/**
 * The **?** in a screen's heading (#460 §6): opens help at the articles about
 * the screen on show.
 *
 * Part of the main bundle, so it is kept to this: which screens have articles
 * comes from `virtual:help-index`, a few hundred bytes the build writes from
 * the articles' `screens:`, and the articles themselves load only when the
 * panel opens. Outside the shell -- a screen rendered on its own, in a test --
 * there is no context and nothing is drawn.
 */

import { createContext, useContext } from "react";
import { t } from "@lingui/core/macro";
import { useLingui } from "@lingui/react";
import index from "virtual:help-index";
import { useCapabilities, type Capabilities } from "../lib/capabilities";

export type ScreenHelp = {
  /** The screen on show. */
  screen: string;
  /** Open the panel at that screen's articles. */
  open: () => void;
};

export const HelpContext = createContext<ScreenHelp | null>(null);

/** The ids of the articles about a screen, as this deployment offers them. */
export function articlesFor(screen: string, offered: Capabilities): string[] {
  return Object.entries(index)
    .filter(([, about]) => about.screens.includes(screen))
    .filter(([, about]) => !about.requires || offered[about.requires as keyof Capabilities])
    .map(([id]) => id);
}

/** Whether an article is offered here at all. */
export function offeredArticle(id: string, offered: Capabilities): boolean {
  const about = index[id];
  return Boolean(about) && (!about.requires || Boolean(offered[about.requires as keyof Capabilities]));
}

export function HelpMark() {
  useLingui();
  const help = useContext(HelpContext);
  const offered = useCapabilities();
  if (!help || articlesFor(help.screen, offered).length === 0) return null;
  const label = t({ message: "Help with this screen", comment: "Screen-reader name and tooltip of the ? beside a screen's title, which opens help about it" });
  return (
    <button type="button" className="help-mark" aria-label={label} title={label} onClick={help.open}>
      ?
    </button>
  );
}
