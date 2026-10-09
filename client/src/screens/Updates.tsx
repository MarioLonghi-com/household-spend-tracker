/**
 * The Updates section of the Application screen (#166).
 *
 * It replaces *Is there a newer version?* and is the owner's whole side of the
 * self-updater: what this instance can do, the check, preparing, the
 * confirmation, the update itself and its outcome. The server's endpoints are
 * `app/api/routers/updates.py`; the updater is another container that the app
 * talks to through files, so everything here is "ask, then watch".
 *
 * **What it says first depends on the case** the server reports (`case`):
 * a checkout (`not_container`), a container with no updater (`no_updater`),
 * an updater the engine refuses (`refused`), one the engine has outgrown
 * (`outdated`), and a working one (`working`). Container and engine names
 * come from the updater's heartbeat, never from a constant: Docker Compose
 * and podman-compose name the same service differently.
 *
 * **The check is one request, when pressed** (A4). Release notes are plain
 * text from the repository and are rendered as text, never as HTML.
 *
 * **Polling** happens only while something is in flight: every two seconds
 * while the updater prepares, and, once *Update* is pressed, the full-width
 * panel below watches on its own -- the update state every two seconds while
 * the app answers, then `/api/health` every three once it has stopped, keeping
 * the last progress it saw, and loading `/?open=application#updates` when the
 * app is back, so the page opens on this section and its outcome rather than
 * on the register.
 *
 * **The Update button needs every box**: one per migration a downgrade cannot
 * undo (an undeclared one counts), *I have saved the recovery code*, and the
 * password and a code. The server and the updater both refuse an apply whose
 * lossy list is not exactly the report's, so the boxes are the owner's
 * acknowledgement, not the only guard.
 *
 * **The recovery code is fetched with a `POST`** when the confirmation is
 * drawn (R21): issuing one replaces the code held for this report, so it is
 * not something a `GET` -- or a prefetch -- may do.
 */

