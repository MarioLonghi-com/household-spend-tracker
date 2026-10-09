/**
 * "Suggest a better wording" -- review mode's own control (#272).
 *
 * Shown only while an owner is reviewing a draft language on this device. It
 * finds a message in the language's catalog, by its words in either
 * language -- text selected on the screen when it opens is searched for
 * straight away, which is how a reviewer picks a message off the screen --
 * and shows the English beside the translator's note and the draft, then
 * keeps the reviewer's wording as a suggestion for the household. The
 * catalogs themselves change only when the suggestions are applied, on a
 * branch (`scripts/apply_translation_suggestions.py`).
 */

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { t } from "@lingui/core/macro";
import { useLingui } from "@lingui/react";
import { Trans } from "@lingui/react/macro";
import { api } from "../lib/api";
import { placeholdersOf, readPo, type PoEntry } from "../lib/po";
import type { Household } from "../lib/types";
import { Panel, Problem } from "./bits";

/** Each draft catalog as text, its own chunk, fetched when a reviewer searches it. */
const RAW: Record<string, () => Promise<{ default: string }>> = {
  "pt-BR": () => import("../locales/pt-BR/messages.po?raw"),
  "es-ES": () => import("../locales/es-ES/messages.po?raw"),
  "sv-SE": () => import("../locales/sv-SE/messages.po?raw"),
};

/** At most this many matches are listed; a longer search narrows them. */
const SHOWN = 30;

function sameShape(a: string, b: string): boolean {
  const one = placeholdersOf(a);
  const two = placeholdersOf(b);
  return JSON.stringify(one) === JSON.stringify(two);
}

export function SuggestWording({ household, locale }: { household: Household; locale: string }) {
  useLingui();
  const [open, setOpen] = useState<string | null>(null);
  return (
    <>
      <button
        type="button"
        className="suggest-wording"
        aria-haspopup="dialog"
        onClick={() => setOpen(window.getSelection()?.toString().trim() ?? "")}
      >
        <Trans comment="Button shown while reviewing a draft language: propose better words for one message">
          Suggest a better wording
        </Trans>
      </button>
      {open !== null && (
        <SuggestPanel household={household} locale={locale} start={open} onClose={() => setOpen(null)} />
      )}
    </>
  );
}

function SuggestPanel({
  household,
  locale,
  start,
  onClose,
}: {
  household: Household;
  locale: string;
  start: string;
  onClose: () => void;
}) {
  const catalog = useQuery({
    queryKey: ["catalog-text", locale],
    queryFn: async () => readPo((await RAW[locale]()).default).filter((one) => one.id),
    staleTime: Infinity,
  });
  const [search, setSearch] = useState(start);
  const [picked, setPicked] = useState<PoEntry | null>(null);

  const matches = useMemo(() => {
    const words = search.trim().toLowerCase();
    if (!words || !catalog.data) return [];
    return catalog.data
      .filter((one) => one.id.toLowerCase().includes(words) || one.translation.toLowerCase().includes(words))
      .slice(0, SHOWN);
  }, [catalog.data, search]);

  return (
    <Panel
      title={t({ message: "Suggest a better wording", comment: "Title of the panel where a reviewer proposes better words for one message" })}
      onClose={onClose}
      config
    >
      {picked ? (
        <Suggestion household={household} locale={locale} entry={picked} onBack={() => setPicked(null)} />
      ) : (
        <>
          <label className="field">
            <span>
              <Trans comment="Label of the search box in review mode: find a message by its words">
                Find the message, in English or as it reads now
              </Trans>
            </span>
            <input type="search" value={search} onChange={(e) => setSearch(e.target.value)} autoFocus />
          </label>
          <Problem error={catalog.error} />
          {catalog.isLoading && (
            <p className="muted small">
              <Trans comment="Review mode: the language's messages are loading">Reading the messages…</Trans>
            </p>
          )}
          {search.trim() && catalog.data && matches.length === 0 && (
            <p className="muted small">
              <Trans comment="Review mode: no message has these words">No message has those words.</Trans>
            </p>
          )}
          <ul className="plain-list suggest-matches">
            {matches.map((one) => (
              <li key={`${one.context}\u0004${one.id}`}>
                <button type="button" className="link" onClick={() => setPicked(one)}>
                  <strong>{one.translation || one.id}</strong>
                </button>
                <div className="small muted" lang="en">
                  {one.id}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
    </Panel>
  );
}

function Suggestion({
  household,
  locale,
  entry,
  onBack,
}: {
  household: Household;
  locale: string;
  entry: PoEntry;
  onBack: () => void;
}) {
  const client = useQueryClient();
  const [words, setWords] = useState(entry.translation);
  const [note, setNote] = useState("");
  const save = useMutation({
    mutationFn: () =>
      api.post(`/households/${household.id}/translation-suggestions`, {
        locale,
        message: entry.id,
        context: entry.context,
        suggested: words,
        note: note.trim() || null,
      }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["translation-suggestions", household.id] }),
  });
  const keeps = sameShape(words, entry.id);
  // Examples of what to keep, passed in as values so the message's own
  // placeholders and tags stay the catalog's.
  const placeholder = "{0}";
  const tag = "<0>";
  const changed = words.trim() !== "" && words !== entry.translation;

  return (
    <>
      <dl className="suggest-facts">
        <dt>
          <Trans comment="Review mode: label of the message's English source">English</Trans>
        </dt>
        <dd lang="en">{entry.id}</dd>
        {entry.notes.length > 0 && (
          <>
            <dt>
              <Trans comment="Review mode: label of the note written for translators">Note for translators</Trans>
            </dt>
            <dd lang="en">{entry.notes.join(" ")}</dd>
          </>
        )}
        <dt>
          <Trans comment="Review mode: label of the draft translation as it reads now">Now</Trans>
        </dt>
        <dd>{entry.translation}</dd>
      </dl>
      <label className="field">
        <span>
          <Trans comment="Review mode: label of the box for the better translation">Your wording</Trans>
        </span>
        <textarea value={words} rows={3} onChange={(e) => setWords(e.target.value)} />
      </label>
      {!keeps && (
        <p className="banner small" role="alert">
          <Trans comment="Review mode: the suggestion lost a placeholder or a tag; the two examples are written as they are">
            Keep every placeholder and tag the English has, like <code>{placeholder}</code> or <code>{tag}</code>: they
            are where names, numbers and links go.
          </Trans>
        </p>
      )}
      <label className="field">
        <span>
          <Trans comment="Review mode: label of an optional note explaining the suggestion">Why, if it helps (optional)</Trans>
        </span>
        <textarea value={note} rows={2} onChange={(e) => setNote(e.target.value)} />
      </label>
      <Problem error={save.error} />
      {save.isSuccess ? (
        <p className="small" role="status">
          <Trans comment="Review mode: the suggestion was saved">
            Saved. It is in the next download of suggestions, on Application management.
          </Trans>
        </p>
      ) : null}
      <div className="row" style={{ gap: 8 }}>
        <button className="primary" disabled={!keeps || !changed || save.isPending || save.isSuccess} onClick={() => save.mutate()}>
          <Trans comment="Button in review mode: keep this suggestion">Save the suggestion</Trans>
        </button>
        <button type="button" onClick={onBack}>
          <Trans comment="Button in review mode: back to finding another message">Find another message</Trans>
        </button>
      </div>
    </>
  );
}
