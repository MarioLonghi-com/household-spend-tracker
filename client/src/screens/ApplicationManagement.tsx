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
 * - **Operations** — what is about the installation rather than about either
 *   of the above: check the repository for a newer version.
 * - **About** — where the code and its author live.
 *
 * Nothing here is polled. Every figure is read when the screen opens and after
 * an operation that would move it; a page that re-reads the size of a database
 * every few seconds is a page that is itself the load.
 */

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Empty, Hint, Problem, SortHeading, sortRows, useSort } from "../components/bits";
import { bytes } from "../lib/bytes";
import { BackupList, type Backup } from "./Backups";
import { formatInstant } from "../lib/time";

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

interface Release {
  version: string;
  tag: string;
  name: string | null;
  published_at: string | null;
  /** Plain text: render it as text, never as HTML. */
  notes: string;
  notes_from: "changelog" | "release";
}

interface UpdaterOffer {
  version: string | null;
  compatible: boolean | null;
  note: string;
}

interface Upstream {
  checked_at: string;
  running: string;
  latest: string | null;
  newer: boolean;
  problem: string | null;
  releases: Release[];
  updater: UpdaterOffer | null;
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

const count = (n: number) => n.toLocaleString();

export function ApplicationManagement() {
  const client = useQueryClient();
  const it = useQuery({
    queryKey: ["application"],
    queryFn: () => api.get<Instance>("/admin/application"),
  });

  const refresh = () => client.invalidateQueries({ queryKey: ["application"] });

  if (it.isLoading) return <div className="card muted">Reading this instance…</div>;
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
        <h1>Application management</h1>
      </div>
      <p className="muted small" style={{ marginTop: 0 }}>
        This installation, rather than any of the ledgers in it. Only an owner can open this
        screen, and only an owner can reach anything on it — the paths, the logs and the counts
        below are about every household on this instance.
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
      <h2 className="section-title">Runtime</h2>
      <p className="muted small" style={{ marginTop: 0 }}>
        The process answering this request.
      </p>
      <dl className="stat-grid application-facts">
        <Fact label="Version" value={me.version} note={me.app_name} />
        {/* The version only moves at a release, so main and a dev far ahead of
            it both say the same thing. The commit is what tells them apart. */}
        <Fact
          label="Commit"
          value={me.build.commit ? me.build.commit.slice(0, 7) : "unknown"}
          note={<BuildNote build={me.build} repository={me.repository} />}
        />
        <Fact label="Environment" value={me.environment} note={`Python ${me.python}`} />
        <Fact label="Machine" value={me.platform} />
        {/* The one number that tells two instances on one machine apart. A dev
            run and the real one look identical on every other fact here, and
            this is what you hand to `kill` or `lsof`. */}
        <Fact label="Process" value={String(me.process_id)} note="hand this to kill or lsof" />
        <Fact
          label="Started"
          value={me.started_at ? formatInstant(me.started_at) : "—"}
          note="this process"
        />
      </dl>

      <h3 className="section-title" style={{ marginTop: 18 }}>
        Where it can be reached
      </h3>
      <ul className="plain-list mono small">
        {me.addresses.map((one) => (
          <li key={one}>{one}</li>
        ))}
      </ul>
      <p className="muted small">
        The second one is what a phone on this network types in. Location on the Snap page needs
        an https address — a browser will not offer it over plain http, whatever this app sends.
      </p>

      <PlacesTable places={me.places} />
      <PackagesTable packages={me.packages} />
    </section>
  );
}

