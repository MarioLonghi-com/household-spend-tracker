/**
 * "Ask how to…": the help panel (#460 §6).
 *
 * Opened from Help in the menu, from the **?** in a screen's heading, and by
 * the old How import works route (§12). A question in plain words is
 * searched over the articles of the reader's language; with no question, or
 * none that matches, the panel offers the articles about the screen on show.
 * It follows a change of language while open, like every screen.
 *
 * Loaded lazily by the shell together with MiniSearch, so neither is in the
 * main bundle; the articles arrive as their language's own chunk.
 */

import { useEffect, useMemo, useState } from "react";
import { t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";
import { useLingui } from "@lingui/react";
import { Panel } from "../components/bits";
import { useCapabilities } from "../lib/capabilities";
import type { Article } from "./compile";
import { HelpArticle } from "./HelpArticle";
import { loadHelp, type Loaded } from "./load";
import { indexArticles } from "./search";

export type HelpPanelProps = {
  /** The screen on show: its articles come first. */
  screen: string;
  /** An article to open straight away. */
  start?: string;
  onClose: () => void;
  /** Follow a `screen:` link: the shell opens the screen and closes the panel. */
  onScreen: (screen: string) => void;
};

export function HelpPanel({ screen, start, onClose, onScreen }: HelpPanelProps) {
  const { i18n } = useLingui();
  const offered = useCapabilities();
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [failed, setFailed] = useState(false);
  const [question, setQuestion] = useState("");
  // The articles opened, newest last, so Back retraces a `help:` link.
  const [trail, setTrail] = useState<string[]>(start ? [start] : []);

  useEffect(() => {
    let current = true;
    setFailed(false);
    loadHelp(i18n.locale).then(
      (answer) => current && setLoaded(answer),
      () => current && setFailed(true),
    );
    return () => {
      current = false;
    };
  }, [i18n.locale]);

  // What this deployment offers: an article about a feature it does not have
  // is not listed, searched or opened.
  const articles = useMemo(
    () =>
      (loaded?.articles ?? []).filter(
        (article) => !article.requires || offered[article.requires as keyof typeof offered],
      ),
    [loaded, offered],
  );
  const index = useMemo(() => (loaded ? indexArticles(articles, loaded.locale) : null), [articles, loaded]);
  const here = articles.filter((article) => article.screens.includes(screen));
  const found = index ? index.search(question) : [];
  const open = trail.length ? articles.find((article) => article.id === trail[trail.length - 1]) : undefined;

  const list = (some: Article[]) => (
    <ul className="help-list">
      {some.map((article) => (
        <li key={article.id}>
          <button type="button" className="link" onClick={() => setTrail([...trail, article.id])}>
            {article.title}
          </button>
        </li>
      ))}
    </ul>
  );

  const body = !loaded ? (
    failed ? (
      <p className="banner" role="alert">
        <Trans>Help could not be loaded. Close this panel and open it again.</Trans>
      </p>
    ) : (
      <p className="muted">
        <Trans comment="Text in the help panel while its articles load">Loading…</Trans>
      </p>
    )
  ) : open ? (
    <>
      <button type="button" className="link help-back" onClick={() => setTrail(trail.slice(0, -1))}>
        {trail.length > 1
          ? t({ message: "← Back", comment: "Button in the help panel: back to the article before" })
          : t({ message: "← All articles", comment: "Button in the help panel: from an article back to the list" })}
      </button>
      <HelpArticle
        article={open}
        onArticle={(id) => setTrail([...trail, id])}
        onScreen={(name) => {
          onScreen(name);
          onClose();
        }}
      />
    </>
  ) : (
    <>
      <label className="field">
        <span>
          <Trans comment="Label of the search box in the help panel">What do you want to do?</Trans>
        </span>
        <input
          type="search"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder={t({ message: "For example: split a receipt", comment: "Placeholder in the help panel's search box: an example question" })}
          autoFocus
        />
      </label>
      {question.trim() && found.length > 0 ? (
        list(found)
      ) : (
        <>
          {question.trim() ? (
            <p className="muted small" role="status">
              <Trans>Nothing matched those words. Try fewer, or other words.</Trans>
            </p>
          ) : null}
          {here.length > 0 ? (
            <>
              <h4>
                <Trans comment="Heading in the help panel over the articles about the screen on show">About this screen</Trans>
              </h4>
              {list(here)}
            </>
          ) : null}
          {question.trim() ? null : (
            <>
              <h4>
                <Trans comment="Heading in the help panel over the list of every article">Every article</Trans>
              </h4>
              {list(
                articles
                  .filter((article) => !here.includes(article))
                  .sort((a, b) => a.title.localeCompare(b.title, loaded.locale)),
              )}
            </>
          )}
        </>
      )}
    </>
  );

  return (
    <Panel title={t({ message: "Ask how to…", comment: "Title of the help panel, opened from Help in the menu" })} onClose={onClose}>
      {loaded?.fallback ? (
        <p className="banner info">
          <Trans>Help has not been written in your language yet, so here it is in English.</Trans>
        </p>
      ) : null}
      <div className="help-panel" lang={loaded?.fallback ? "en" : undefined}>
        {body}
      </div>
    </Panel>
  );
}

export default HelpPanel;