import { useEffect, useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { plural, t } from "@lingui/core/macro";
import { Trans, useLingui } from "@lingui/react/macro";
import { api } from "../lib/api";
import { Problem } from "../components/bits";
import { formatInstant } from "../lib/time";
import { listText } from "../lib/locale";
import {
  NO_PROOF,
  spent,
  StepUpFields,
  stepUpReady,
  stepUpToken,
  type StepUpProof,
} from "../components/StepUp";

// --------------------------------------------------------------------------- //
// What the server sends. `app/schemas.py`; `tests/test_client_agrees.py`
// holds each interface to its model, field for field.
// --------------------------------------------------------------------------- //

export interface Release {
  version: string;
  tag: string;
  name: string | null;
  published_at: string | null;
  /** Plain text: render it as text, never as HTML. */
  notes: string;
  notes_from: "changelog" | "release";
}

export interface UpdaterOffer {
  version: string | null;
  compatible: boolean | null;
  note: string;
}

export interface Upstream {
  checked_at: string;
  running: string;
  latest: string | null;
  newer: boolean;
  problem: string | null;
  releases: Release[];
  updater: UpdaterOffer | null;
}

export interface Heartbeat {
  seen_at: string | null;
  fresh: boolean;
  updater_version: string | null;
  image_digest: string | null;
  engine: string | null;
  engine_version: string | null;
  rootless: boolean | null;
  layout: string | null;
  socket: string | null;
  hook: boolean | null;
  busy: boolean | null;
  role: string | null;
  protocols: string | null;
  api_version: string | null;
  engine_api: string | null;
  container: string | null;
  socket_sentence: string | null;
  /** Why the updater cannot go on, in its own sentence (#262). Absent from older updaters. */
  problem?: string | null;
}

export interface UpdateStatus {
  state: string;
  id: string | null;
  kind: string | null;
  step: string | null;
  sentences: string[];
  updated_at: string | null;
}

export interface Migration {
  revision: string;
  title: string | null;
  /** `clean`, `lossy` or `undeclared`; anything not `clean` needs its box. */
  reversible: string;
  note: string | null;
}

export interface Report {
  id: string;
  from_version: string;
  to_version: string;
  digest: string | null;
  updater_digest: string | null;
  expires_at: string;
  database_stamp: string | null;
  pending: Migration[];
  /** Exactly the revisions `accepted_lossy` must name. */
  lossy: string[];
  sizes: Record<string, unknown>;
  attestations: Record<string, unknown>;
}

export interface Outcome {
  id: string;
  kind: string | null;
  state: string;
  sentence: string | null;
  code: string | null;
  finished_at: string | null;
  started_at: string | null;
  failed_step: string | null;
  backup: string | null;
  duration_s: number | null;
  gap_s: number | null;
  log_tail: string[];
}

export type UpdateCase = "not_container" | "no_updater" | "refused" | "outdated" | "working";

export interface UpdateState {
  case: UpdateCase;
  running: string;
  protocol: number;
  in_flight: boolean;
  heartbeat: Heartbeat | null;
  status: UpdateStatus | null;
  report: Report | null;
  outcome: Outcome | null;
  backups: unknown[];
}

export interface RecoveryCode {
  id: string;
  code: string;
  prepared_id: string;
  expires_at: string;
}

export interface UpdateRequest {
  id: string;
  kind: string;
  to_version: string | null;
}

// --------------------------------------------------------------------------- //
// Small helpers
// --------------------------------------------------------------------------- //

export const STATE_KEY = ["application", "update"] as const;
const BASE = "/admin/application/update";

/** States after which nothing is in flight (`services/updates.SETTLED`). */
const SETTLED = new Set([
  "succeeded",
  "rolled_back",
  "not_started",
  "refused",
  "recovered",
  "left_for_operator",
]);

/** How often the section re-reads the state while the updater works (3.3). */
export const PREPARE_POLL_MS = 2_000;
/** While the app still answers, during an update. */
export const APPLY_POLL_MS = 2_000;
/** Once it has stopped: `/api/health`, every three seconds (3.5). */
export const HEALTH_POLL_MS = 3_000;
/** When the panel stops only waiting and points at the recovery page. */
export const NOT_BACK_AFTER_MS = 30 * 60 * 1_000;

function numbers(version: string): number[] {
  return version.split(".").map((part) => Number(part) || 0);
}

/** -1, 0 or 1, comparing `X.Y.Z` as numbers: 0.10.0 is newer than 0.9.0. */
export function compareVersions(a: string, b: string): number {
  const x = numbers(a);
  const y = numbers(b);
  for (let at = 0; at < Math.max(x.length, y.length); at += 1) {
    const by = (x[at] ?? 0) - (y[at] ?? 0);
    if (by !== 0) return by < 0 ? -1 : 1;
  }
  return 0;
}

/** The engine as the owner knows it, with its version: "Docker Desktop 4.48.0". */
function engineName(beat: Heartbeat | null): string {
  const version = beat?.engine_version ?? "";
  switch (beat?.engine) {
    case "docker-desktop":
      return `Docker Desktop ${version}`.trim();
    case "docker-engine":
      return `Docker Engine ${version}`.trim();
    case "podman":
      return `Podman ${version}`.trim();
    case "podman-machine":
      return t({
        message: `Podman ${version} (podman machine)`,
        comment: "The container engine, by product name; the version may be empty",
      });
    default:
      return t`the container engine`;
  }
}

/** A personal computer, where sleep is the risk (8.6): Desktop or podman machine. */
function onALaptop(beat: Heartbeat | null): boolean {
  return beat?.engine === "docker-desktop" || beat?.engine === "podman-machine";
}

function shortDigest(digest: string | null): string {
  if (!digest) return "—";
  return digest.startsWith("sha256:") ? `sha256:${digest.slice(7, 11)}…` : digest;
}

/** "2 min 40 s", or "40 s". */
function duration(seconds: number | null): string | null {
  if (seconds === null || !Number.isFinite(seconds)) return null;
  const whole = Math.round(seconds);
  const minutes = Math.floor(whole / 60);
  const rest = whole % 60;
  return minutes > 0
    ? t({ message: `${minutes} min ${rest} s`, comment: "How long an update took: minutes and seconds" })
    : t({ message: `${rest} s`, comment: "How long an update took: seconds" });
}

function timeOf(stamp: string | null): string {
  if (!stamp) return "—";
  const when = new Date(stamp);
  return Number.isNaN(when.getTime())
    ? stamp
    : when.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

/**
 * What the prepare report says about where an image came from, if the shape
 * is the one `updater/verify.py`'s `Verified` writes. Anything else reads as
 * "checked", without detail, rather than as a guess.
 */
function attested(report: Report, which: "app" | "updater"): { commit?: string; repository?: string } {
  const one = report.attestations?.[which];
  if (!one || typeof one !== "object") return {};
  const found = one as Record<string, unknown>;
  return {
    commit: typeof found.commit === "string" ? found.commit : undefined,
    repository: typeof found.repository === "string" ? found.repository : undefined,
  };
}

/** Save `text` as a file, through a link the browser downloads. */
function saveText(name: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1_000);
}

// --------------------------------------------------------------------------- //
// The section
// --------------------------------------------------------------------------- //

/**
 * Where the page goes once the app is back after an update or a rollback: a
 * fresh load -- the new version's client, not this one -- opened on
 * Application management, where the outcome is. A bare reload landed on the
 * register, because the shell keeps its screen in memory only, and the owner
 * never saw whether the update worked or was rolled back. `#updates` scrolls
 * this section into view there. `App.tsx` reads both and puts the address
 * back to `/`.
 */
export function backToThisScreen(): void {
  window.location.assign("/?open=application#updates");
}

export function Updates({
  repository,
  commit,
  onBack,
}: {
  repository: string;
  /** The commit this process runs, for "Running now". */
  commit: string | null;
  /** What to do when the app is back after an update. `backToThisScreen`, but for tests. */
  onBack?: () => void;
}) {
  const client = useQueryClient();
  const state = useQuery({
    queryKey: STATE_KEY,
    queryFn: () => api.get<UpdateState>(BASE),
    // Only while the updater is working on something, and never on a timer
    // otherwise: this screen asks nobody anything until it is asked to.
    refetchInterval: (query) => (query.state.data?.in_flight ? PREPARE_POLL_MS : false),
  });
  const refresh = () => client.invalidateQueries({ queryKey: STATE_KEY });

  const check = useMutation({
    mutationFn: () => api.post<Upstream>("/admin/application/upstream"),
  });
  /** The version the owner last asked to prepare, for *Try again*. */
  const [lastPrepared, setLastPrepared] = useState<string | null>(null);
  /** Set when *Update* was pressed in this tab: the panel takes over. */
  const [applying, setApplying] = useState<string | null>(null);

  const prepare = useMutation({
    mutationFn: (to_version: string) =>
      api.post<UpdateRequest>(`${BASE}/prepare`, { to_version }),
    onMutate: (to_version) => setLastPrepared(to_version),
    onSuccess: refresh,
  });
  const updaterOnly = useMutation({
    mutationFn: (to_version: string | null) =>
      api.post<UpdateRequest>(`${BASE}/updater`, to_version ? { to_version } : {}),
    onSuccess: refresh,
  });
  // `outdated`: the newest updater is what the check offers, so the button
  // checks first when nobody has yet.
  const replaceUpdater = useMutation({
    mutationFn: async () => {
      const upstream = check.data ?? (await check.mutateAsync());
      const offered = upstream.updater?.version ?? null;
      return api.post<UpdateRequest>(`${BASE}/updater`, offered ? { to_version: offered } : {});
    },
    onSuccess: refresh,
  });

  if (state.isLoading) {
    return (
      <p className="muted small">
        <Trans>Reading what the updater last said…</Trans>
      </p>
    );
  }
  if (state.isError || !state.data) return <Problem error={state.error} />;
  const it = state.data;
  const beat = it.heartbeat;

  const busy = it.status && !SETTLED.has(it.status.state) ? it.status : null;
  const applyInFlight = applying !== null || (busy?.kind === "apply" && it.in_flight);

  return (
    <div className="updates">
      {it.outcome && (
        <OutcomeBlock
          outcome={it.outcome}
          running={it.running}
          heartbeat={beat}
          lastPrepared={lastPrepared}
          onPrepare={(version) => prepare.mutate(version)}
          onChanged={refresh}
        />
      )}

      <CaseSentence
        state={it}
        onReplaceUpdater={() => replaceUpdater.mutate()}
        replacing={replaceUpdater.isPending}
      />
      {beat?.problem ? (
        <div className="banner warn" data-problem="updater">
          {beat.problem}
        </div>
      ) : null}
      <Problem error={replaceUpdater.error ?? updaterOnly.error ?? prepare.error} />

      {applyInFlight ? (
        <Updating
          toVersion={applying ?? it.report?.to_version ?? null}
          initial={busy?.sentences ?? []}
          onBack={onBack ?? backToThisScreen}
        />
      ) : it.in_flight ? (
        <InFlight status={busy} toVersion={lastPrepared} />
      ) : it.report ? (
        <Confirm
          key={it.report.id}
          report={it.report}
          heartbeat={beat}
          repository={repository}
          commit={commit}
          onApplied={(to) => setApplying(to)}
          onDiscarded={refresh}
        />
      ) : (
        <Check
          state={it}
          repository={repository}
          check={check.data ?? null}
          checking={check.isPending}
          checkError={check.error}
          onCheck={() => check.mutate()}
          onPrepare={(version) => prepare.mutate(version)}
          preparing={prepare.isPending}
          onUpdaterOnly={(version) => updaterOnly.mutate(version)}
          updatingUpdater={updaterOnly.isPending}
        />
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// 3.1: what the section says before anything is pressed
// --------------------------------------------------------------------------- //

function CaseSentence({
  state,
  onReplaceUpdater,
  replacing,
}: {
  state: UpdateState;
  onReplaceUpdater: () => void;
  replacing: boolean;
}) {
  const beat = state.heartbeat;
  const updaterVersion = beat?.updater_version ?? "?";
  switch (state.case) {
    case "refused":
      return (
        <div className="banner warn" data-case="refused">
          {beat?.socket_sentence ??
            t`The updater cannot use the container engine (${beat?.socket ?? "?"}), so this screen cannot update the app.`}{" "}
          <Trans>
            Update by replacing the compose bundle with the newest release's: your data and version
            are kept.
          </Trans>
        </div>
      );
    case "outdated": {
      const engine = engineName(beat);
      return (
        <div className="banner warn" data-case="outdated">
          <Trans>
            {engine} is newer than this updater ({updaterVersion}) can work with. It will try to
            replace itself with a newer one:
          </Trans>{" "}
          <button onClick={onReplaceUpdater} disabled={replacing}>
            {replacing
              ? t({ message: "Asking…", comment: "Button while a request is sent" })
              : t({ message: "Update the updater", comment: "Button: replace the updater container only" })}
          </button>{" "}
          <Trans>
            If that fails, download the newest Spend Tracker zip and run its launcher; your data and
            version are kept.
          </Trans>
        </div>
      );
    }
    case "working": {
      const engine = engineName(beat);
      const container = beat?.container ?? "updater";
      return (
        <p className="muted small" data-case="working">
          <Trans>
            Updates run in the updater (<span className="mono">{container}</span>, version{" "}
            {updaterVersion}, on {engine}). Nothing installs until you confirm it.
          </Trans>
        </p>
      );
    }
    default:
      // `not_container` and `no_updater` say their piece under a newer
      // version, where it answers "so how do I install it?" (3.1).
      return null;
  }
}

/** What to do instead, when this screen cannot install a newer version itself. */
function NoUpdaterHere({ state }: { state: UpdateState }) {
  if (state.case === "not_container") {
    return (
      <p className="small" data-case="not_container">
        <Trans>
          This instance runs from a checkout. Update it from the terminal:{" "}
          <span className="mono">make upgrade-check</span>, then{" "}
          <span className="mono">make upgrade</span>.
        </Trans>
      </p>
    );
  }
  if (state.case === "no_updater") {
    // A stale heartbeat still names the container; no heartbeat at all
    // names nothing, so the sentence names the service instead.
    const name = state.heartbeat?.container;
    return (
      <div className="small" data-case="no_updater">
        <p style={{ marginTop: 0 }}>
          <Trans>
            Updating from this screen needs the updater, which comes with the standard compose file
            from the release that introduced self-update onwards.
          </Trans>
        </p>
        <p style={{ marginBottom: 0 }}>
          {name ? (
            <Trans>
              In Docker Desktop or Podman Desktop, open Containers, find{" "}
              <span className="mono">{name}</span> and press Start.
            </Trans>
          ) : (
            <Trans>
              In Docker Desktop or Podman Desktop, open Containers, find the{" "}
              <span className="mono">updater</span> service of the{" "}
              <span className="mono">spend-tracker</span> project and press Start.
            </Trans>
          )}
        </p>
      </div>
    );
  }
  return null;
}

// --------------------------------------------------------------------------- //
// 3.2: checking
// --------------------------------------------------------------------------- //

function Check({
  state,
  repository,
  check,
  checking,
  checkError,
  onCheck,
  onPrepare,
  preparing,
  onUpdaterOnly,
  updatingUpdater,
}: {
  state: UpdateState;
  repository: string;
  check: Upstream | null;
  checking: boolean;
  checkError: unknown;
  onCheck: () => void;
  onPrepare: (version: string) => void;
  preparing: boolean;
  onUpdaterOnly: (version: string) => void;
  updatingUpdater: boolean;
}) {
  return (
    <>
      <p className="muted small">
        <Trans>
          Asks {repository} for its published releases and compares them with the {state.running}{" "}
          this is running. Nothing leaves this instance unless an owner presses a button like this
          one, nothing is asked on a timer, and no request says anything about this instance.
        </Trans>
      </p>
      <button onClick={onCheck} disabled={checking}>
        {checking
          ? t({ message: "Asking…", comment: "Button while a request is sent" })
          : t({ message: "Check the repository", comment: "Button: look for a newer release" })}
      </button>
      <Problem error={checkError} />
      {check && (
        <CheckResult
          state={state}
          check={check}
          onPrepare={onPrepare}
          preparing={preparing}
          onUpdaterOnly={onUpdaterOnly}
          updatingUpdater={updatingUpdater}
        />
      )}
    </>
  );
}

function CheckResult({
  state,
  check,
  onPrepare,
  preparing,
  onUpdaterOnly,
  updatingUpdater,
}: {
  state: UpdateState;
  check: Upstream;
  onPrepare: (version: string) => void;
  preparing: boolean;
  onUpdaterOnly: (version: string) => void;
  updatingUpdater: boolean;
}) {
  const releases = check.releases ?? [];
  const [chosen, setChosen] = useState<string | null>(releases[0]?.version ?? null);
  const target = releases.find((one) => one.version === chosen) ?? releases[0] ?? null;
  // Every release up to the one chosen: its migrations all run (3.2).
  const included = target
    ? releases.filter((one) => compareVersions(one.version, target.version) <= 0)
    : [];
  const skipped = included.filter((one) => one !== target).map((one) => one.version);

  const offer = check.updater;
  const runningUpdater = state.heartbeat?.updater_version ?? null;
  const updaterOffered =
    state.case === "working" &&
    offer?.version &&
    (!runningUpdater || compareVersions(offer.version, runningUpdater) > 0)
      ? offer.version
      : null;

  if (check.problem) {
    return (
      <div className="banner" style={{ marginTop: 10 }}>
        {check.problem}
      </div>
    );
  }
  if (!check.newer || !target) {
    return (
      <>
        <div className="banner info" style={{ marginTop: 10 }}>
          <Trans>
            This is the newest there is: {check.running}, and the repository's latest release is{" "}
            {check.latest}.
          </Trans>
        </div>
        {updaterOffered && (
          <UpdaterOnlyOffer
            version={updaterOffered}
            note={offer?.note ?? null}
            onAsk={onUpdaterOnly}
            pending={updatingUpdater}
          />
        )}
      </>
    );
  }

  const released = target.published_at ? formatInstant(target.published_at) : null;
  const skippedText = listText(skipped);
  return (
    <div className="update-offer" style={{ marginTop: 10 }}>
      <div className="banner warn">
        <p style={{ margin: 0 }}>
          <strong>
            <Trans comment="Headline of the check: a newer release exists">{target.version} is available.</Trans>
          </strong>{" "}
          <Trans>This instance is running {check.running}.</Trans>{" "}
          {released ? <Trans comment="When a release was published">Released {released}.</Trans> : null}
        </p>
        {skipped.length > 0 && (
          <p style={{ marginBottom: 0 }}>
            {plural(skipped.length, {
              one: `Installing it also installs ${skippedText}, which it skips over: notes for both below.`,
              other: `Installing it also installs ${skippedText}, which it skips over: notes for all of them below.`,
            })}
          </p>
        )}
      </div>

      {state.case === "working" ? (
        <>
          <div className="row update-choose">
            <button
              className="primary"
              onClick={() => onPrepare(target.version)}
              disabled={preparing}
            >
              {t({
                message: `Prepare ${target.version}`,
                comment: "Button: download and check this release without installing it",
              })}
            </button>
            {releases.length > 1 && (
              <label className="small">
                <Trans comment="Before a menu of other newer releases to prepare instead">or choose:</Trans>{" "}
                <select
                  aria-label={t`Another newer release`}
                  value={target.version}
                  onChange={(event) => setChosen(event.target.value)}
                >
                  {releases.map((one) => (
                    <option key={one.version} value={one.version}>
                      {one.version}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>
          <p className="muted small">
            <Trans>
              Preparing downloads the image and checks where it came from. It does not stop
              anything or change your data.
            </Trans>
          </p>
        </>
      ) : (
        <NoUpdaterHere state={state} />
      )}

      {updaterOffered && (
        <UpdaterOnlyOffer
          version={updaterOffered}
          note={offer?.note ?? null}
          onAsk={onUpdaterOnly}
          pending={updatingUpdater}
        />
      )}

      <ReleaseNotes releases={included} />
    </div>
  );
}

function UpdaterOnlyOffer({
  version,
  note,
  onAsk,
  pending,
}: {
  version: string;
  note: string | null;
  onAsk: (version: string) => void;
  pending: boolean;
}) {
  return (
    <div className="small" data-offer="updater" style={{ marginTop: 10 }}>
      <Trans>A newer updater ({version}) is available:</Trans>{" "}
      <button onClick={() => onAsk(version)} disabled={pending}>
        {t({
          message: "Update the updater only",
          comment: "Button: replace the updater container, leave the app and its data as they are",
        })}
      </button>
      {note ? <p className="muted small">{note}</p> : null}
    </div>
  );
}

/**
 * Every release the chosen one includes, newest first. **Text, not HTML**: a
 * release body is whatever was typed on GitHub, and this page is the owner's.
 */
export function ReleaseNotes({ releases }: { releases: Release[] }) {
  return (
    <div className="release-notes-list">
      {releases.map((one) => (
        <section key={one.version} className="release-notes-one">
          <h4 className="section-title">
            {one.version}
            {one.published_at ? (
              <span className="muted small"> · {formatInstant(one.published_at)}</span>
            ) : null}
          </h4>
          <div className="release-notes" data-version={one.version}>
            {one.notes.trim() ||
              t({ message: "No notes.", comment: "A release published without release notes" })}
          </div>
        </section>
      ))}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// 3.3: the updater at work (prepare, discard, the updater replacing itself)
// --------------------------------------------------------------------------- //

function InFlight({ status, toVersion }: { status: UpdateStatus | null; toVersion: string | null }) {
  const title =
    status?.kind === "prepare"
      ? toVersion
        ? t({ message: `Preparing ${toVersion}`, comment: "Heading while the updater prepares a release" })
        : t`Preparing the update`
      : status?.kind === "discard"
        ? t`Discarding the prepared update`
        : status?.kind === "update_updater"
          ? t`Updating the updater`
          : t`Waiting for the updater to take the request…`;
  return (
    <div className="update-progress" aria-live="polite">
      <h3 className="section-title">{title}</h3>
      <Progress sentences={status?.sentences ?? []} running />
      {status?.kind === "prepare" && (
        <p className="muted small">
          <Trans>The app stays up throughout. Nothing in the ledger changes.</Trans>
        </p>
      )}
    </div>
  );
}

function Progress({ sentences, running }: { sentences: string[]; running: boolean }) {
  if (sentences.length === 0) return null;
  return (
    <ul className="update-steps">
      {sentences.map((one, at) => {
        const current = running && at === sentences.length - 1;
        return (
          <li key={`${at}-${one}`} className={current ? "current" : "done"}>
            <span aria-hidden="true">{current ? "…" : "✓"}</span> {one}
          </li>
        );
      })}
    </ul>
  );
}

// --------------------------------------------------------------------------- //
// 3.4: confirming
// --------------------------------------------------------------------------- //

export function Confirm({
  report,
  heartbeat,
  repository,
  commit,
  onApplied,
  onDiscarded,
}: {
  report: Report;
  heartbeat: Heartbeat | null;
  repository: string;
  commit: string | null;
  onApplied: (toVersion: string) => void;
  onDiscarded: () => void;
}) {
  const { t } = useLingui();
  // One code per drawing of the confirmation, by POST (R21). A query rather
  // than an effect so it is asked once per report however often this renders,
  // and never again on focus.
  const code = useQuery({
    queryKey: ["application", "update", "recovery-code", report.id],
    queryFn: () => api.post<RecoveryCode>(`${BASE}/recovery-code`),
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  const [ticked, setTicked] = useState<Set<string>>(new Set());
  const [saved, setSaved] = useState(false);
  const [proof, setProof] = useState<StepUpProof>(NO_PROOF);

  const lossy = report.lossy;
  const allTicked = lossy.every((revision) => ticked.has(revision));
  const ready =
    allTicked &&
    saved &&
    stepUpReady(proof) &&
    !!code.data &&
    !!report.digest &&
    !!report.updater_digest;

  const apply = useMutation({
    mutationFn: async () => {
      const token = await stepUpToken(proof);
      setProof(spent);
      return api.post<UpdateRequest>(`${BASE}/apply`, {
        prepared_id: report.id,
        digest: report.digest,
        updater_digest: report.updater_digest,
        accepted_lossy: lossy,
        recovery_code_id: code.data!.id,
        step_up_token: token,
      });
    },
    onSuccess: () => onApplied(report.to_version),
  });
  const discard = useMutation({
    mutationFn: () => api.post<UpdateRequest>(`${BASE}/discard`, { prepared_id: report.id }),
    onSuccess: onDiscarded,
  });
  const newCode = () => {
    setSaved(false);
    void code.refetch();
  };

  const app = attested(report, "app");
  const updaterChanges =
    !!report.updater_digest && report.updater_digest !== heartbeat?.image_digest;
  const toggle = (revision: string, on: boolean) =>
    setTicked((was) => {
      const next = new Set(was);
      if (on) next.add(revision);
      else next.delete(revision);
      return next;
    });

  const from = report.from_version;
  const to = report.to_version;
  const shortCommit = commit ? commit.slice(0, 7) : null;
  const toCommit = app.commit ? app.commit.slice(0, 7) : null;
  const digest = shortDigest(report.digest);

  return (
    <div className="update-confirm">
      <h3 className="section-title">
        <Trans comment="Heading on the Updates screen">Update to {to}</Trans>
      </h3>
      <div className="table-scroll">
        <table className="update-facts">
          <tbody>
            <tr>
              <th scope="row">{t({ message: "Running now", comment: "Row heading in the update confirmation" })}</th>
              <td>
                {from}
                {shortCommit ? <span className="mono"> ({shortCommit})</span> : null}
              </td>
            </tr>
            <tr>
              <th scope="row">{t({ message: "Will run", comment: "Row heading in the update confirmation" })}</th>
              <td>
                {to}
                {toCommit ? <span className="mono"> ({toCommit})</span> : null},{" "}
                <Trans comment="The container image of a release, by its digest">
                  image <span className="mono">{digest}</span>
                </Trans>
              </td>
            </tr>
            <tr>
              <th scope="row">{t({ message: "Origin", comment: "Row heading in the update confirmation: where the image was built" })}</th>
              <td>
                <Trans>
                  Built by <span className="mono">.github/workflows/release.yml</span> at{" "}
                  <span className="mono">v{to}</span> in {app.repository ?? repository}. Checked by
                  the updater when it was prepared.
                </Trans>
              </td>
            </tr>
            <tr>
              <th scope="row">{t({ message: "Updater", comment: "Row heading in the update confirmation" })}</th>
              <td>
                {updaterChanges ? (
                  <Trans>
                    The updater that ships with {to} was checked too, and takes over the update
                    before anything is stopped.
                  </Trans>
                ) : (
                  <Trans>Stays as it is.</Trans>
                )}
              </td>
            </tr>
            <tr>
              <th scope="row">{t({ message: "Downtime", comment: "Row heading in the update confirmation" })}</th>
              <td>
                <Trans>
                  A few minutes. The app is stopped while the backup is taken and the migrations
                  run.
                </Trans>
              </td>
            </tr>
            {onALaptop(heartbeat) && (
              <tr>
                <th scope="row">{t({ message: "On a laptop", comment: "Row heading in the update confirmation" })}</th>
                <td>
                  <Trans>
                    Keep it plugged in and awake until this finishes. If it sleeps, the update
                    resumes or rolls back when it wakes.
                  </Trans>
                </td>
              </tr>
            )}
            <tr>
              <th scope="row">{t({ message: "Backup", comment: "Row heading in the update confirmation" })}</th>
              <td>
                <Trans>Taken after the app stops, verified, kept in the data volume.</Trans>
              </td>
            </tr>
            <tr>
              <th scope="row">{t({ message: "Pre-update hook", comment: "Row heading in the update confirmation" })}</th>
              <td>
                {heartbeat?.hook
                  ? t`On: runs before the backup.`
                  : t({ message: "Off.", comment: "State of the pre-update hook" })}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <h4 className="section-title" style={{ marginTop: 14 }}>
        {plural(report.pending.length, {
          0: "No migrations will run.",
          one: "# migration will run",
          other: "# migrations will run",
        })}
      </h4>
      {report.pending.length > 0 && (
        <ul className="update-migrations small">
          {report.pending.map((one) => (
            <li key={one.revision}>
              <span className="mono">{one.revision}</span> {one.title ?? ""} —{" "}
              {one.reversible === "clean" ? (
                <em>{t({ message: "rolling back: clean", comment: "A migration a downgrade can undo" })}</em>
              ) : (
                <em>
                  <Trans comment="A migration's verdict: lossy or undeclared, a word from the server">
                    rolling back: <strong>{one.reversible}</strong>
                  </Trans>
                  {one.note ? ` — ${one.note}` : null}
                </em>
              )}
            </li>
          ))}
        </ul>
      )}

      {lossy.length > 0 && (
        <div className="banner warn update-lossy" role="group" aria-label={t`Migrations that cannot be undone`}>
          <p style={{ marginTop: 0 }}>
            <strong>
              {plural(lossy.length, {
                one: "One of these cannot be undone by a downgrade",
                other: "# of these cannot be undone by a downgrade",
              })}
            </strong>
          </p>
          <p>
            <Trans>
              If something goes wrong <strong>during</strong> this update, it rolls back by
              restoring the backup, and nothing is lost. If you decide <strong>afterwards</strong>{" "}
              that you want {from} back, the only way is that same backup, and everything written
              after the update goes with it.
            </Trans>
          </p>
          {lossy.map((revision) => (
            <label className="check" key={revision}>
              <input
                type="checkbox"
                checked={ticked.has(revision)}
                onChange={(event) => toggle(revision, event.target.checked)}
              />{" "}
              <span>
                <Trans>
                  I understand that <span className="mono">{revision}</span> cannot be undone
                  except from the backup.
                </Trans>
              </span>
            </label>
          ))}
        </div>
      )}

      <div className="update-recovery">
        <h4 className="section-title">
          <Trans>Your recovery code for this update</Trans>
        </h4>
        <Problem error={code.error} />
        {code.data ? (
          <p className="row" style={{ flexWrap: "wrap", gap: 8 }}>
            <code className="recovery-code" data-testid="recovery-code">
              {code.data.code}
            </code>
            <button
              onClick={() =>
                saveText(
                  `spend-tracker-recovery-code-${to}.txt`,
                  t`Spend Tracker: recovery code for the update from ${from} to ${to}\n\n${code.data!.code}\n\nIf the update fails and cannot undo itself, the app is replaced by a recovery page, and this code is what opens it. It works only for this update and stops working once the update has finished.\n`,
                )
              }
            >
              {t({ message: "Download as a file", comment: "Button: save the recovery code as a text file" })}
            </button>
            <button className="link" onClick={newCode} disabled={code.isFetching}>
              {t({ message: "Get a new code", comment: "Button: replace the recovery code shown" })}
            </button>
          </p>
        ) : (
          <p className="muted small">
            <Trans>Asking for a recovery code…</Trans>
          </p>
        )}
        <p className="muted small">
          <Trans>
            If the update fails and cannot undo itself, this page is replaced by a recovery page,
            and this code is what opens it. It works only for this update and stops working once the
            update has finished. It is shown once: the server keeps only a hash of it.
          </Trans>
        </p>
        <label className="check">
          <input
            type="checkbox"
            checked={saved}
            disabled={!code.data}
            onChange={(event) => setSaved(event.target.checked)}
          />{" "}
          <Trans>I have saved the recovery code.</Trans>
        </label>
      </div>

      <div className="update-stepup">
        <StepUpFields
          proof={proof}
          onChange={setProof}
          why={t`Updating stops the app and changes the ledger's schema.`}
        />
      </div>

      <div className="row update-actions">
        <button className="primary" disabled={!ready || apply.isPending} onClick={() => apply.mutate()}>
          {apply.isPending
            ? t({ message: "Starting…", comment: "Button while the update request is sent" })
            : t({ message: `Update to ${to}`, comment: "Button: install the prepared release" })}
        </button>
        <button disabled={apply.isPending || discard.isPending} onClick={() => discard.mutate()}>
          {t({ message: "Discard", comment: "Button: throw the prepared update away; deletes the downloaded images" })}
        </button>
      </div>
      <Problem error={apply.error ?? discard.error} />
      {apply.error ? (
        <p className="small">
          <button className="link" onClick={newCode}>
            {t({ message: "Get a new recovery code", comment: "Button after an expired recovery code was refused" })}
          </button>
        </p>
      ) : null}
      <p className="muted small">
        <Trans>
          A prepared update goes stale after 24 hours, or as soon as the running version changes.
          Discard removes it and asks the updater to delete the images it downloaded.
        </Trans>
      </p>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// 3.5: updating
// --------------------------------------------------------------------------- //

type Phase = "up" | "away";

/**
 * The full-width panel that takes over once *Update* has been pressed.
 *
 * While the app still answers, it reads the update state every two seconds
 * and shows the updater's sentences. When the app stops answering, it polls
 * `/api/health` every three seconds, keeping the last sentences it saw. When
 * the app answers again -- or the update ended without stopping it -- the page
 * loads again on Application management (`backToThisScreen`), where the
 * section opens with the outcome. After 30 minutes away it
 * says so and points at the recovery page, and keeps waiting.
 *
 * Plain `fetch` rather than `api`: a maintenance page answering for the app
 * must not be read as the session ending.
 */
export function Updating({
  toVersion,
  initial,
  onBack,
}: {
  toVersion: string | null;
  initial: string[];
  onBack: () => void;
}) {
  const [phase, setPhase] = useState<Phase>("up");
  const [sentences, setSentences] = useState<string[]>(initial);
  const [notBack, setNotBack] = useState(false);
  const back = useRef(onBack);
  back.current = onBack;

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let awaySince: number | null = null;
    let current: Phase = "up";

    const later = (ms: number) => {
      if (!stopped) timer = setTimeout(tick, ms);
    };
    const goAway = () => {
      if (awaySince === null) awaySince = Date.now();
      current = "away";
      setPhase("away");
    };

    async function tick() {
      if (current === "up") {
        try {
          const answer = await fetch(`/api${BASE}`, { credentials: "same-origin" });
          if (!answer.ok) throw new Error(String(answer.status));
          const it = (await answer.json()) as UpdateState;
          if (stopped) return;
          if (it.status?.sentences?.length) setSentences(it.status.sentences);
          if (!it.in_flight) {
            // Over without the app going away (refused before it stopped),
            // or back so quickly that no poll saw it gone.
            back.current();
            return;
          }
        } catch {
          if (stopped) return;
          goAway();
        }
        later(current === "up" ? APPLY_POLL_MS : HEALTH_POLL_MS);
        return;
      }
      try {
        const answer = await fetch("/api/health", { credentials: "same-origin" });
        const body = answer.ok ? ((await answer.json()) as { status?: string }) : null;
        if (stopped) return;
        if (body?.status === "ok") {
          back.current();
          return;
        }
      } catch {
        // Still away.
      }
      if (stopped) return;
      if (awaySince !== null && Date.now() - awaySince >= NOT_BACK_AFTER_MS) setNotBack(true);
      later(HEALTH_POLL_MS);
    }

    later(APPLY_POLL_MS);
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  return (
    <div className="update-takeover" role="dialog" aria-modal="true" aria-labelledby="updating-title">
      <div className="update-takeover-body">
        <h2 id="updating-title">
          {toVersion ? (
            <Trans comment="Heading of the panel shown while an update runs">Updating to {toVersion}</Trans>
          ) : (
            <Trans comment="Heading of the panel shown while an update runs">Updating</Trans>
          )}
        </h2>
        <p>
          <Trans>
            The app is about to stop. This page keeps checking and reloads when it is back. Keep
            this computer awake and Docker running until it says it has finished.
          </Trans>
        </p>
        <div aria-live="polite">
          <Progress sentences={sentences} running />
          {phase === "away" && (
            <p className="muted small">
              <Trans>The app has stopped. Waiting for it to come back…</Trans>
            </p>
          )}
        </div>
        {notBack && (
          <div className="banner warn" role="status">
            <Trans>
              The app has not come back yet. If your computer slept or Docker restarted, the update
              carries on when they are back. If this page says the same in an hour, open the
              recovery page with your recovery code.
            </Trans>{" "}
            <a href="/recovery">
              <Trans>Open the recovery page</Trans>
            </a>
          </div>
        )}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// 3.8: the outcome
// --------------------------------------------------------------------------- //

export function OutcomeBlock({
  outcome,
  running,
  heartbeat,
  lastPrepared,
  onPrepare,
  onChanged,
}: {
  outcome: Outcome;
  running: string;
  heartbeat: Heartbeat | null;
  lastPrepared: string | null;
  onPrepare: (version: string) => void;
  onChanged: () => void;
}) {
  const { t } = useLingui();
  const seen = useMutation({
    mutationFn: () => api.post<null>(`${BASE}/outcome/${encodeURIComponent(outcome.id)}/seen`),
    onSuccess: onChanged,
  });
  const retryUpdater = useMutation({
    mutationFn: () => api.post<UpdateRequest>(`${BASE}/updater`, { to_version: running }),
    onSuccess: onChanged,
  });

  const said = outcome.sentence ?? "";
  const at = timeOf(outcome.finished_at);
  const took = duration(outcome.duration_s);
  const failed = outcome.state !== "succeeded";
  let body: ReactNode;
  let tone = "banner info";

  if (outcome.kind === "apply") {
    switch (outcome.state) {
      case "succeeded": {
        const updaterVersion = heartbeat?.updater_version ?? null;
        const stayed = !!updaterVersion && compareVersions(updaterVersion, running) < 0;
        body = (
          <>
            <p style={{ marginTop: 0 }} data-outcome="updated">
              {took ? (
                <Trans>
                  Updated to {running} at {at} in {took}.
                </Trans>
              ) : (
                <Trans>
                  Updated to {running} at {at}.
                </Trans>
              )}{" "}
              {stayed ? (
                <>
                  <Trans>The updater stayed on {updaterVersion} and will try again:</Trans>{" "}
                  <button disabled={retryUpdater.isPending} onClick={() => retryUpdater.mutate()}>
                    {t({ message: "Retry updater update", comment: "Button: try replacing the updater again" })}
                  </button>
                </>
              ) : updaterVersion ? (
                <Trans>The updater is now {updaterVersion} too.</Trans>
              ) : null}
            </p>
            <p style={{ marginBottom: 0 }}>
              <Trans>
                The backup taken before it is listed under Database; keep it for at least a day of
                use.
              </Trans>
            </p>
          </>
        );
        break;
      }
      case "rolled_back": {
        tone = "banner warn";
        const step = outcome.failed_step ?? t({ message: "an unknown step", comment: "Where an update failed, when the updater did not say" });
        const stopped = timeOf(outcome.started_at);
        body = (
          <div data-outcome="rolled_back">
            <p style={{ marginTop: 0 }}>
              <Trans>
                The update failed at {step}, so it was undone. You are on {running}, with the ledger
                exactly as it was at {stopped}, when the app stopped. What failed:
              </Trans>
            </p>
            {said ? <p>{said}</p> : null}
            {outcome.log_tail.length > 0 && (
              <pre className="log-dump" tabIndex={0} aria-label={t`The end of the update's log`}>
                {outcome.log_tail.join("\n")}
              </pre>
            )}
            <p style={{ marginBottom: 0 }}>
              <Trans>The image is kept so you can try again.</Trans>
            </p>
          </div>
        );
        break;
      }
      case "recovered":
        tone = "banner warn";
        body = (
          <p style={{ margin: 0 }} data-outcome="recovered">
            <Trans>The recovery page was used for the last update.</Trans> {said}{" "}
            {outcome.backup ? (
              <Trans comment="Which backup folder the recovery page restored">
                Restored from <span className="mono">{outcome.backup}</span>.
              </Trans>
            ) : null}{" "}
            <Trans>This instance now runs {running}.</Trans>
          </p>
        );
        break;
      case "left_for_operator":
        tone = "banner warn";
        body = (
          <p style={{ margin: 0 }} data-outcome="left_for_operator">
            <Trans>The update was left to be finished from a terminal.</Trans> {said}
          </p>
        );
        break;
      default:
        // `not_started` and `refused`: nothing was touched.
        tone = "banner warn";
        body = (
          <p style={{ margin: 0 }} data-outcome="not_started">
            <Trans>The update did not start: {said} Nothing was changed.</Trans>
          </p>
        );
    }
  } else if (outcome.kind === "prepare" && failed) {
    tone = "banner warn";
    body = (
      <p style={{ margin: 0 }} data-outcome="prepare_failed">
        <Trans>Preparing the update failed: {said} Nothing was changed.</Trans>{" "}
        {lastPrepared ? (
          <button onClick={() => onPrepare(lastPrepared)}>
            {t({ message: "Try again", comment: "Button: prepare the same release again" })}
          </button>
        ) : null}
      </p>
    );
  } else {
    // The updater's own sentence, for the requests that change nothing the
    // owner has: a discard, a prepare that worked, the updater replacing itself.
    tone = failed ? "banner warn" : "banner info";
    body = (
      <p style={{ margin: 0 }} data-outcome={outcome.kind ?? "other"}>
        {said || outcome.state}
      </p>
    );
  }

  return (
    <div className={`${tone} update-outcome`} role="status">
      {body}
      <p style={{ marginBottom: 0 }}>
        <button className="link" disabled={seen.isPending} onClick={() => seen.mutate()}>
          {t({ message: "Dismiss", comment: "Button: hide this update outcome" })}
        </button>
      </p>
      <Problem error={seen.error ?? retryUpdater.error} />
    </div>
  );
}
