/**
 * Getting a backup's zip into Google Drive or Dropbox, with no keys (#133).
 *
 * Every official route -- Google Picker, Dropbox Chooser and Saver -- needs an
 * app key or an OAuth client registered to somebody, and Dropbox Saver fetches
 * the file from a public URL, which a household instance on a LAN or on
 * localhost does not have. So nothing here talks to either service. What the
 * browser itself offers, in the order the panel tries them:
 *
 * 1. **Share** -- the Web Share API with a file, which opens the phone's (or
 *    Safari's) share sheet, where the Drive and Dropbox apps are. Only where
 *    the browser says it can share *this kind of file*.
 * 2. **Save into a folder** -- `showSaveFilePicker`, Chromium only, pointed at
 *    the folder *Google Drive for desktop* or the Dropbox app keeps in sync.
 *    The zip is streamed to disk, never held whole in memory.
 * 3. **Download, then upload** -- always there: the ordinary download, and
 *    the service's own page opened in a new tab to drop it on.
 *
 * Both of the first two need a secure context: https, or localhost. On
 * `http://<LAN address>` neither exists, and the panel says why rather than
 * showing a button that does nothing.
 */

export type SaveRoute = "share" | "folder" | "download";

/**
 * The plain download link for one backup: a GET, which never carries the key.
 *
 * A zip *with* `secret.key` is a POST that spends a step-up grant (#204), so
 * it cannot be a link at all -- see `requestZip`.
 */
export function backupDownloadUrl(name: string): string {
  return `/api/admin/application/backups/${encodeURIComponent(name)}/download`;
}

/**
 * Buys a step-up grant. Called only when the key is asked for, and only at the
 * moment the zip is requested, so a grant is never bought and then left.
 */
export type Grant = () => Promise<string>;

/**
 * Ask the server for the zip. Without the key it is the same GET as the link;
 * with it, a POST carrying a grant bought just now.
 */
async function requestZip(name: string, includeKey: boolean, grant?: Grant): Promise<Response> {
  if (!includeKey) return fetch(backupDownloadUrl(name), { credentials: "same-origin" });
  if (!grant) throw new Error("a zip with secret.key needs your password and a code");
  const token = await grant();
  return fetch(backupDownloadUrl(name), {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ include_key: true, step_up_token: token }),
  });
}

export function zipName(name: string): string {
  return `${name.replace(/\.sqlite3$/, "")}.zip`;
}

/**
 * Which routes this browser really has, best first. `download` is always last
 * and always there.
 *
 * Share is probed with a zip-typed file rather than taken on `navigator.share`
 * existing: desktop Chrome has `share` and refuses a zip, and a button that
 * fails on press is worse than no button.
 */
export function availableRoutes(scope: {
  isSecureContext?: boolean;
  navigator?: Partial<Pick<Navigator, "share" | "canShare">>;
  showSaveFilePicker?: unknown;
} = globalThis as never): SaveRoute[] {
  const routes: SaveRoute[] = [];
  if (!scope.isSecureContext) return ["download"];

  const nav = scope.navigator;
  if (nav?.share && nav.canShare && typeof File !== "undefined") {
    try {
      const probe = new File([new Uint8Array(1)], "probe.zip", { type: "application/zip" });
      if (nav.canShare({ files: [probe] })) routes.push("share");
    } catch {
      // A browser that throws on the probe cannot share a zip either.
    }
  }
  if (typeof scope.showSaveFilePicker === "function") routes.push("folder");
  routes.push("download");
  return routes;
}

/** Fetch the zip whole, as a File the share sheet can take. */
export async function fetchZip(name: string, includeKey: boolean, grant?: Grant): Promise<File> {
  const response = await requestZip(name, includeKey, grant);
  if (!response.ok) throw new Error(await refusal(response));
  const blob = await response.blob();
  return new File([blob], zipName(name), { type: "application/zip" });
}

/**
 * The download route when the key is in it: no link can carry a grant, so the
 * zip is fetched and handed to the browser's own download as a blob.
 */
export async function downloadZip(name: string, includeKey: boolean, grant?: Grant): Promise<void> {
  const file = await fetchZip(name, includeKey, grant);
  const url = URL.createObjectURL(file);
  try {
    const link = document.createElement("a");
    link.href = url;
    link.download = file.name;
    document.body.appendChild(link);
    link.click();
    link.remove();
  } finally {
    // After the click has handed the blob to the download; revoking in the
    // same task is what every browser expects.
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}

type Writable = { close(): Promise<void>; abort?(reason?: unknown): Promise<void> } & WritableStream;
type Picker = (options: {
  suggestedName: string;
  types: { description: string; accept: Record<string, string[]> }[];
}) => Promise<{ createWritable(): Promise<Writable> }>;

/**
 * Ask where to save, then stream the zip there.
 *
 * The picker is opened **first**, while the click that asked for it still
 * counts as a user gesture -- fetching before it would spend that gesture and
 * the browser would refuse to show the picker. Resolves false when the person
 * cancelled the picker, which is not an error.
 */
export async function saveZipToFolder(
  name: string,
  includeKey: boolean,
  grant?: Grant,
): Promise<boolean> {
  const pick = (globalThis as unknown as { showSaveFilePicker: Picker }).showSaveFilePicker;
  let handle;
  try {
    handle = await pick({
      suggestedName: zipName(name),
      types: [{ description: "Zip archive", accept: { "application/zip": [".zip"] } }],
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") return false;
    throw error;
  }
  const writable = await handle.createWritable();
  let response: Response;
  try {
    // The grant is bought only now, after the picker: buying it first would
    // leave one unspent every time somebody cancels the picker.
    response = await requestZip(name, includeKey, grant);
  } catch (error) {
    await writable.abort?.();
    throw error;
  }
  if (!response.ok || !response.body) {
    await writable.abort?.();
    throw new Error(response.ok ? "the server sent no file" : await refusal(response));
  }
  // pipeTo closes the writable when the body ends, which is what commits it.
  await response.body.pipeTo(writable);
  return true;
}

async function refusal(response: Response): Promise<string> {
  try {
    const payload = await response.json();
    if (payload && typeof payload.detail === "string") return payload.detail;
  } catch {
    // Not JSON: fall through to the status line.
  }
  return response.statusText || `the server answered ${response.status}`;
}

/** Where to drop the file for the third route. Opened in a new tab, never fetched. */
export const CLOUD_PAGES = [
  { label: "Open Google Drive", href: "https://drive.google.com/drive/my-drive" },
  { label: "Open Dropbox", href: "https://www.dropbox.com/home" },
] as const;
