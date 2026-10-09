/**
 * Application management: the instance, rather than any ledger inside it.
 *
 * **Owner-only, and the server is what enforces it** — every call on this
 * screen is a 403 for a member, so keeping the nav entry out of a member's
 * menu is a courtesy exactly as it is for the rest of the Admin section.
 *
 * Five blocks, in the order somebody asks the questions. The first two used
 * to be one **What this is** block that mixed the process and the ledger in a
 * single list, with the actions on each somewhere else again — so neither
 * question could be answered by pointing at a section (#52).
 *
 * - **Runtime** — what is running: version, environment, Python, machine,
 *   process id, when it started, and where it can be reached.
 * - **Log files** — what it has been writing, and the end of any one of them.
 * - **Database** — what it is serving: engine and version, journal mode,
 *   schema revision, size and pages, where the file is, how much of it is
 *   which household — and backing it up, which is an action on *this*.
 * - **Updates** — what is about the installation rather than about either
 *   of the above: is there a newer version, and installing it (#166).
 * - **About** — where the code and its author live.
 *
 * Nothing here is polled. Every figure is read when the screen opens and after
 * an operation that would move it; a page that re-reads the size of a database
 * every few seconds is a page that is itself the load.
 */

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useIsFetching, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Empty, Hint, Problem, SortHeading, sortRows, useSort } from "../components/bits";
import { bytes } from "../lib/bytes";
import { BackupList, UpdateBackupList, type Backup } from "./Backups";
import { Updates } from "./Updates";
import { formatInstant } from "../lib/time";
import { plural, t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";
import { formatCount } from "../lib/locale";

// --------------------------------------------------------------------------- //
// What the server sends. `InstanceOut` and friends in `app/schemas.py`.
// --------------------------------------------------------------------------- //

interface Place {
  what: string;
  path: string;
  exists: boolean;
  bytes: number | null;
  note: string;
  /** Being absent is a normal state for this one, so it is not flagged. */
  optional: boolean;
}

interface HouseholdData {
  id: string;
  name: string;
  transactions: number;
  receipts: number;
}

interface Size {
  total_bytes: number;
  main_bytes: number;
  wal_bytes: number;
  page_size: number | null;
  page_count: number | null;
  free_pages: number | null;
}

interface Package {
  name: string;
  version: string;
}

interface LogFile {
  name: string;
  bytes: number;
  modified: string;
}

interface LoggingStyle {
  key: string;
  label: string;
  blurb: string;
  echo_sql: boolean;
}

/** One of the three files the instance writes. */
interface LogStream {
  key: string;
  label: string;
  filename: string;
  blurb: string;
  /** True for sql.log, which at the “with SQL” style holds every statement and
   *  every row it returned — transactions, payees and amounts in plain text.
   *  Marked here so nobody finds out after emailing it to someone. */
  holds_ledger_values: boolean;
}

interface LoggingState {
  current: string;
  styles: LoggingStyle[];
  files: LogFile[];
  directory: string;
  streams: LogStream[];
}

interface DatabaseEngine {
  name: string;
  version: string | null;
  journal_mode: string | null;
  path: string | null;
}

// Which commit the process is running. `BuildOut` in `app/schemas.py`, and
// `app/build.py` for where each field comes from.
interface Build {
  commit: string | null;
  branch: string | null;
  committed_at: string | null;
  dirty: boolean | null;
  source: string;
}

interface Instance {
  app_name: string;
  version: string;
  build: Build;
  environment: string;
  python: string;
  platform: string;
  schema_revision: string | null;
  started_at: string | null;
  process_id: number;
  database_url_scheme: string;
  engine: DatabaseEngine;
  size: Size;
  households: HouseholdData[];
  places: Place[];
  packages: Package[];
  addresses: string[];
  latest_backup: Backup | null;
  logging_style: string;
  logs: LogFile[];
  repository: string;
  author: string;
}

const count = (n: number) => formatCount(n);

/**
 * Scroll `target` to the top, and for the next few frames scroll it back
 * whenever it has moved. The rows of every table skip layout while off screen
 * (`content-visibility: auto` in styles.css) and stand in at a guessed
 * height; on a phone the backups lists above the Updates section took their
 * real height a frame after the scroll and pushed it back below the fold.
 */
function bringIntoView(target: HTMLElement, frames = 20): void {
  target.scrollIntoView({ block: "start" });
  let settled = target.getBoundingClientRect().top;
  let left = frames;
  const watch = () => {
    if (!target.isConnected || --left < 0) return;
    if (target.getBoundingClientRect().top !== settled) {
      target.scrollIntoView({ block: "start" });
      settled = target.getBoundingClientRect().top;
    }
    requestAnimationFrame(watch);
  };
  requestAnimationFrame(watch);
}

export function ApplicationManagement({
  section = null,
  onSectionShown,
}: {
  /**
   * A section to scroll into view once the screen has drawn: `updates` when
   * the page was opened by the Updating panel after an update or a rollback,
   * so its outcome is seen without scrolling. Null on a visit from the menu.
   */
  section?: "updates" | null;
  /** Called once it has scrolled, so it happens once. */
  onSectionShown?: () => void;
} = {}) {
  const client = useQueryClient();
  const it = useQuery({
    queryKey: ["application"],
    queryFn: () => api.get<Instance>("/admin/application"),
  });
  const shown = useRef(onSectionShown);
  shown.current = onSectionShown;
  // Not as soon as the instance arrives: the logs, the backups and the
  // update state load after it, and the backups above the section pushed it
  // back below the fold. So once nothing on the page is still being fetched
  // -- asked of the client here rather than from the render's count, because
  // the sections start their requests in the same commit.
  const drawn = Boolean(it.data);
  const fetching = useIsFetching();
  useEffect(() => {
    if (section !== "updates" || !drawn) return;
    if (client.isFetching() > 0) return;
    const target = document.getElementById("updates");
    if (target) bringIntoView(target);
    shown.current?.();
  }, [section, drawn, fetching, client]);

  const refresh = () => client.invalidateQueries({ queryKey: ["application"] });

  if (it.isLoading)
    return (
      <div className="card muted">
        <Trans comment="Application management: loading">Reading this instance…</Trans>
      </div>
    );
  if (it.isError)
    return (
      <div className="card">
        <Problem error={it.error} />
      </div>
    );
  const me = it.data!;

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 4 }}>
        <h1><Trans comment="Screen title on the Application management screen. See GLOSSARY.md">Application management</Trans></h1>
      </div>
      <p className="muted small" style={{ marginTop: 0 }}>
        <Trans>
          This installation, rather than any of the ledgers in it. Only an owner can open this
          screen, and only an owner can reach anything on it — the paths, the logs and the counts
          below are about every household on this instance.
        </Trans>
      </p>

      <Runtime me={me} />
      <Logs current={me.logging_style} onChanged={refresh} />
      <Database me={me} onChanged={refresh} />
      <Operations me={me} />
      <About me={me} />
    </>
  );
}

