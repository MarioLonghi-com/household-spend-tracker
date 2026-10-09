/**
 * Application management's Translations section: review mode (#272).
 *
 * The household reviews the draft languages in the app before #58 ships them.
 * An owner turns review mode on **on this device**; Profile's language picker
 * then offers the drafts as previews, and a "Suggest a better wording" button
 * keeps their better words. Here they are listed, downloaded for
 * `scripts/apply_translation_suggestions.py`, marked applied once the
 * catalogs carry them, or deleted.
 *
 * Off by default. Off, the only thing on this screen is the switch, and
 * nothing anywhere else changes.
 */

import { useMemo, useState, useSyncExternalStore } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { t } from "@lingui/core/macro";
import { useLingui } from "@lingui/react";
import { Trans } from "@lingui/react/macro";
import { api } from "../lib/api";
import { Empty, Problem, SortHeading, useSort } from "../components/bits";
import { DRAFT_LOCALES, onReviewingChange, reviewing, setReviewer } from "../lib/i18n";
import { reviewStored, storeReview } from "../lib/review";
import { sortRows } from "../lib/sorting";
import { formatInstant } from "../lib/time";
import type { Household } from "../lib/types";

export interface TranslationSuggestion {
  id: string;
  locale: string;
  context: string;
  message: string;
  suggested: string;
  note: string | null;
  suggested_by_id: string | null;
  status: "open" | "applied";
  created_at: string;
}

/** A language's name in itself, as the picker names it. */
function ownName(locale: string): string {
  try {
    return new Intl.DisplayNames([locale], { type: "language" }).of(locale) ?? locale;
  } catch {
    return locale;
  }
}

type SuggestionSort = "locale" | "message" | "suggested" | "when" | "status";

export function TranslationReview({ household }: { household: Household }) {
  useLingui();
  const on = useSyncExternalStore(onReviewingChange, reviewing);
  // The stored switch, as well as whether the previews are on: the screen is
  // an owner's, so the two agree here, but the switch is what is shown.
  const [stored, setStored] = useState(reviewStored);

  const turn = (next: boolean) => {
    storeReview(next);
    setStored(next);
    void setReviewer(next);
  };

  return (
    <section className="card" id="translations" aria-labelledby="translations-title">
      <h2 className="section-title" id="translations-title">
        <Trans comment="Heading on the Application management screen: reviewing the draft languages">Translations</Trans>
      </h2>
      <label className="row" style={{ gap: 8, alignItems: "flex-start" }}>
        <input
          type="checkbox"
          checked={stored}
          onChange={(e) => turn(e.target.checked)}
          style={{ width: "auto", marginTop: 3 }}
        />
        <span>
          <strong>
            <Trans comment="Switch on Application management: show the draft languages on this device only">
              Review translations on this device
            </Trans>
          </strong>
          <span className="small muted" style={{ display: "block" }}>
            <Trans>
              Your profile's language picker then offers Português, Español and Svenska as previews: machine
              translated, under review. Nobody else is offered them, and English stays the default everywhere
              else.
            </Trans>
          </span>
        </span>
      </label>
      {on && <Suggestions household={household} />}
    </section>
  );
}

