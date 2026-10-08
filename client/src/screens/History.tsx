/** What has been done to this household, and putting it back. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "../lib/api";
import { Actor, Empty, Panel, Problem, SortHeading, sortRows, useSort } from "../components/bits";
import { formatInstant } from "../lib/time";
import { useWindowed } from "../lib/useWindowed";
import type { Batch, BatchDetail, Household } from "../lib/types";
import { plural, t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";
import { formatCount } from "../lib/locale";

type HistorySort = "when" | "what" | "by" | "state" | "rows";

export function History({ household }: { household: Household }) {
  const client = useQueryClient();
  const [everything, setEverything] = useState(false);
  const [opened, setOpened] = useState<Batch | null>(null);

  const batches = useQuery({
    queryKey: ["batches", household.id, everything],
    queryFn: () =>
      api.get<Batch[]>(
        `/households/${household.id}/batches?include_single_edits=${everything}`,
      ),
  });

  //: Newest first, which is the order the server sends and the order anyone
  //: opening this screen is looking for.
  const { sort, direction, onSort } = useSort<HistorySort>("when", "desc");

  const entries = useMemo(
    () =>
      sortRows(
        batches.data ?? [],
        sort,
        direction,
        (entry, column) => {
          switch (column) {
            case "when":
              return entry.started_at;
            case "what":
              // The headline is the kind of act; the detail is what it did to
              // this household. Sorting on the pair groups the imports
              // together and then orders inside the group.
              return [entry.headline, entry.detail];
            case "by":
              // The person first, the program second: an agent acting for
              // somebody is still their act, so sorting by who groups it with
              // the rest of theirs rather than filing it under the robot.
              return [entry.actor_name ?? "", entry.via ?? ""];
            case "state":
              return entry.status;
            default:
              return entry.change_count;
          }
        },
        // Ties fall back to newest first, so a column of equal states still
        // reads chronologically rather than in whatever order it arrived.
        (a, b) => b.started_at.localeCompare(a.started_at),
      ),
    [batches.data, sort, direction],
  );

  const page = useWindowed(entries);

  const refresh = () => {
    client.invalidateQueries({ queryKey: ["batches", household.id] });
    client.invalidateQueries({ queryKey: ["register", household.id] });
    client.invalidateQueries({ queryKey: ["accounts", household.id] });
    client.invalidateQueries({ queryKey: ["categories", household.id] });
  };

  return (
    <>
      <h1><Trans comment="Screen title on the History screen. See GLOSSARY.md">History</Trans></h1>
      <p className="muted small">
        <Trans>
          Everything that has changed this household, grouped by the act that changed it. An import
          of two hundred rows is one entry here, and undoing it puts all two hundred back.
        </Trans>
      </p>

      <Problem error={batches.error} />

      <div className="card">
        <label className="small" style={{ display: "block", marginBottom: 12 }}>
          <input
            type="checkbox"
            checked={everything}
            onChange={(e) => setEverything(e.target.checked)}
            style={{ width: "auto", marginRight: 8 }}
          />
          <Trans>
            Include single edits made in the register
          </Trans>
        </label>

        {entries.length === 0 ? (
          <Empty><Trans>Nothing has happened here yet.</Trans></Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <SortHeading
                    label={t({ message: "When", comment: "Column heading on the History screen: noun, the time" })}
                    column="when"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                  />
                  <SortHeading
                    label={t({ message: "What happened", comment: "Column heading on the History screen" })}
                    column="what"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                  />
                  <SortHeading
                    label={t({ message: "By", comment: "Column heading on the History screen: preposition, done by a person" })}
                    column="by"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                  />
                  <SortHeading
                    label={t({ message: "State", comment: "Column heading on the History screen: noun, a row's status" })}
                    column="state"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                  />
                  {/* It was unlabelled and holds "17 rows", which is a fact
                      about the batch and therefore something to sort on: the
                      big imports are what you go looking for. */}
                  <SortHeading
                    label={t({ message: "Rows", comment: "Column heading on the History screen: noun, lines of a file or table" })}
                    column="rows"
                    sort={sort}
                    direction={direction}
                    onSort={onSort}
                    align="right"
                  />
                </tr>
              </thead>
              <tbody>
                {page.visible.map((entry) => (
                  <tr key={entry.id}>
                    <td
                      className="small mono"
                      style={{ whiteSpace: "nowrap" }}
                      data-label={t({ message: "When", comment: "Column name shown beside a value on phones on the History screen: noun, the time" })}
                      data-detail-first="true"
                    >
                      {formatInstant(entry.started_at)}
                    </td>
                    {/* The whole sentence opens it. The row is the thing you
                        want to know more about, so the row is the target --
                        rather than a "details" link you have to aim at. */}
                    {/* The sentence is the row, so on a phone it is the
                        headline -- the timestamp and the actor are what you
                        read once you have found the act you meant. */}
                    <td className="editable" data-primary="true">
                      <button
                        type="button"
                        className="cell-edit"
                        title={t`Everything recorded about this`}
                        onClick={() => setOpened(entry)}
                      >
                        <span className="small muted">{entry.headline}</span>
                        <span style={{ display: "block" }}>{entry.detail}</span>
                        {entry.source?.filename ? (
                          <span className="small muted mono" style={{ display: "block" }}>
                            {String(entry.source.filename)}
                          </span>
                        ) : null}
                      </button>
                    </td>
                    <td className="small muted" data-label={t({ message: "By", comment: "Column name shown beside a value on phones on the History screen: preposition, done by a person" })}>
                      <Actor name={entry.actor_name} via={entry.via} />
                    </td>
                    <td>
                      <span className="pill">{statusWord(entry.status)}</span>
                    </td>
                    <td className="amount small muted" style={{ whiteSpace: "nowrap" }}>
                      {plural(entry.change_count, { one: "1 row", other: `${entry.change_count} rows` })}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!page.allShown && (
              <button type="button" className="more-rows" ref={page.sentinelRef} onClick={page.extend}>
                {t`Showing ${formatCount(page.shown)} of ${formatCount(page.total)} — show more`}
              </button>
            )}
          </div>
        )}
      </div>

      {opened && (
        <BatchPanel
          household={household}
          batch={opened}
          onClose={() => setOpened(null)}
          onUndone={() => {
            setOpened(null);
            refresh();
          }}
        />
      )}
    </>
  );
}

