/**
 * The inbox: receipts that have not been matched to a transaction yet.
 *
 * `transaction_id IS NULL` is the whole of the state. There is no status
 * column and deliberately so -- the previous build had a four-value
 * `ReceiptStatus` of which two values were derivable from the match and one
 * from a score, which is an enum with no designed behaviour and therefore a
 * bug with a menu item.
 *
 * A photo taken at the till days before the statement posts is the *normal*
 * state, not an error.
 */

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import {
  Dialog,
  Empty,
  Field,
  Panel,
  Problem,
  SortHeading,
  Toasts,
  useSort,
  useToasts,
} from "../components/bits";
import { sortRows } from "../lib/sorting";
import { ALL_DATES, DateRange } from "../components/DateRange";
import type { Range } from "../components/DateRange";
import { formatInstant } from "../lib/time";
import {
  Lightbox,
  MoreInfo,
  ReceiptDrop,
  ReceiptFrame,
  ReceiptPeek,
  sizeText,
} from "../components/Receipts";
import { RowPicker } from "../components/RowPicker";
import type { Household, Receipt } from "../lib/types";
import { plural, t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";

type Filter = "unattached" | "attached" | "all";

/** Thumbnails to look through, or rows to read and write notes on. */
type View = "grid" | "list";

/** What the list sorts at its headings. The picture is not one of them: it is
 *  the row, not a fact about the row, and the facts it carries -- when it was
 *  taken, how big it is -- are columns of their own. */
type Column = "taken" | "note" | "size" | "where";

/** The window the picker opens on, around when the photo was taken. The
 *  previous build used the same ten days as a *scoring* input; here it is a
 *  filter on a list a person reads, which is most of the value of automatic
 *  matching for a fraction of the surface area. */
export const PICKER_DAYS = 10;

export function Receipts({ household }: { household: Household }) {
  const client = useQueryClient();
  const [filter, setFilter] = useState<Filter>("unattached");
  const [range, setRange] = useState<Range>(ALL_DATES);
  const [opened, setOpened] = useState<Receipt | null>(null);
  const [view, setView] = useState<View>("grid");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const order = useSort<Column>("taken", "desc");
  const toasts = useToasts();

  const query = useQuery({
    queryKey: ["receipts", household.id, filter],
    queryFn: () =>
      api.get<Receipt[]>(
        `/households/${household.id}/receipts` +
          (filter === "all" ? "" : `?unattached=${filter === "unattached"}`),
      ),
  });

  const rows = useMemo(() => {
    const all = query.data ?? [];
    if (range === ALL_DATES || (!range.since && !range.until)) return all;
    return all.filter((one) => {
      const when = (one.captured_at ?? one.created_at).slice(0, 10);
      if (range.since && when < range.since) return false;
      if (range.until && when > range.until) return false;
      return true;
    });
  }, [query.data, range]);

  /** The list, in the order the headings ask for. The grid keeps the server's
   *  own order -- newest first by when it was taken -- because a grid has no
   *  headings to say otherwise. */
  const ordered = useMemo(
    () =>
      view === "grid"
        ? rows
        : sortRows(rows, order.sort, order.direction, (one, column) =>
            column === "note"
              ? (one.note ?? "")
              : column === "size"
                ? one.download_bytes
                : column === "where"
                  ? one.transaction_id
                    ? 1
                    : 0
                  : (one.captured_at ?? one.created_at),
          ),
    [rows, view, order.sort, order.direction],
  );

  function refresh() {
    void client.invalidateQueries({ queryKey: ["receipts"] });
    void client.invalidateQueries({ queryKey: ["register"] });
  }

  function toggle(id: string) {
    setPicked((was) => {
      const next = new Set(was);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  // Only what is on screen. "Select all" that quietly took in rows a filter is
  // hiding is how a delete confirmation says six and removes forty.
  const allShown = ordered.length > 0 && ordered.every((one) => picked.has(one.id));
  function toggleAll() {
    setPicked(allShown ? new Set() : new Set(ordered.map((one) => one.id)));
  }

  /**
   * Many receipts, one act.
   *
   * Through a route of its own rather than a loop over `DELETE /receipts/{id}`:
   * a loop is one batch each, so putting a selection of six back would be six
   * undos in the right order, and the History screen would show six lines for
   * one gesture.
   */
  const removeMany = useMutation({
    mutationFn: (ids: string[]) =>
      api.post<string[]>(`/households/${household.id}/receipts/bulk-delete`, {
        receipt_ids: ids,
      }),
    onSuccess: (gone) => {
      setConfirming(false);
      setPicked(new Set());
      toasts.say(
        plural(gone.length, {
          one: "1 receipt deleted. The History screen can put it back.",
          other: `${gone.length} receipts deleted, as one act. The History screen can put them all back.`,
        }),
      );
      refresh();
    },
  });

  return (
    <div>
      <h1><Trans>Receipts</Trans></h1>
      <p className="muted small">
        <Trans>
          Anything without a transaction waits here. Photograph a receipt at the till and
          match it when the statement arrives — that is the ordinary way round, not a
          mistake to be cleared.
        </Trans>
      </p>

      <div className="card">
        <Problem error={query.error ?? removeMany.error} />
        {/* Filters and views on the left, the way in on the right -- the add
            button is pushed there by `margin-left: auto` rather than by a
            space-between that would drift as the filter row changes width. */}
        <div className="receipts-bar">
          <div className="row" style={{ gap: 6 }}>
            {(["unattached", "attached", "all"] as Filter[]).map((one) => (
              <button
                key={one}
                className={filter === one ? "primary" : undefined}
                aria-pressed={filter === one}
                // The selection goes with the list it was made on. Carrying it
                // across a filter is how a confirmation that says six deletes
                // rows nobody can see.
                onClick={() => {
                  setFilter(one);
                  setPicked(new Set());
                }}
              >
                {one === "unattached" ? t`Inbox` : one === "attached" ? t`Matched` : t`All`}
              </button>
            ))}
          </div>
          <div className="row" style={{ gap: 6 }}>
            {(["grid", "list"] as View[]).map((one) => (
              <button
                key={one}
                className={view === one ? "primary" : undefined}
                aria-pressed={view === one}
                onClick={() => setView(one)}
              >
                {one === "grid" ? t`Thumbnails` : t`List`}
              </button>
            ))}
          </div>
          <ReceiptDrop
            target={{ householdId: household.id }}
            label={t`Add receipts`}
            icon="＋"
            primary
            onDone={refresh}
          />
        </div>

        {picked.size > 0 && (
          <div className="banner info">
            <div>
              <Trans>
                <strong>{picked.size} selected.</strong> Deleting them is one act, so one undo
                brings the whole selection back.
              </Trans>
            </div>
            <div className="row" style={{ marginTop: 8, gap: 8 }}>
              <button
                className="danger"
                disabled={removeMany.isPending}
                onClick={() => setConfirming(true)}
              >
                {plural(picked.size, {
                  one: "Delete 1 receipt",
                  other: `Delete ${picked.size} receipts`,
                })}
              </button>
              <button className="link" onClick={() => setPicked(new Set())}>
                <Trans>
                  Clear selection
                </Trans>
              </button>
            </div>
          </div>
        )}

        <DateRange value={range} onChange={setRange} />

        {rows.length === 0 ? (
          <Empty>
            {filter === "unattached"
              ? t`Nothing waiting. Every receipt is on a transaction.`
              : t`No receipts here yet.`}
          </Empty>
        ) : view === "grid" ? (
          <div className="receipt-grid">
            {rows.map((one) => {
              const when = (one.captured_at ?? one.created_at).slice(0, 10);
              return (
                // A div rather than one big button: the card holds a checkbox
                // *and* a way in, and a button inside a button is neither.
                <div
                  key={one.id}
                  className={picked.has(one.id) ? "receipt-card chosen" : "receipt-card"}
                >
                  <label className="card-pick">
                    <input
                      type="checkbox"
                      checked={picked.has(one.id)}
                      onChange={() => toggle(one.id)}
                    />
                    <span className="sr-only">
                      <Trans>Select the receipt from {when}</Trans>
                    </span>
                  </label>
                  <button
                    type="button"
                    className="card-open"
                    onClick={() => setOpened(one)}
                    aria-label={t`Open the receipt from ${when}`}
                  >
                    <img src={`/api/receipts/${one.id}/thumb`} alt="" loading="lazy" />
                  </button>
                  <span className="small muted">{when}</span>
                  <span className="small muted">{sizeText(one.download_bytes)}</span>
                </div>
              );
            })}
          </div>
        ) : (
          <div className="table-scroll">
            <table className="receipt-list">
              <thead>
                <tr>
                  <th className="pick-column">
                    <input
                      type="checkbox"
                      checked={allShown}
                      onChange={toggleAll}
                      aria-label={t`Select every receipt shown`}
                    />
                  </th>
                  {/* The picture is the row, not a fact about it, so it has
                      nothing to sort by -- the facts it carries are the three
                      headings after it. */}
                  <th className="thumb-column">
                    <span className="sr-only">
                      <Trans>Receipt</Trans>
                    </span>
                  </th>
                  <SortHeading
                    label={t`Taken`}
                    column="taken"
                    sort={order.sort}
                    direction={order.direction}
                    onSort={order.onSort}
                  />
                  <SortHeading
                    label={t`Receipt notes`}
                    column="note"
                    sort={order.sort}
                    direction={order.direction}
                    onSort={order.onSort}
                  />
                  <SortHeading
                    label={t`Size`}
                    column="size"
                    sort={order.sort}
                    direction={order.direction}
                    onSort={order.onSort}
                    align="right"
                  />
                  <SortHeading
                    label={t`Where`}
                    column="where"
                    sort={order.sort}
                    direction={order.direction}
                    onSort={order.onSort}
                  />
                </tr>
              </thead>
              <tbody>
                {ordered.map((one) => (
                  <tr
                    key={one.id}
                    className={picked.has(one.id) ? "row-pick selected" : "row-pick"}
                  >
                    <td data-select="true">
                      <input
                        type="checkbox"
                        checked={picked.has(one.id)}
                        onChange={() => toggle(one.id)}
                        aria-label={t`Select the receipt from ${(one.captured_at ?? one.created_at).slice(0, 10)}`}
                        style={{ width: "auto" }}
                      />
                    </td>
                    <td className="thumb-column" data-label={t`Receipt`}>
                      <ReceiptPeek receipt={one} onOpen={() => setOpened(one)} />
                    </td>
                    <td data-label={t`Taken`} data-detail-first="true">
                      {(one.captured_at ?? one.created_at).slice(0, 10)}
                      {one.captured_at ? null : (
                        <span className="muted small"> {t`(uploaded)`}</span>
                      )}
                    </td>
                    <td data-label={t`Receipt notes`} data-primary="true">
                      <NoteCell
                        receipt={one}
                        onSaved={() => {
                          toasts.say(t`Receipt notes saved.`);
                          void client.invalidateQueries({ queryKey: ["receipts"] });
                        }}
                      />
                    </td>
                    <td className="amount" data-figure="true">
                      {sizeText(one.download_bytes)}
                    </td>
                    <td data-label={t`Where`} className="small muted">
                      {one.transaction_id ? t`On a transaction` : t`Inbox`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {confirming && (
        <Dialog title={t`Delete these receipts?`} onClose={() => setConfirming(false)}>
          <p>
            <strong>
              {plural(picked.size, {
                one: "One receipt will be deleted.",
                other: `${picked.size} receipts will be deleted.`,
              })}
            </strong>{" "}
            {plural(picked.size, {
              one: "It goes as one act, so the History screen puts it back in one undo.",
              other: "They go as one act, so the History screen puts them all back in one undo.",
            })}{" "}
            {t`The picture itself is kept for a day after nothing points at it, which is what makes that undo work.`}
          </p>
          <div className="dialog-choices">
            <button
              className="danger"
              disabled={removeMany.isPending}
              onClick={() => removeMany.mutate([...picked])}
            >
              {removeMany.isPending
                ? t`Deleting…`
                : plural(picked.size, { one: "Delete it", other: `Delete all ${picked.size}` })}
            </button>
            <button onClick={() => setConfirming(false)}>
              <Trans>Keep them</Trans>
            </button>
          </div>
          <Problem error={removeMany.error} />
        </Dialog>
      )}

      <Toasts items={toasts.items} onDone={toasts.dismiss} />

      {opened && (
        <ReceiptPanel
          household={household}
          receipt={opened}
          onClose={() => setOpened(null)}
          onChanged={() => {
            setOpened(null);
            refresh();
          }}
        />
      )}
    </div>
  );
}

/**
 * The receipt's own free text, edited where it is read.
 *
 * This is `note`, the receipt's one free-text field -- the same box the panel
 * shows beside the picture and the capture page offers at the till. It is not
 * the transaction's memo: an inbox receipt has no transaction, and inventing
 * one to hang a memo on is how the inbox stops meaning "evidence waiting for
 * its row".
 *
 * Saved on the way out of the field, like every other cell in this app that
 * saves without a button. The draft is held here rather than read back from
 * the row, so a refetch landing mid-sentence does not take the words away.
 */
function NoteCell({
  receipt,
  onSaved,
}: {
  receipt: Receipt;
  onSaved: (text: string) => void;
}) {
  const [text, setText] = useState(receipt.note ?? "");
  //: What the server last confirmed. Separate from the draft, so a failed save
  //: can be retried by leaving the field again with the same words in it.
  const [saved, setSaved] = useState(receipt.note ?? "");

  const save = useMutation({
    mutationFn: (value: string) =>
      api.patch<Receipt>(
        `/receipts/${receipt.id}`,
        value ? { note: value } : { clear_note: true },
      ),
    onSuccess: (_row, value) => {
      setSaved(value);
      onSaved(value);
    },
  });

  return (
    <span className="note-cell">
      <input
        value={text}
        onChange={(event) => setText(event.target.value)}
        onBlur={() => {
          const value = text.trim();
          if (value !== saved) save.mutate(value);
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter") event.currentTarget.blur();
        }}
        placeholder={t`What was it for?`}
        aria-label={t`Receipt notes`}
      />
      {save.isPending ? (
        <span className="small muted">
          <Trans>saving…</Trans>
        </span>
      ) : save.isError ? (
        <span className="small danger-text">
          <Trans>not saved — leave the field to try again</Trans>
        </span>
      ) : null}
    </span>
  );
}

function ReceiptPanel({
  household,
  receipt,
  onClose,
  onChanged,
}: {
  household: Household;
  receipt: Receipt;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [note, setNote] = useState(receipt.note ?? "");
  const [enlarged, setEnlarged] = useState(false);
  const [matching, setMatching] = useState(false);

  const save = useMutation({
    mutationFn: () =>
      api.patch<Receipt>(`/receipts/${receipt.id}`, {
        note: note.trim() || null,
        clear_note: !note.trim(),
      }),
    onSuccess: onChanged,
  });

  const remove = useMutation({
    mutationFn: () => api.del(`/receipts/${receipt.id}`),
    onSuccess: onChanged,
  });

  const detach = useMutation({
    mutationFn: () => api.patch(`/receipts/${receipt.id}`, { detach: true }),
    onSuccess: onChanged,
  });

  return (
    <Panel title={receipt.download_name.split("/").pop() ?? t`Receipt`} onClose={onClose}>
      <Problem error={save.error ?? remove.error ?? detach.error} />

      <ReceiptFrame receipt={receipt} onEnlarge={() => setEnlarged(true)} />

      <p className="small muted" style={{ marginTop: 8 }}>
        {sizeText(receipt.download_bytes)} ·{" "}
        <a href={`/api/receipts/${receipt.id}/${receipt.has_original ? "original" : "display"}`}>
          <Trans>
            Download
          </Trans>
        </a>
        {receipt.uploaded_by_name ? ` · ${t`added by ${receipt.uploaded_by_name}`}` : null}
      </p>

      {/* It matters more here than in the register: an inbox receipt has no
          payee, no amount and no transaction, so where and when it was taken is
          very often the only thing that identifies it. */}
      <MoreInfo receipt={receipt} />

      <Field label={t`Receipt notes`}>
        <textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
      </Field>

      <div className="row" style={{ marginTop: 14 }}>
        <button className="primary" disabled={save.isPending} onClick={() => save.mutate()}>
          <Trans>
            Save receipt notes
          </Trans>
        </button>
        {receipt.transaction_id ? (
          <button disabled={detach.isPending} onClick={() => detach.mutate()}>
            <Trans>
              Send back to the inbox
            </Trans>
          </button>
        ) : (
          <button onClick={() => setMatching(true)}>
            <Trans>Attach to a transaction</Trans>
          </button>
        )}
        <button className="danger" disabled={remove.isPending} onClick={() => remove.mutate()}>
          <Trans>
            Delete
          </Trans>
        </button>
      </div>

      <p className="small muted" style={{ marginTop: 10 }}>
        <Trans>
          Deleting is recorded and can be put back from the History screen. The picture
          itself is kept for a day after nothing points at it, which is what makes that
          undo work.
        </Trans>
      </p>

      {matching && (
        <Matcher
          household={household}
          receipt={receipt}
          onClose={() => setMatching(false)}
          onDone={onChanged}
        />
      )}
      {enlarged && <Lightbox receipt={receipt} onClose={() => setEnlarged(false)} />}
    </Panel>
  );
}

/**
 * Pick the transaction this receipt belongs to.
 *
 * Opens on a ten-day window around `captured_at`, which is the first concrete
 * return on parsing the metadata: a month of receipts uploaded in one batch
 * all share an upload time and nothing else, and when they were *taken* is the
 * order the statement will arrive in.
 *
 * Exported for its test, which holds the window at ten days now that the
 * picker itself is shared with the register's "Find the payment…".
 */
export function Matcher({
  household,
  receipt,
  onClose,
  onDone,
}: {
  household: Household;
  receipt: Receipt;
  onClose: () => void;
  onDone: () => void;
}) {
  const attach = useMutation({
    mutationFn: (transactionId: string) =>
      api.patch(`/receipts/${receipt.id}`, { transaction_id: transactionId }),
    onSuccess: onDone,
  });

  return (
    <RowPicker
      household={household}
      title={t`Which transaction?`}
      anchor={receipt.captured_at ?? receipt.created_at}
      days={PICKER_DAYS}
      intro={
        receipt.captured_at
          ? t`Taken ${formatInstant(receipt.captured_at)}, so this opens on the ten days either side.`
          : t`This receipt carries no date of its own, so the window is around when it was uploaded.`
      }
      action={t`Attach`}
      busy={attach.isPending}
      error={attach.error}
      onPick={(txn) => attach.mutate(txn.id)}
      onClose={onClose}
    />
  );
}
