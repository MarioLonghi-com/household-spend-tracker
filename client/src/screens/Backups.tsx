/**
 * The backups list on the Application screen: download, save to a cloud
 * folder, delete (#133).
 *
 * Every backup in the list gets the same three actions, the ones made before
 * this screen could download them included -- the zip is built when it is
 * asked for, not when the backup was made.
 *
 * **`secret.key` is opt-in**, one tick box above the list that governs both
 * the download and the save. Off by default and not remembered: it is a
 * choice about one zip, and a box that stays ticked is how a key ends up in
 * a shared folder nobody meant it to reach.
 *
 * **And it costs both factors** (#204). A zip with the key opens every
 * member's authenticator, so like minting an agent key it spends a step-up
 * grant, bought inside the click that uses it. No link can carry one, so once
 * the box is ticked each row's Download opens the save panel, which asks for
 * the password and a code and fetches the zip itself.
 */

import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { bytes } from "../lib/bytes";
import {
  availableRoutes,
  backupDownloadUrl,
  CLOUD_PAGES,
  downloadZip,
  fetchZip,
  saveZipToFolder,
  zipName,
  type Grant,
  type SaveRoute,
} from "../lib/saveBackup";
import { formatInstant } from "../lib/time";
import {
  Dialog,
  Panel,
  Problem,
  SortHeading,
  sortRows,
  useSort,
  type SortKeyPart,
} from "../components/bits";
import { plural } from "@lingui/core/macro";
import { Trans, useLingui } from "@lingui/react/macro";
import {
  NO_PROOF,
  spent,
  StepUpFields,
  stepUpReady,
  stepUpToken,
  type StepUpProof,
} from "../components/StepUp";

export interface Backup {
  name: string;
  path: string;
  bytes: number;
  made_at: string;
  /** `file` from this screen, `update` from an update's drill, `folder` by hand. */
  kind?: "file" | "update" | "folder";
  version?: string | null;
  revision?: string | null;
  /** One of the newest five update backups: the server will not delete it. */
  protected?: boolean;
}