// --------------------------------------------------------------------------- //
// Runtime
// --------------------------------------------------------------------------- //
//
// This screen used to open with one "What this is" block that mixed two
// unrelated subjects in a single `dl`: the process (version, environment,
// Python, machine, started) and the database (schema, bytes, pages, free
// pages). The actions on each were in a third place, under Operations. So
// "what am I running" and "is the ledger healthy" -- both asked under
// pressure -- were answered by reading across four blocks.
//
// Two sections now, each with its facts and its actions together. Issue #52.

function Runtime({ me }: { me: Instance }) {
  return (
    <section className="card">
      <h2 className="section-title"><Trans comment="Heading on the Application management screen: noun, the running program">Runtime</Trans></h2>
      <p className="muted small" style={{ marginTop: 0 }}>
        <Trans comment="Application management: describes the running server process">
          The process answering this request.
        </Trans>
      </p>
      <dl className="stat-grid application-facts">
        <Fact label={t({ message: "Version", comment: "Name of a fact on the Application management screen: noun, the software version" })} value={me.version} note={me.app_name} />
        {/* The version only moves at a release, so main and a dev far ahead of
            it both say the same thing. The commit is what tells them apart. */}
        <Fact
          label={t({ message: "Commit", comment: "Name of a fact on the Application management screen: noun, the git commit the software was built from" })}
          value={me.build.commit ? me.build.commit.slice(0, 7) : t({ message: "unknown", comment: "Text on the Application management screen: not known" })}
          note={<BuildNote build={me.build} repository={me.repository} />}
        />
        <Fact label={t({ message: "Environment", comment: "Name of a fact on the Application management screen: noun, development or production" })} value={me.environment} note={t({ message: `Python ${me.python}`, comment: "Note on the Application management screen" })} />
        <Fact label={t({ message: "Machine", comment: "Name of a fact on the Application management screen: noun, the computer it runs on" })} value={me.platform} />
        {/* The one number that tells two instances on one machine apart. A dev
            run and the real one look identical on every other fact here, and
            this is what you hand to `kill` or `lsof`. */}
        <Fact label={t({ message: "Process", comment: "Name of a fact on the Application management screen: noun, the operating-system process id" })} value={String(me.process_id)} note={t`hand this to kill or lsof`} />
        <Fact
          label={t({ message: "Started", comment: "Name of a fact on the Application management screen" })}
          value={me.started_at ? formatInstant(me.started_at) : "—"}
          note={t({ message: "this process", comment: "Note on the Application management screen" })}
        />
      </dl>

      <h3 className="section-title" style={{ marginTop: 18 }}>
        <Trans comment="Section heading on Application management: the addresses the app answers on">
          Where it can be reached
        </Trans>
      </h3>
      <ul className="plain-list mono small">
        {me.addresses.map((one) => (
          <li key={one}>{one}</li>
        ))}
      </ul>
      <p className="muted small">
        <Trans>
          The second one is what a phone on this network types in. Location on the Snap page needs
          an https address — a browser will not offer it over plain http, whatever this app sends.
        </Trans>
      </p>

      <PlacesTable places={me.places} />
      <PackagesTable packages={me.packages} />
    </section>
  );
}