function Suggestions({ household }: { household: Household }) {
  const client = useQueryClient();
  const key = ["translation-suggestions", household.id];
  const base = `/households/${household.id}/translation-suggestions`;
  const list = useQuery({ queryKey: key, queryFn: () => api.get<TranslationSuggestion[]>(base) });
  const changed = () => client.invalidateQueries({ queryKey: key });
  const mark = useMutation({
    mutationFn: (body: { ids: string[]; status: "open" | "applied" }) => api.post(`${base}/status`, body),
    onSuccess: changed,
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.del(`${base}/${id}`),
    onSuccess: changed,
  });
  const { sort, direction, onSort } = useSort<SuggestionSort>("when", "desc");
  const heading = { sort, direction, onSort };
  const rows = useMemo(
    () =>
      sortRows(list.data ?? [], sort, direction, (one, column) => {
        switch (column) {
          case "locale":
            return ownName(one.locale);
          case "message":
            return one.message;
          case "suggested":
            return one.suggested;
          case "when":
            return one.created_at;
          case "status":
            return one.status;
        }
      }),
    [list.data, sort, direction],
  );
  const busy = mark.isPending || remove.isPending;
  const open = (list.data ?? []).filter((one) => one.status === "open").length;

  return (
    <>
      <p className="small" style={{ marginBottom: 6 }}>
        <Trans comment="Application management: how to send the suggestions on; the links follow">
          Download the open suggestions for <code>scripts/apply_translation_suggestions.py</code>:
        </Trans>
      </p>
      <p className="row small" style={{ gap: 12, flexWrap: "wrap", marginTop: 0 }}>
        <a href={`/api${base}/export.json`} download>
          <Trans comment="Link on Application management: download every language's suggestions as one JSON file">
            All languages (JSON)
          </Trans>
        </a>
        {DRAFT_LOCALES.map((locale) => (
          <a key={locale} href={`/api${base}/export.po?locale=${locale}`} download lang={locale}>
            {`${ownName(locale)} (.po)`}
          </a>
        ))}
      </p>
      <Problem error={list.error ?? mark.error ?? remove.error} />
      {list.data && list.data.length === 0 ? (
        <Empty>
          <Trans comment="Application management: nobody has suggested a wording yet">
            No suggestions yet. Choose a preview language in your profile, then use “Suggest a better wording”.
          </Trans>
        </Empty>
      ) : null}
      {rows.length > 0 && (
        <>
          <p className="small muted">
            {t({
              message: `${open} open, ${rows.length - open} applied.`,
              comment: "Application management: how many suggestions are waiting and how many are in the catalogs",
            })}
          </p>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <SortHeading label={t({ message: "Language", comment: "Column heading on Application management's suggestions" })} column="locale" {...heading} />
                  <SortHeading label={t({ message: "English", comment: "Column heading on Application management's suggestions: the message's English source" })} column="message" {...heading} />
                  <SortHeading label={t({ message: "Suggested", comment: "Column heading on Application management's suggestions: the better wording" })} column="suggested" {...heading} />
                  <SortHeading label={t({ message: "When", comment: "Column heading on Application management's suggestions: when it was suggested" })} column="when" {...heading} />
                  <SortHeading label={t({ message: "State", comment: "Column heading on Application management's suggestions: open or applied" })} column="status" {...heading} />
                  <th aria-label={t({ message: "Actions", comment: "Screen-reader name on Application management's suggestions" })} />
                </tr>
              </thead>
              <tbody>
                {rows.map((one) => (
                  <tr key={one.id}>
                    <td data-primary="true" lang={one.locale}>
                      {ownName(one.locale)}
                    </td>
                    <td className="small" lang="en" data-label={t({ message: "English", comment: "Column name shown beside a value on phones on Application management's suggestions" })}>
                      {one.message}
                    </td>
                    <td className="small" lang={one.locale} data-label={t({ message: "Suggested", comment: "Column name shown beside a value on phones on Application management's suggestions" })}>
                      {one.suggested}
                      {one.note ? <div className="muted">{one.note}</div> : null}
                    </td>
                    <td className="small muted" data-label={t({ message: "When", comment: "Column name shown beside a value on phones on Application management's suggestions" })}>
                      {formatInstant(one.created_at)}
                    </td>
                    <td className="small" data-label={t({ message: "State", comment: "Column name shown beside a value on phones on Application management's suggestions" })}>
                      {one.status === "open"
                        ? t({ message: "Open", context: "suggestion state", comment: "State of a suggestion: not yet in the catalogs, so in the next download" })
                        : t({ message: "Applied", context: "suggestion state", comment: "State of a suggestion: the catalogs carry it" })}
                    </td>
                    <td className="row-actions">
                      <button
                        className="link"
                        disabled={busy}
                        onClick={() => mark.mutate({ ids: [one.id], status: one.status === "open" ? "applied" : "open" })}
                      >
                        {one.status === "open"
                          ? t({ message: "Mark applied", comment: "Button on Application management: the catalogs now carry this suggestion" })
                          : t({ message: "Mark open", comment: "Button on Application management: put this suggestion back in the next download" })}
                      </button>
                      <button className="link danger-text" disabled={busy} onClick={() => remove.mutate(one.id)}>
                        <Trans comment="Button on Application management: delete this suggestion">Delete</Trans>
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}