function BuildNote({ build, repository }: { build: Build; repository: string }) {
  if (!build.commit) {
    return <>no git history and no build stamp to ask</>;
  }
  const said = [
    build.branch ?? "no branch",
    build.committed_at ? `committed ${formatInstant(build.committed_at)}` : null,
    build.dirty ? "with uncommitted changes" : null,
  ].filter(Boolean);
  return (
    <>
      {said.join(" · ")} ·{" "}
      <a href={`${repository}/commit/${build.commit}`} target="_blank" rel="noreferrer noopener">
        see it
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
        How much data, per household
      </h3>
      {rows.length === 0 ? (
        <Empty>No households on this instance yet.</Empty>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <SortHeading label="Household" column="name" {...order} />
                <SortHeading label="Transactions" column="transactions" align="right" {...order} />
                <SortHeading label="Receipts" column="receipts" align="right" {...order} />
              </tr>
            </thead>
            <tbody>
              {rows.map((house) => (
                <tr key={house.id}>
                  <td data-primary="true">{house.name}</td>
                  <td className="amount" data-label="Transactions">
                    {count(house.transactions)}
                  </td>
                  <td className="amount" data-label="Receipts">
                    {count(house.receipts)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted small">
        Counts, never amounts — this ledger never converts one currency into another, so there is
        no such thing as an instance-wide total. For the full picture,{" "}
        <a href="/api/admin/application/tables.csv" download>
          download every table with its row count and size
        </a>
        . That report scans the database, which is why it is a download rather than a block here.
      </p>
    </>
  );
}

function PlacesTable({ places }: { places: Place[] }) {
  return (
    <>
      <h3 className="section-title" style={{ marginTop: 18 }}>
        Where the files are
      </h3>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>What</th>
              <th>Path</th>
              <th className="amount">Size</th>
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
                    <span className="tag" title="this path does not exist">
                      missing
                    </span>
                  )}
                  <div className="small muted">{one.note}</div>
                </td>
                <td className="mono small" data-label="Path">
                  {one.path}
                </td>
                <td className="amount muted" data-label="Size">
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
        What it depends on
        <Hint label="what this list is">
          <p>
            Every distribution installed in this environment, with the version that is actually
            loaded — not what <code>requirements.txt</code> locked, which an install may not have followed.
            When a dependency is the suspect, this is the list that answers it.
          </p>
        </Hint>
      </h3>
      <p className="muted small">
        {count(packages.length)} packages.{" "}
        <button className="link" onClick={() => setOpen(!open)}>
          {open ? "Hide them" : "Show them"}
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
      <h2 className="section-title">Log files</h2>
      <Problem error={state.error ?? choose.error} />
      <p className="muted small" style={{ marginTop: 0 }}>
        Written to <span className="mono">{it?.directory ?? "…"}</span>, rotated at a megabyte and
        five files deep. A rotated file is named for the moment it was closed &mdash;{" "}
        <span className="mono">app-20260923-131545.log</span> &mdash; so it keeps its name instead
        of shuffling along behind a number.
      </p>

      {/* What is in each file, and which one is not safe to send anybody. */}
      <dl className="log-streams">
        {(it?.streams ?? []).map((stream) => (
          <div key={stream.key}>
            <dt className="mono small">
              {stream.filename}
              {stream.holds_ledger_values ? (
                <span className="pill danger" style={{ marginLeft: 6 }}>
                  your data
                </span>
              ) : null}
            </dt>
            <dd className="muted small">{stream.blurb}</dd>
          </div>
        ))}
      </dl>
      <p className="muted small">
        Changing the setting below writes a line into <strong>all three</strong>, on both sides of
        the change &mdash; including when you turn it <em>down</em>, which is the one that used to
        leave no trace at all.
      </p>

      {it && it.files.length === 0 ? (
        <Empty>Nothing written yet. The file appears the next time this instance starts.</Empty>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>File</th>
                <th>Last written</th>
                <th className="amount">Size</th>
                <th className="amount row-actions" />
              </tr>
            </thead>
            <tbody>
              {(it?.files ?? []).map((one) => (
                <tr key={one.name}>
                  <td data-primary="true" className="mono small">
                    {one.name}
                  </td>
                  <td className="small muted" data-label="Last written">
                    {formatInstant(one.modified)}
                  </td>
                  <td className="amount muted" data-label="Size">
                    {bytes(one.bytes)}
                  </td>
                  <td className="amount row-actions">
                    <button
                      className="link"
                      onClick={() => setOpen(open === one.name ? null : one.name)}
                    >
                      {open === one.name ? "Close" : "Read"}
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
            aria-label={`The end of ${open}`}
          >
            {body.isLoading ? "Reading…" : (body.data?.text ?? "")}
          </pre>
        </>
      )}

      <h3 className="section-title" style={{ marginTop: 18 }}>
        How much to write down
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
                  This writes the ledger's own values into a file in plain text. Turn it off when
                  you are done.
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
// Operations
// --------------------------------------------------------------------------- //

function Operations({ me }: { me: Instance }) {
  // Backing up moved to Database, where the thing it acts on is described.
  // What is left is the one operation that is about the *installation* rather
  // than about either the process or the ledger.
  const upstream = useMutation({
    mutationFn: () => api.post<Upstream>("/admin/application/upstream"),
  });

  return (
    <section className="card">
      <h2 className="section-title">Operations</h2>
      <Problem error={upstream.error} />

      <h3 className="section-title">Is there a newer version?</h3>
      <p className="muted small">
        Asks {me.repository} for its published releases and compares them with the {me.version}{" "}
        this is running. Nothing leaves this instance unless an owner presses a button like this
        one, nothing is asked on a timer, and no request says anything about this instance.
      </p>
      <button onClick={() => upstream.mutate()} disabled={upstream.isPending}>
        {upstream.isPending ? "Asking…" : "Check the repository"}
      </button>
      {upstream.data ? (
        <div className={upstream.data.newer ? "banner warn" : "banner"} style={{ marginTop: 10 }}>
          {upstream.data.problem ? (
            upstream.data.problem
          ) : upstream.data.newer ? (
            <>
              <strong>{upstream.data.latest}</strong> is available. This instance is running{" "}
              {upstream.data.running}.
            </>
          ) : (
            <>
              This is the newest there is: {upstream.data.running}, and the repository's latest
              release is {upstream.data.latest}.
            </>
          )}
        </div>
      ) : null}
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
      <h2 className="section-title">Database</h2>
      <p className="muted small" style={{ marginTop: 0 }}>
        The ledger this process is serving.
      </p>
      <Problem error={backup.error} />
      <dl className="stat-grid application-facts">
        {/* The screen used to say `sqlite`, as a note under the size, and
            nothing else -- not which SQLite, and not whether it was in WAL
            mode, although the WAL and SHM entries in Places already assumed
            it was. */}
        <Fact
          label="Engine"
          value={me.engine.version ? `${me.engine.name} ${me.engine.version}` : me.engine.name}
          note={me.engine.journal_mode ? `${me.engine.journal_mode} journal` : undefined}
        />
        <Fact
          label="Schema"
          value={me.schema_revision ?? "—"}
          note={me.schema_revision ? "the migration this database is at" : "not migrated"}
        />
        <Fact
          label="Size"
          value={bytes(me.size.total_bytes)}
          note={
            me.size.wal_bytes > 0
              ? `${bytes(me.size.main_bytes)} + ${bytes(me.size.wal_bytes)} not yet checkpointed`
              : undefined
          }
        />
        <Fact
          label="Pages"
          value={me.size.page_count === null ? "—" : count(me.size.page_count)}
          note={
            me.size.page_size === null
              ? undefined
              : `${bytes(me.size.page_size)} each, ${count(me.size.free_pages ?? 0)} free`
          }
        />
        <Fact
          label="Last backup"
          value={me.latest_backup ? formatInstant(me.latest_backup.made_at) : "never"}
          note={me.latest_backup ? bytes(me.latest_backup.bytes) : "use the button below"}
        />
      </dl>

      {/* The path was filed under Places, with the log directory and the
          installation -- which is where you look for paths and not where you
          look for the database. It comes from `database_path()`, never from
          `settings.database_url`, which can carry a password. */}
      <h3 className="section-title" style={{ marginTop: 18 }}>
        Where it is
      </h3>
      <p className="mono small" style={{ marginTop: 0 }}>
        {me.engine.path ?? `${me.database_url_scheme} — not a local file`}
      </p>

      <HouseholdTable households={me.households} />

      <hr className="rule" />

      <h3 className="section-title">Back the database up</h3>
      <p className="muted small">
        Writes a complete, compacted copy into the backups directory above, with this instance
        still serving. It does <strong>not</strong> copy <span className="mono">secret.key</span>{" "}
        there: without that key every authenticator is refused, so a copy that leaves this machine
        either carries it (tick the box below) or travels with it separately.
      </p>
      <button className="primary" onClick={() => backup.mutate()} disabled={backup.isPending}>
        {backup.isPending ? "Writing…" : "Back up now"}
      </button>
      <BackupList backups={backups.data ?? []} onChanged={onChanged} />
      <p className="muted small" style={{ marginBottom: 0 }}>
        A download is a zip with a README for whoever opens it next: what the file is, how to
        read it, and how to put it back with <span className="mono">make restore</span>. Delete
        removes one file when you say so and confirm. Nothing here deletes a backup on its own —
        no pruning and no timer.
      </p>
    </section>
  );
}

// --------------------------------------------------------------------------- //
// About
// --------------------------------------------------------------------------- //

function About({ me }: { me: Instance }) {
  return (
    <section className="card">
      <h2 className="section-title">About</h2>
      <p className="muted small" style={{ marginTop: 0 }}>
        {me.app_name} {me.version} — a self-hosted, multi-currency spend tracker for one household,
        under the AGPL.
      </p>
      <ul className="plain-list">
        <li>
          <a href={me.repository} target="_blank" rel="noreferrer noopener">
            The source, on GitHub
          </a>
        </li>
        <li>
          <a href={me.author} target="_blank" rel="noreferrer noopener">
            Its author, and their other projects
          </a>
        </li>
      </ul>
    </section>
  );
}