function BuildNote({ build, repository }: { build: Build; repository: string }) {
  if (!build.commit) {
    return <Trans>no git history and no build stamp to ask</Trans>;
  }
  const said = [
    build.branch ?? t({ message: "no branch", comment: "Label on the Application management screen" }),
    build.committed_at ? t({ message: `committed ${formatInstant(build.committed_at)}`, comment: "Label on the Application management screen" }) : null,
    build.dirty ? t({ message: `with uncommitted changes`, comment: "Application management: the code was built with changes not yet saved in git" }) : null,
  ].filter(Boolean);
  return (
    <>
      {said.join(" · ")} ·{" "}
      <a href={`${repository}/commit/${build.commit}`} target="_blank" rel="noreferrer noopener">
        <Trans comment="Link on the Application management screen">
          see it
        </Trans>
      </a>
    </>
  );
}

function Fact({ label, value, note }: { label: string; value: string; note?: ReactNode }) {
  return (
    <div className="stat">
      <dt>{label}</dt>
      <dd>
        <span className="stat-figure stat-figure-text">{value}</span>
        {note ? <span className="muted small">{note}</span> : null}
      </dd>
    </div>
  );
}

function HouseholdTable({ households }: { households: HouseholdData[] }) {
  const order = useSort<"name" | "transactions" | "receipts">("name");
  const rows = useMemo(
    () =>
      sortRows(households, order.sort, order.direction, (house, column) =>
        column === "name" ? house.name : house[column],
      ),
    [households, order.sort, order.direction],
  );

  return (
    <>
      <h3 className="section-title" style={{ marginTop: 18 }}>
        <Trans comment="Section heading on Application management">
          How much data, per household
        </Trans>
      </h3>
      {rows.length === 0 ? (
        <Empty><Trans>No households on this instance yet.</Trans></Empty>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <SortHeading label={t({ message: "Household", comment: "Column heading on the Application management screen: noun, the people who share one ledger. See GLOSSARY.md" })} column="name" {...order} />
                <SortHeading label={t({ message: "Transactions", comment: "Column heading on the Application management screen. See GLOSSARY.md" })} column="transactions" align="right" {...order} />
                <SortHeading label={t({ message: "Receipts", comment: "Column heading on the Application management screen: noun, photos or PDFs of receipts. See GLOSSARY.md" })} column="receipts" align="right" {...order} />
              </tr>
            </thead>
            <tbody>
              {rows.map((house) => (
                <tr key={house.id}>
                  <td data-primary="true">{house.name}</td>
                  <td className="amount" data-label={t({ message: "Transactions", comment: "Column name shown beside a value on phones on the Application management screen. See GLOSSARY.md" })}>
                    {count(house.transactions)}
                  </td>
                  <td className="amount" data-label={t({ message: "Receipts", comment: "Column name shown beside a value on phones on the Application management screen: noun, photos or PDFs of receipts. Se…" })}>
                    {count(house.receipts)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted small">
        <Trans>
          Counts, never amounts — this ledger never converts one currency into another, so there
          is no such thing as an instance-wide total. For the full picture,{" "}
          <a href="/api/admin/application/tables.csv" download>
            download every table with its row count and size
          </a>
          . That report scans the database, which is why it is a download rather than a block
          here.
        </Trans>
      </p>
    </>
  );
}

function PlacesTable({ places }: { places: Place[] }) {
  return (
    <>
      <h3 className="section-title" style={{ marginTop: 18 }}>
        <Trans comment="Section heading on Application management: the folders the app uses">
          Where the files are
        </Trans>
      </h3>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th><Trans comment="Column heading on the Application management screen: noun, which thing">What</Trans></th>
              <th><Trans comment="Column heading on the Application management screen: noun, a place in the file system">Path</Trans></th>
              <th className="amount"><Trans comment="Column heading on the Application management screen: noun, size of a file">Size</Trans></th>
            </tr>
          </thead>
          <tbody>
            {places.map((one) => (
              <tr key={one.what}>
                <td data-primary="true">
                  {one.what}
                  {/* The one that is missing is usually the answer to whatever
                      brought somebody to this screen -- a `dist` directory that
                      is not there is why `/` 404s, which was issue #1 from a
                      fresh clone. Not flagged for the paths whose absence is
                      the healthy state: a screen that reports two faults on a
                      working instance is one nobody reads the third time. */}
                  {one.exists || one.optional ? null : (
                    <span className="tag" title={t({ message: `this path does not exist`, comment: "Tooltip on a folder path on Application management" })}>
                      <Trans comment="Tag beside a name on the Application management screen">
                        missing
                      </Trans>
                    </span>
                  )}
                  <div className="small muted">{one.note}</div>
                </td>
                <td className="mono small" data-label={t({ message: "Path", comment: "Column name shown beside a value on phones on the Application management screen: noun, a place in the file system" })}>
                  {one.path}
                </td>
                <td className="amount muted" data-label={t({ message: "Size", comment: "Column name shown beside a value on phones on the Application management screen: noun, size of a file" })}>
                  {bytes(one.bytes)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function PackagesTable({ packages }: { packages: Package[] }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <h3 className="section-title" style={{ marginTop: 18 }}>
        <Trans comment="Section heading on Application management: the installed software packages">What it depends on</Trans>
        <Hint label={t({ message: `what this list is`, comment: "Screen-reader name of a help button on Application management" })}>
          <p>
            <Trans>
              Every distribution installed in this environment, with the version that is actually
              loaded — not what <code>requirements.txt</code> locked, which an install may not have followed.
              When a dependency is the suspect, this is the list that answers it.
            </Trans>
          </p>
        </Hint>
      </h3>
      <p className="muted small">
        {plural(packages.length, {
          one: `${count(packages.length)} packages.`,
          other: `${count(packages.length)} packages.`,
        })}{" "}
        <button className="link" onClick={() => setOpen(!open)}>
          {open ? t({ message: "Hide them", comment: "Button on the Application management screen" }) : t({ message: "Show them", comment: "Button on the Application management screen" })}
        </button>
      </p>
      {open && (
        <ul className="package-list mono small">
          {packages.map((one) => (
            <li key={one.name}>
              {one.name} <span className="muted">{one.version}</span>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

// --------------------------------------------------------------------------- //
// Log files
// --------------------------------------------------------------------------- //

function Logs({ current, onChanged }: { current: string; onChanged: () => void }) {
  const client = useQueryClient();
  const [open, setOpen] = useState<string | null>(null);

  const state = useQuery({
    queryKey: ["application", "logging"],
    queryFn: () => api.get<LoggingState>("/admin/application/logging"),
  });
  const body = useQuery({
    queryKey: ["application", "logging", open],
    queryFn: () => api.get<{ name: string; bytes: number; text: string }>(
      `/admin/application/logs/${encodeURIComponent(open!)}`,
    ),
    enabled: open !== null,
  });

  const choose = useMutation({
    mutationFn: (style: string) => api.post<LoggingState>("/admin/application/logging", { style }),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["application", "logging"] });
      onChanged();
    },
  });

  // The server sends the END of the file, and the box is a fixed-height
  // scroller that opens at scrollTop 0 -- so pressing Read showed the oldest
  // part of a tail and the thing you opened the log for was off screen.
  //
  // Keyed on `body.data` rather than on `open`, so a re-read of the same file
  // lands at the bottom too; and gated on the loading state, because while it
  // is loading the element holds "Reading..." and has no height to scroll.
  const dump = useRef<HTMLPreElement>(null);
  useEffect(() => {
    if (body.isLoading) return;
    const el = dump.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [body.data, body.isLoading]);

  const it = state.data;

  return (
    <section className="card">
      <h2 className="section-title"><Trans comment="Heading on the Application management screen">Log files</Trans></h2>
      <Problem error={state.error ?? choose.error} />
      <p className="muted small" style={{ marginTop: 0 }}>
        <Trans>
          Written to <span className="mono">{it?.directory ?? "…"}</span>, rotated at a megabyte
          and five files deep. A rotated file is named for the moment it was closed &mdash;{" "}
          <span className="mono">app-20260923-131545.log</span> &mdash; so it keeps its name
          instead of shuffling along behind a number.
        </Trans>
      </p>

      {/* What is in each file, and which one is not safe to send anybody. */}
      <dl className="log-streams">
        {(it?.streams ?? []).map((stream) => (
          <div key={stream.key}>
            <dt className="mono small">
              {stream.filename}
              {stream.holds_ledger_values ? (
                <span className="pill danger" style={{ marginLeft: 6 }}>
                  <Trans comment="Tag beside a name on the Application management screen">
                    your data
                  </Trans>
                </span>
              ) : null}
            </dt>
            <dd className="muted small">{stream.blurb}</dd>
          </div>
        ))}
      </dl>
      <p className="muted small">
        <Trans>
          Changing the setting below writes a line into <strong>all three</strong>, on both sides of
          the change &mdash; including when you turn it <em>down</em>, which is the one that used to
          leave no trace at all.
        </Trans>
      </p>

      {it && it.files.length === 0 ? (
        <Empty><Trans>Nothing written yet. The file appears the next time this instance starts.</Trans></Empty>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th><Trans comment="Column heading on the Application management screen">File</Trans></th>
                <th><Trans comment="Column heading on the Application management screen">Last written</Trans></th>
                <th className="amount"><Trans comment="Column heading on the Application management screen: noun, size of a file">Size</Trans></th>
                <th className="amount row-actions" />
              </tr>
            </thead>
            <tbody>
              {(it?.files ?? []).map((one) => (
                <tr key={one.name}>
                  <td data-primary="true" className="mono small">
                    {one.name}
                  </td>
                  <td className="small muted" data-label={t({ message: "Last written", comment: "Column name shown beside a value on phones on the Application management screen" })}>
                    {formatInstant(one.modified)}
                  </td>
                  <td className="amount muted" data-label={t({ message: "Size", comment: "Column name shown beside a value on phones on the Application management screen: noun, size of a file" })}>
                    {bytes(one.bytes)}
                  </td>
                  <td className="amount row-actions">
                    <button
                      className="link"
                      onClick={() => setOpen(open === one.name ? null : one.name)}
                    >
                      {open === one.name ? t({ message: "Close", comment: "Button on the Application management screen: verb, close this panel" }) : t({ message: "Read", comment: "Button on the Application management screen: verb, show the file's contents" })}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {open !== null && (
        <>
          <h3 className="section-title" style={{ marginTop: 18 }}>
            {open}
          </h3>
          <Problem error={body.error} />
          {/* The end of the file, not the whole of it: a rotated megabyte
              rendered into the DOM is the browser falling over. The server
              says so in the first line when it has truncated. */}
          {/* `tabIndex` because a <pre> that scrolls is not focusable by
              default, so without it a keyboard user cannot scroll it at all.
              `aria-live` so a re-read announces rather than changing silently
              under a screen reader. */}
          <pre
            ref={dump}
            className="log-dump"
            tabIndex={0}
            aria-live="polite"
            aria-label={t({ message: `The end of ${open}`, comment: "Screen-reader name of a log file's last lines; the placeholder is the file's name" })}
          >
            {body.isLoading ? t({ message: "Reading…", comment: "Text on the Application management screen" }) : (body.data?.text ?? "")}
          </pre>
        </>
      )}

      <h3 className="section-title" style={{ marginTop: 18 }}>
        <Trans comment="Section heading on Application management: the log level">
          How much to write down
        </Trans>
      </h3>
      <div className="logging-styles">
        {(it?.styles ?? []).map((style) => (
          <button
            key={style.key}
            className={style.key === current ? "primary" : undefined}
            aria-pressed={style.key === current}
            disabled={choose.isPending}
            onClick={() => choose.mutate(style.key)}
          >
            {style.label}
          </button>
        ))}
      </div>
      {(it?.styles ?? []).map((style) =>
        style.key === current ? (
          <p className="muted small" key={style.key} style={{ marginBottom: 0 }}>
            {style.blurb}
            {style.echo_sql ? (
              <>
                {" "}
                <strong>
                  <Trans>
                    This writes the ledger's own values into a file in plain text. Turn it off when
                    you are done.
                  </Trans>
                </strong>
              </>
            ) : null}
          </p>
        ) : null,
      )}
    </section>
  );
}

// --------------------------------------------------------------------------- //
// Updates
// --------------------------------------------------------------------------- //

function Operations({ me }: { me: Instance }) {
  // What used to be *Is there a newer version?*, grown into the owner's side
  // of the self-updater (#166). Everything it says and does is in Updates.tsx.
  return (
    <section className="card" id="updates" aria-labelledby="updates-title">
      <h2 className="section-title" id="updates-title">
        <Trans comment="Heading on the Application management screen: new versions and installing them">
          Updates
        </Trans>
      </h2>
      <Updates repository={me.repository} commit={me.build.commit} />
    </section>
  );
}

// --------------------------------------------------------------------------- //
// Database
// --------------------------------------------------------------------------- //

function Database({ me, onChanged }: { me: Instance; onChanged: () => void }) {
  const client = useQueryClient();
  const backups = useQuery({
    queryKey: ["application", "backups"],
    queryFn: () => api.get<Backup[]>("/admin/application/backups"),
  });
  const backup = useMutation({
    mutationFn: () => api.post<Backup>("/admin/application/backups"),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["application", "backups"] });
      onChanged();
    },
  });

  return (
    <section className="card">
      <h2 className="section-title"><Trans comment="Heading on the Application management screen">Database</Trans></h2>
      <p className="muted small" style={{ marginTop: 0 }}>
        <Trans>
          The ledger this process is serving.
        </Trans>
      </p>
      <Problem error={backup.error} />
      <dl className="stat-grid application-facts">
        {/* The screen used to say `sqlite`, as a note under the size, and
            nothing else -- not which SQLite, and not whether it was in WAL
            mode, although the WAL and SHM entries in Places already assumed
            it was. */}
        <Fact
          label={t({ message: "Engine", comment: "Name of a fact on the Application management screen: noun, the database software" })}
          value={me.engine.version ? `${me.engine.name} ${me.engine.version}` : me.engine.name}
          note={me.engine.journal_mode ? t({ message: `${me.engine.journal_mode} journal`, comment: "Text on the Application management screen" }) : undefined}
        />
        <Fact
          label={t({ message: "Schema", comment: "Name of a fact on the Application management screen: noun, the database's migration revision" })}
          value={me.schema_revision ?? "—"}
          note={me.schema_revision ? t`the migration this database is at` : t({ message: "not migrated", comment: "Text on the Application management screen" })}
        />
        <Fact
          label={t({ message: "Size", comment: "Name of a fact on the Application management screen: noun, size of a file" })}
          value={bytes(me.size.total_bytes)}
          note={
            me.size.wal_bytes > 0
              ? t({ message: `${bytes(me.size.main_bytes)} + ${bytes(me.size.wal_bytes)} not yet checkpointed`, comment: "Database size: the main file plus recent writes not yet copied into it" })
              : undefined
          }
        />
        <Fact
          label={t({ message: "Pages", comment: "Name of a fact on the Application management screen: noun, database pages" })}
          value={me.size.page_count === null ? "—" : count(me.size.page_count)}
          note={
            me.size.page_size === null
              ? undefined
              : t({ message: `${bytes(me.size.page_size)} each, ${count(me.size.free_pages ?? 0)} free`, comment: "Text on the Application management screen" })
          }
        />
        <Fact
          label={t({ message: "Last backup", comment: "Name of a fact on the Application management screen" })}
          value={me.latest_backup ? formatInstant(me.latest_backup.made_at) : t({ message: "never", comment: "Text on the Application management screen: has never happened" })}
          note={me.latest_backup ? bytes(me.latest_backup.bytes) : t({ message: `use the button below`, comment: "Application management: shown where the last backup's size would be, when there is none" })}
        />
      </dl>

      {/* The path was filed under Places, with the log directory and the
          installation -- which is where you look for paths and not where you
          look for the database. It comes from `database_path()`, never from
          `settings.database_url`, which can carry a password. */}
      <h3 className="section-title" style={{ marginTop: 18 }}>
        <Trans comment="Section heading on Application management: where the database file is">
          Where it is
        </Trans>
      </h3>
      <p className="mono small" style={{ marginTop: 0 }}>
        {me.engine.path ?? t({ message: `${me.database_url_scheme} — not a local file`, comment: "Application management: the database is not a file on this machine" })}
      </p>

      <HouseholdTable households={me.households} />

      <hr className="rule" />

      <h3 className="section-title"><Trans comment="Section heading on Application management. See GLOSSARY.md">Back the database up</Trans></h3>
      <p className="muted small">
        <Trans>
          Writes a complete, compacted copy into the backups directory above, with this instance
          still serving. It does <strong>not</strong> copy <span className="mono">secret.key</span>{" "}
          there: without that key every authenticator is refused, so a copy that leaves this
          machine either carries it (tick the box below) or travels with it separately.
        </Trans>
      </p>
      <button className="primary" onClick={() => backup.mutate()} disabled={backup.isPending}>
        {backup.isPending ? t({ message: "Writing…", comment: "Button on the Application management screen" }) : t({ message: `Back up now`, comment: "Button on Application management. See GLOSSARY.md" })}
      </button>
      <BackupList
        backups={(backups.data ?? []).filter((one) => one.kind !== "update")}
        onChanged={onChanged}
      />
      <p className="muted small" style={{ marginBottom: 0 }}>
        <Trans>
          A download is a zip with a README for whoever opens it next: what the file is, how to
          read it, and how to put it back with <span className="mono">make restore</span>. Delete
          removes one file when you say so and confirm. Nothing here deletes one of these on its own
          — no pruning and no timer.
        </Trans>
      </p>
      <UpdateBackupList
        backups={(backups.data ?? []).filter((one) => one.kind === "update")}
        onChanged={onChanged}
      />
    </section>
  );
}

// --------------------------------------------------------------------------- //
// About
// --------------------------------------------------------------------------- //

function About({ me }: { me: Instance }) {
  return (
    <section className="card">
      <h2 className="section-title"><Trans comment="Heading on the Application management screen">About</Trans></h2>
      <p className="muted small" style={{ marginTop: 0 }}>
        <Trans>
          {me.app_name} {me.version} — a self-hosted, multi-currency spend tracker for one
          household, under the AGPL.
        </Trans>
      </p>
      <ul className="plain-list">
        <li>
          <a href={me.repository} target="_blank" rel="noreferrer noopener">
            <Trans comment="Link on Application management to the app's source code">
              The source, on GitHub
            </Trans>
          </a>
        </li>
        <li>
          <a href={me.author} target="_blank" rel="noreferrer noopener">
            <Trans>
              Its author, and their other projects
            </Trans>
          </a>
        </li>
      </ul>
    </section>
  );
}