export function BackupList({
  backups,
  onChanged,
}: {
  backups: Backup[];
  onChanged: () => void;
}) {
  const client = useQueryClient();
  const [withKey, setWithKey] = useState(false);
  const [saving, setSaving] = useState<Backup | null>(null);
  const [deleting, setDeleting] = useState<Backup | null>(null);

  const order = useSort<"name" | "date" | "size">("date", "desc");
  const rows = useMemo(
    () =>
      sortRows(backups, order.sort, order.direction, (one, column) =>
        column === "name" ? one.name : column === "date" ? one.made_at : one.bytes,
      ),
    [backups, order.sort, order.direction],
  );
  const newest = useMemo(
    () => backups.reduce<Backup | null>((a, b) => (a && a.made_at >= b.made_at ? a : b), null),
    [backups],
  );

  const remove = useMutation({
    mutationFn: (one: Backup) =>
      api.del<null>(`/admin/application/backups/${encodeURIComponent(one.name)}`),
    onSuccess: () => {
      setDeleting(null);
      client.invalidateQueries({ queryKey: ["application", "backups"] });
      onChanged();
    },
  });

  if (backups.length === 0) {
    return (
      <p className="muted small" style={{ marginTop: 10, marginBottom: 0 }}>
        Nothing backed up yet.
      </p>
    );
  }

  return (
    <>
      <label className="check" style={{ marginTop: 12 }}>
        <input
          type="checkbox"
          checked={withKey}
          onChange={(event) => setWithKey(event.target.checked)}
        />{" "}
        Include <span className="mono">secret.key</span> in downloads
      </label>
      <p className={withKey ? "small danger-text" : "muted small"} style={{ marginTop: 4 }}>
        {withKey
          ? "The zip will hold the key that decrypts every authenticator. With it and a member's password, anyone holding the zip can sign in as that member once it is restored. Keep it somewhere only you can open. Each download asks for your password and a code."
          : "Left out, the zip restores onto this instance as it is. Anywhere else, every authenticator is refused unless you supply the key separately, and the zip's README says how."}
      </p>

      <div className="table-scroll" style={{ marginTop: 10 }}>
        <table>
          <thead>
            <tr>
              <SortHeading label="Backup" column="name" {...order} />
              <SortHeading label="Made" column="date" {...order} />
              <SortHeading label="Size" column="size" align="right" {...order} />
              <th className="row-actions backup-actions">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((one) => (
              <tr key={one.name}>
                <td data-primary="true" className="mono small">
                  {one.name}
                </td>
                <td className="small muted" data-label="Made">
                  {formatInstant(one.made_at)}
                </td>
                <td className="amount muted" data-label="Size">
                  {bytes(one.bytes)}
                </td>
                <td className="row-actions backup-actions">
                  {withKey ? (
                    // No link can carry a step-up grant, so the key's download
                    // goes through the panel that asks for one.
                    <button className="link" onClick={() => setSaving(one)}>
                      Download…
                    </button>
                  ) : (
                    <a href={backupDownloadUrl(one.name)} download={zipName(one.name)}>
                      Download
                    </a>
                  )}
                  <button className="link" onClick={() => setSaving(one)}>
                    Save to Drive/Dropbox…
                  </button>
                  <button className="link danger" onClick={() => setDeleting(one)}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {saving && (
        <SavePanel backup={saving} withKey={withKey} onClose={() => setSaving(null)} />
      )}

      {deleting && (
        <Dialog title="Delete this backup?" onClose={() => setDeleting(null)}>
          <p style={{ marginTop: 0 }}>
            <span className="mono">{deleting.name}</span>, made {formatInstant(deleting.made_at)},{" "}
            {bytes(deleting.bytes)}. The file is removed from the backups directory and{" "}
            <strong>cannot be brought back</strong> — a backup is a file, not a ledger row, so
            there is no undo in History.
          </p>
          {backups.length === 1 ? (
            <p className="danger-text">
              <strong>This is the only backup.</strong> After this there is none: nothing on this
              instance could put the ledger back if it were damaged.
            </p>
          ) : newest?.name === deleting.name ? (
            <p className="danger-text">
              <strong>This is the newest backup.</strong> The next newest is older, so a restore
              would lose everything since then.
            </p>
          ) : null}
          <div className="dialog-choices">
            <button
              className="danger"
              disabled={remove.isPending}
              onClick={() => remove.mutate(deleting)}
            >
              {remove.isPending ? "Deleting…" : "Yes, delete it"}
            </button>
            <button disabled={remove.isPending} onClick={() => setDeleting(null)}>
              Keep it
            </button>
          </div>
          <Problem error={remove.error} />
        </Dialog>
      )}
    </>
  );
}

// --------------------------------------------------------------------------- //
// Save to Drive or Dropbox
// --------------------------------------------------------------------------- //

/**
 * The routine for putting one backup somewhere off this machine.
 *
 * It offers only what this browser can actually do (see `lib/saveBackup.ts`),
 * best first, and always ends with the one that works everywhere. Nothing is
 * sent to Google or Dropbox by this page: the file goes through the share
 * sheet, the file picker or a download, all of which are the person's own.
 */
export function SavePanel({
  backup,
  withKey,
  onClose,
  routes = availableRoutes(),
}: {
  backup: Backup;
  withKey: boolean;
  onClose: () => void;
  /** Injected by tests; the real page asks the browser. */
  routes?: SaveRoute[];
}) {
  const [prepared, setPrepared] = useState<File | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [proof, setProof] = useState<StepUpProof>(NO_PROOF);

  // Only with the key, and only at the moment a zip is asked for. The code is
  // burned by the grant it buys, so the field empties for the next one.
  const grant: Grant | undefined = withKey
    ? async () => {
        const token = await stepUpToken(proof);
        setProof(spent);
        return token;
      }
    : undefined;
  // Nothing to ask for without the key; with it, both factors first.
  const ready = !withKey || stepUpReady(proof);

  const prepare = useMutation({
    mutationFn: () => fetchZip(backup.name, withKey, grant),
    onSuccess: (file) => setPrepared(file),
  });
  const share = useMutation({
    mutationFn: async (file: File) => {
      try {
        await navigator.share({ files: [file], title: file.name });
        return true;
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return false;
        throw error;
      }
    },
    onSuccess: (shared) => shared && setDone("Handed to the share sheet."),
  });
  const folder = useMutation({
    mutationFn: () => saveZipToFolder(backup.name, withKey, grant),
    onSuccess: (saved) => saved && setDone("Saved. Your sync app will upload it from there."),
  });
  const download = useMutation({
    mutationFn: () => downloadZip(backup.name, withKey, grant),
    onSuccess: () => setDone("Downloaded."),
  });

  const secure = typeof window !== "undefined" && window.isSecureContext;
  let step = 0;

  return (
    <Panel title="Save to Google Drive or Dropbox" onClose={onClose}>
      <p className="muted small" style={{ marginTop: 0 }}>
        <span className="mono">{zipName(backup.name)}</span>, from a backup made{" "}
        {formatInstant(backup.made_at)} ({bytes(backup.bytes)} before zipping),{" "}
        {withKey ? (
          <strong className="danger-text">with secret.key</strong>
        ) : (
          <>without secret.key</>
        )}
        . This page never talks to Google or Dropbox and needs no key or sign-in of theirs: the
        file goes through your own browser, apps and accounts.
      </p>

      {withKey && (
        <section className="save-route">
          <StepUpFields
            proof={proof}
            onChange={setProof}
            why="A zip with secret.key opens every member's authenticator, wherever it ends up, long after this browser is closed."
          />
        </section>
      )}

      {routes.includes("share") && (
        <section className="save-route">
          <h3 className="section-title">
            {++step}. Share it to the Drive or Dropbox app
          </h3>
          <p className="muted small">
            Opens your device's share sheet. Pick Drive or Dropbox there, and choose the folder in
            that app.
          </p>
          {prepared ? (
            <button
              className="primary"
              disabled={share.isPending}
              onClick={() => share.mutate(prepared)}
            >
              Share {prepared.name}…
            </button>
          ) : (
            <button disabled={!ready || prepare.isPending} onClick={() => prepare.mutate()}>
              {prepare.isPending ? "Preparing the zip…" : "Prepare the zip"}
            </button>
          )}
          <Problem error={prepare.error ?? share.error} />
        </section>
      )}

      {routes.includes("folder") && (
        <section className="save-route">
          <h3 className="section-title">
            {++step}. Save it into your synced Drive or Dropbox folder
          </h3>
          <p className="muted small">
            If <em>Google Drive for desktop</em> or the Dropbox app is installed, it keeps a folder
            in sync — usually <span className="mono">Google Drive</span> or{" "}
            <span className="mono">Dropbox</span> in your home folder. Pick it, and the app
            uploads the zip from there.
          </p>
          <button disabled={!ready || folder.isPending} onClick={() => folder.mutate()}>
            {folder.isPending ? "Saving…" : "Choose a folder…"}
          </button>
          <Problem error={folder.error} />
        </section>
      )}

      <section className="save-route">
        <h3 className="section-title">
          {++step}. {step === 1 ? "Download it, then upload it" : "Or download it and upload it"}
        </h3>
        <ol className="small" style={{ paddingLeft: 18 }}>
          <li>
            {withKey ? (
              <>
                <button
                  className="link"
                  disabled={!ready || download.isPending}
                  onClick={() => download.mutate()}
                >
                  {download.isPending ? "Downloading…" : `Download ${zipName(backup.name)}`}
                </button>
                <Problem error={download.error} />
              </>
            ) : (
              <>
                <a href={backupDownloadUrl(backup.name)} download={zipName(backup.name)}>
                  Download {zipName(backup.name)}
                </a>
                .
              </>
            )}
          </li>
          <li>
            Open the service in a new tab:{" "}
            {CLOUD_PAGES.map((page, index) => (
              <span key={page.href}>
                {index > 0 ? " or " : ""}
                <a href={page.href} target="_blank" rel="noopener noreferrer">
                  {page.label}
                </a>
              </span>
            ))}
            .
          </li>
          <li>Drag the zip from your downloads onto that page, into the folder you keep it in.</li>
        </ol>
      </section>

      {!secure && (
        <p className="muted small">
          Sharing and saving straight into a folder need this page on https or on localhost. Over
          plain http on a network address the browser switches both off, which is why only the
          download is offered here.
        </p>
      )}

      {done && (
        <p className="small" role="status">
          {done}
        </p>
      )}
    </Panel>
  );
}

// --------------------------------------------------------------------------- //
// Update backups (#166, design notes 8.7)
// --------------------------------------------------------------------------- //

/** `X.Y.Z` as numbers, so 0.10.0 sorts after 0.9.0. */
function versionKey(version: string | null | undefined): SortKeyPart[] | null {
  if (!version) return null;
  return version.split(".").map((part) => Number(part) || 0);
}

/**
 * The folders an update's drill took before it migrated, listed apart from
 * the backups an owner makes, with the version and the migration each holds.
 *
 * **The newest five are kept**: they are what a failed update is undone from
 * and what the recovery page restores, and the updater prunes the older ones
 * itself after a successful update. So *Delete* is offered only on the older
 * ones, and the server refuses the newest five whatever is sent
 * (`backup.protected`).
 */
export function UpdateBackupList({
  backups,
  onChanged,
}: {
  backups: Backup[];
  onChanged: () => void;
}) {
  const { t } = useLingui();
  const client = useQueryClient();
  const [deleting, setDeleting] = useState<Backup | null>(null);
  const order = useSort<"name" | "version" | "revision" | "date" | "size">("date", "desc");
  const rows = useMemo(
    () =>
      sortRows(backups, order.sort, order.direction, (one, column) =>
        column === "name"
          ? one.name
          : column === "version"
            ? versionKey(one.version)
            : column === "revision"
              ? (one.revision ?? null)
              : column === "date"
                ? one.made_at
                : one.bytes,
      ),
    [backups, order.sort, order.direction],
  );
  const remove = useMutation({
    mutationFn: (one: Backup) =>
      api.del<null>(`/admin/application/backups/${encodeURIComponent(one.name)}`),
    onSuccess: () => {
      setDeleting(null);
      client.invalidateQueries({ queryKey: ["application", "backups"] });
      onChanged();
    },
  });

  if (backups.length === 0) return null;
  const kept = backups.filter((one) => one.protected).length;

  return (
    <div className="update-backups">
      <h3 className="section-title" style={{ marginTop: 18 }}>
        <Trans>Backups taken by updates</Trans>
      </h3>
      <p className="muted small" style={{ marginTop: 0 }}>
        {plural(kept, {
          one: "Each update backs the ledger up after the app stops and before it migrates. The newest one is kept so an update can be undone; older ones can be deleted here, and the updater removes them itself after a successful update.",
          other: "Each update backs the ledger up after the app stops and before it migrates. The newest # are kept so an update can be undone; older ones can be deleted here, and the updater removes them itself after a successful update.",
        })}
      </p>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <SortHeading
                label={t({ message: "Backup", comment: "Column heading: the backup folder's name" })}
                column="name"
                {...order}
              />
              <SortHeading
                label={t({ message: "Version", comment: "Column heading: the app version that took the backup" })}
                column="version"
                {...order}
              />
              <SortHeading
                label={t({ message: "Revision", comment: "Column heading: the database migration the backup is at" })}
                column="revision"
                {...order}
              />
              <SortHeading
                label={t({ message: "Made", comment: "Column heading: when the backup was taken" })}
                column="date"
                {...order}
              />
              <SortHeading
                label={t({ message: "Size", comment: "Column heading: the backup's size on disk" })}
                column="size"
                align="right"
                {...order}
              />
              <th className="row-actions backup-actions">
                <span className="sr-only">
                  {t({ message: "Actions", comment: "Hidden column heading over row buttons" })}
                </span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((one) => (
              <tr key={one.name} data-protected={one.protected ? "true" : "false"}>
                <td data-primary="true" className="mono small">
                  {one.name}
                </td>
                <td className="small" data-label={t({ message: "Version", comment: "Column heading: the app version that took the backup" })}>
                  {one.version ?? "—"}
                </td>
                <td className="mono small" data-label={t({ message: "Revision", comment: "Column heading: the database migration the backup is at" })}>
                  {one.revision ?? "—"}
                </td>
                <td className="small muted" data-label={t({ message: "Made", comment: "Column heading: when the backup was taken" })}>
                  {formatInstant(one.made_at)}
                </td>
                <td className="amount muted" data-label={t({ message: "Size", comment: "Column heading: the backup's size on disk" })}>
                  {bytes(one.bytes)}
                </td>
                <td className="row-actions backup-actions">
                  <a href={backupDownloadUrl(one.name)} download={zipName(one.name)}>
                    {t({ message: "Download", comment: "Link: download this backup as a zip" })}
                  </a>
                  {one.protected ? (
                    <span
                      className="tag"
                      title={t`One of the newest five update backups: kept so an update can be undone`}
                    >
                      {t({ message: "kept", comment: "Tag on an update backup that cannot be deleted" })}
                    </span>
                  ) : (
                    <button className="link danger" onClick={() => setDeleting(one)}>
                      {t({ message: "Delete", comment: "Button: delete this backup" })}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {deleting && (
        <Dialog
          title={t`Delete this update backup?`}
          onClose={() => setDeleting(null)}
        >
          <p style={{ marginTop: 0 }}>
            <Trans>
              <span className="mono">{deleting.name}</span>, taken by the update from{" "}
              {deleting.version ?? "?"}, {bytes(deleting.bytes)}. The folder is removed and{" "}
              <strong>cannot be brought back</strong>. The five newest update backups are kept
              whatever happens to this one.
            </Trans>
          </p>
          <div className="dialog-choices">
            <button
              className="danger"
              disabled={remove.isPending}
              onClick={() => remove.mutate(deleting)}
            >
              {remove.isPending
                ? t({ message: "Deleting…", comment: "Button while a backup is deleted" })
                : t({ message: "Yes, delete it", comment: "Button confirming a backup's deletion" })}
            </button>
            <button disabled={remove.isPending} onClick={() => setDeleting(null)}>
              {t({ message: "Keep it", comment: "Button cancelling a backup's deletion" })}
            </button>
          </div>
          <Problem error={remove.error} />
        </Dialog>
      )}
    </div>
  );
}