/**
 * Everything recorded about one act.
 *
 * The list answers "what happened". This answers "what exactly" — which is
 * most of the reason anybody opens an audit log at all. Every column that
 * moved, both sides of it, the whole row for an insert or a delete, what the
 * file was, what the counts were, and which columns were deliberately kept out.
 *
 * The undo lives here too, and still asks twice. The first press only reveals
 * the second: the failure being guarded against is a click landing where it was
 * not aimed, and two targets in one gesture is exactly what a stray click
 * crosses.
 */
/** A batch's state, as the list and the panel name it. */
function statusWord(status: string): string {
  switch (status) {
    case "applied":
      return t({ message: "applied", comment: "Label on the History screen: written to the ledger" });
    case "undone":
      return t({ message: "undone", comment: "Label on the History screen: taken back with undo" });
    case "preview":
      return t({ message: "preview", comment: "Label on the History screen: noun or step name: what would happen, not yet done" });
    case "running":
      return t({ message: "running", comment: "Label on the History screen" });
    case "failed":
      return t({ message: "failed", comment: "Label on the History screen: it did not work" });
    default:
      return status;
  }
}

function BatchPanel({
  household,
  batch,
  onClose,
  onUndone,
}: {
  household: Household;
  batch: Batch;
  onClose: () => void;
  onUndone: () => void;
}) {
  const [sure, setSure] = useState(false);

  const detail = useQuery({
    queryKey: ["batch", household.id, batch.id],
    queryFn: () => api.get<BatchDetail>(`/households/${household.id}/batches/${batch.id}`),
  });

  const undo = useMutation({
    mutationFn: () => api.post(`/households/${household.id}/batches/${batch.id}/undo`),
    onSuccess: onUndone,
  });

  const changes = detail.data?.changed_rows ?? [];
  const count = detail.data?.change_count ?? batch.change_count;
  const source = (batch.source ?? {}) as Record<string, unknown>;
  const summary = (batch.summary ?? {}) as Record<string, number>;

  return (
    <Panel title={batch.headline} onClose={onClose} wide>
      <Problem error={detail.error ?? undo.error} />

      <dl className="facts">
        <dt><Trans comment="Name of a fact on the History screen: noun, the time">When</Trans></dt>
        <dd>{formatInstant(batch.started_at)}</dd>
        <dt><Trans comment="Name of a fact on the History screen: preposition, done by a person">By</Trans></dt>
        <dd>
          <Actor name={batch.actor_name} via={batch.via} />
        </dd>
        <dt><Trans comment="Name of a fact on the History screen: noun, a row's status">State</Trans></dt>
        <dd>
          <span className="pill">{statusWord(batch.status)}</span>
          {batch.undone_by_id ? <span className="small muted"> — {t({ message: "undone later", comment: "Value of a fact on the History screen" })}</span> : null}
        </dd>
        <dt><Trans comment="Name of a fact on the History screen">Rows touched</Trans></dt>
        <dd>{count}</dd>
        {Object.entries(source).map(([key, value]) => (
          <div key={key} style={{ display: "contents" }}>
            <dt>{key.replace(/_/g, " ")}</dt>
            <dd className="mono small" style={{ wordBreak: "break-all" }}>
              {typeof value === "object" ? JSON.stringify(value) : String(value)}
            </dd>
          </div>
        ))}
        {Object.entries(summary)
          .filter(([, value]) => value > 0)
          .map(([key, value]) => (
            <div key={key} style={{ display: "contents" }}>
              <dt>{key.replace(/_/g, " ")}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        <dt><Trans comment="Name of a fact on the History screen: noun, one act in History, undone as a whole">Batch</Trans></dt>
        <dd className="mono small" style={{ wordBreak: "break-all" }}>
          {batch.id}
        </dd>
      </dl>

      <p style={{ marginTop: 14 }}>{batch.detail}</p>

      <hr className="rule" />
      <h3 className="section-title"><Trans>Every row it changed</Trans></h3>
      {changes.length > 0 && changes.length < count && (
        <p className="muted small" style={{ marginTop: 0 }}>
          {t`The first ${changes.length} of ${count}, in the order they were changed. Undo puts back all ${count}.`}
        </p>
      )}

      {detail.isLoading ? (
        <p className="muted small"><Trans>Reading the log…</Trans></p>
      ) : changes.length === 0 ? (
        <p className="muted small" style={{ margin: 0 }}>
          <Trans>
            Nothing recorded against this one.
          </Trans>
        </p>
      ) : (
        <ol className="changes">
          {changes.map((change) => (
            <li key={change.seq}>
              <div className="change-head">
                <span className={`tag op-${change.op}`}>{change.op}</span>
                <span className="small">{change.summary}</span>
              </div>

              {change.fields.length > 0 && (
                <table className="change-fields">
                  <tbody>
                    {change.fields.map((field) => (
                      <tr key={field.field}>
                        <th scope="row">{field.field}</th>
                        <td className="muted">{field.was}</td>
                        <td aria-hidden="true">→</td>
                        <td>{field.now}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}

              {change.snapshot.length > 0 && (
                <details>
                  <summary className="small muted">
                    {change.op === "insert"
                      ? t`The whole row as it was written`
                      : t`The whole row as it was before it went`}
                  </summary>
                  <table className="change-fields">
                    <tbody>
                      {change.snapshot.map((field) => (
                        <tr key={field.field}>
                          <th scope="row">{field.field}</th>
                          <td colSpan={3}>{field.now}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </details>
              )}

              {change.redacted.length > 0 && (
                <p className="small muted" style={{ margin: "6px 0 0" }}>
                  {t`Kept out of the log: ${change.redacted.join(", ")}. Absent rather than starred over — an undo writes back what is stored, and "***" would become the password.`}
                </p>
              )}

              <p className="small muted mono" style={{ margin: "6px 0 0", wordBreak: "break-all" }}>
                {change.table} {change.row_id}
              </p>
            </li>
          ))}
        </ol>
      )}

      {batch.status === "applied" && (
        <>
          <hr className="rule" />
          <div className="banner warn">
            {count === 1 ? (
              <Trans>
                Undoing puts back <strong>{count}</strong> row exactly as they were before. Anything
                changed since is overwritten, and undoing the undo is the only way back.
              </Trans>
            ) : (
              <Trans>
                Undoing puts back <strong>{count}</strong> rows exactly as they were before.
                Anything changed since is overwritten, and undoing the undo is the only way back.
              </Trans>
            )}
          </div>
          {!sure ? (
            <div className="row">
              <button className="danger" disabled={detail.isLoading} onClick={() => setSure(true)}>
                <Trans comment="Button on the History screen">
                  Undo this
                </Trans>
              </button>
              <button onClick={onClose}><Trans>Leave it alone</Trans></button>
            </div>
          ) : (
            <div className="confirm-again">
              <p style={{ margin: 0 }}>
                {count === 1 ? (
                  <Trans>
                    <strong>Last check.</strong> {count} row goes back to how they were{" "}
                    {formatInstant(batch.started_at)}.
                  </Trans>
                ) : (
                  <Trans>
                    <strong>Last check.</strong> {count} rows go back to how they were{" "}
                    {formatInstant(batch.started_at)}.
                  </Trans>
                )}
              </p>
              <div className="row" style={{ marginTop: 10 }}>
                <button className="danger" disabled={undo.isPending} onClick={() => undo.mutate()}>
                  {undo.isPending ? t`Putting it back…` : t`Yes, undo it`}
                </button>
                <button onClick={() => setSure(false)}>
                  <Trans>No, go back</Trans>
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </Panel>
  );
}
