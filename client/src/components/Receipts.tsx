/**
 * The receipt frame, the metadata block, and the three ways a file gets in.
 *
 * Shared between the transaction panel and the Receipts screen, because the
 * inbox needs exactly the same frame and exactly the same *More info* — and
 * more than the panel does: an inbox receipt has no payee, no amount and no
 * transaction, so where and when it was taken is very often the only thing
 * that identifies it.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../lib/api";
import type { Receipt, ReceiptUpload } from "../lib/types";
import { formatInstant } from "../lib/time";

/** What the file picker offers. A hint, never a control — the magic-byte
 *  sniff on the server is the control, and the two must not be confused. */
export const ACCEPT =
  "image/jpeg,image/png,image/webp,image/avif,image/gif,image/tiff,image/heic,image/heif,application/pdf";

export function sizeText(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** A fix worse than this is a district, not a doorway. */
const VAGUE_METRES = 100;

function compass(degrees: number): string {
  const points = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  return points[Math.round(degrees / 22.5) % 16];
}

/**
 * The location, at the precision the accuracy actually justifies.
 *
 * A UI that presents a 4-metre fix and a 2-kilometre cell-tower fix the same
 * way teaches people to trust the wrong one.
 */
function whereText(receipt: Receipt): { text: string; query: string } | null {
  if (receipt.gps_lat === null || receipt.gps_lon === null) return null;
  const vague =
    receipt.gps_accuracy_m !== null && receipt.gps_accuracy_m > VAGUE_METRES;
  const places = vague ? 2 : 5;
  const lat = receipt.gps_lat.toFixed(places);
  const lon = receipt.gps_lon.toFixed(places);

  let text = vague ? `approximate — ${lat}, ${lon}` : `${lat}, ${lon}`;
  if (receipt.gps_accuracy_m !== null) {
    const metres = receipt.gps_accuracy_m;
    text += metres >= 1000 ? ` ±${(metres / 1000).toFixed(1)} km` : ` ±${Math.round(metres)} m`;
  }
  if (receipt.gps_bearing !== null) {
    text += ` facing ${Math.round(receipt.gps_bearing)}° ${compass(receipt.gps_bearing)}`;
  }
  // Five decimals is about a metre, and more than any fix here is worth. The
  // full float stays in the database; the link does not advertise a precision
  // the ± beside it contradicts.
  return { text, query: `${lat},${lon}` };
}

/**
 * Where a coordinate came from.
 *
 * The capture page can record the *phone's* position when the toggle is on,
 * and it lands in the same columns EXIF GPS does -- one notion of where a
 * receipt was taken, not two. What keeps that honest is this stamp: a fix from
 * the browser means "where the phone was when it was sent", which is the same
 * thing at a till and a completely different thing when a wallet is emptied on
 * the kitchen table on Sunday, and the block below says which it is looking at.
 *
 * Written by `app/services/receipts.py`; the key is spelled the same in both
 * places and nowhere else.
 */
function fromDevice(receipt: Receipt): boolean {
  return receipt.exif?.["SpendTrackerLocationSource"] === "device";
}

/**
 * A thumbnail that shows a larger copy while the pointer is on it.
 *
 * The bigger picture is fetched when the pointer arrives and not before: a
 * list of two hundred receipts would otherwise pull two hundred display
 * copies to render forty thumbnails. Focus does the same thing as hover, so a
 * keyboard reaches it; the button itself still opens the receipt, which is
 * what a tap does on a phone, where there is no hover at all.
 */
export function ReceiptPeek({
  receipt,
  onOpen,
}: {
  receipt: Receipt;
  onOpen: () => void;
}) {
  const [near, setNear] = useState(false);
  const when = (receipt.captured_at ?? receipt.created_at).slice(0, 10);

  return (
    <span className="peek-host">
      <button
        type="button"
        className="thumb"
        onClick={onOpen}
        onMouseEnter={() => setNear(true)}
        onMouseLeave={() => setNear(false)}
        onFocus={() => setNear(true)}
        onBlur={() => setNear(false)}
        aria-label={`Open the receipt from ${when}`}
      >
        <img src={`/api/receipts/${receipt.id}/thumb`} alt="" loading="lazy" />
      </button>
      {near ? (
        <span className="peek" aria-hidden="true">
          <img src={`/api/receipts/${receipt.id}/display`} alt="" />
        </span>
      ) : null}
    </span>
  );
}

/**
 * Everything the camera wrote, collapsed.
 *
 * `<details>` rather than a React toggle: keyboard-accessible,
 * screen-reader-correct, and state-free for nothing. Rows with no value are
 * skipped entirely, so a screenshot's block is two lines rather than a grid of
 * dashes — which is the common case, not the exception.
 */
export function MoreInfo({ receipt }: { receipt: Receipt }) {
  const where = whereText(receipt);
  const rows: [string, React.ReactNode][] = [];

  if (receipt.captured_at) {
    rows.push([
      "Taken",
      <>
        {receipt.captured_at_is_local
          ? receipt.captured_at.replace("T", ", ")
          : formatInstant(receipt.captured_at)}
        {receipt.captured_at_is_local ? (
          <span className="muted"> (local time, no zone recorded)</span>
        ) : null}
      </>,
    ]);
  }
  if (receipt.camera) rows.push(["Camera", receipt.camera]);
  if (where)
    rows.push([
      "Where",
      <>
        {where.text}
        <div style={{ marginTop: 4 }}>
          {/* A link out, not an embed. Tiles would mean adding a tile host to
              img-src, and then every time anybody opens a receipt a third
              party learns where it was taken — rebuilding remotely exactly the
              movement history that keeping the coordinates locally was meant
              to keep at home. Nothing is requested until somebody asks, once.

              `noopener` is mandatory: without it the opened tab gets a
              window.opener handle back into an app with one-click irreversible
              actions. `noreferrer` is belt-and-braces — Referrer-Policy is
              already no-referrer globally, but this link has to stay safe if
              that header is ever relaxed. */}
          <a
            href={`https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(where.query)}`}
            target="_blank"
            rel="noopener noreferrer"
          >
            Open in Google Maps ↗
          </a>
        </div>
        {/* Which of the two claims this is. Without it the pair of columns
            means either "where the receipt was photographed" or "where the
            phone was when it was sent", and nothing on screen could say
            which. */}
        <div className="small muted" style={{ marginTop: 2 }}>
          {fromDevice(receipt)
            ? "Recorded by the phone that sent it, not by the camera — so this is where the phone was when it went up."
            : "From the photograph's own metadata."}
        </div>
      </>,
    ]);
  rows.push([
    "Uploaded",
    <>
      {formatInstant(receipt.created_at)}
      {receipt.uploaded_by_name ? ` by ${receipt.uploaded_by_name}` : ""}
      {receipt.client_encoded ? (
        <span className="muted"> · compressed on the device before sending</span>
      ) : null}
    </>,
  ]);
  rows.push([
    "File",
    [
      receipt.original_filename,
      sizeText(receipt.byte_size),
      receipt.width && receipt.height ? `${receipt.width}×${receipt.height}` : null,
      receipt.page_count && receipt.page_count > 1 ? `${receipt.page_count} pages` : null,
      receipt.media_type,
    ]
      .filter(Boolean)
      .join(" · "),
  ]);
  rows.push(["Stored as", <span className="mono">{receipt.download_name}</span>]);
  rows.push([
    "Checksum",
    <span className="mono" title={receipt.content_sha256}>
      {receipt.content_sha256.slice(0, 12)}… <span className="muted">(SHA-256 of the original)</span>
    </span>,
  ]);

  return (
    <details className="more-info">
      <summary>More info</summary>
      <dl>
        {rows.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

/**
 * The picture, at a fixed aspect so the panel does not reflow as thumbnails
 * arrive — the same reasoning as the register holding still while its rows
 * move.
 *
 * `<img>` only, never `<object>`: the CSP has `object-src 'none'`, which is
 * why a PDF shows a server-rendered page-1 raster rather than an embedded
 * viewer. Rendering page 1 on the server gets the same result and relaxes
 * nothing.
 */
export function ReceiptFrame({
  receipt,
  preview,
  onEnlarge,
}: {
  receipt: Receipt | null;
  /** A local blob: URL, shown while the server is still encoding. */
  preview?: string | null;
  onEnlarge?: () => void;
}) {
  const source = preview ?? (receipt ? `/api/receipts/${receipt.id}/thumb` : null);
  const isPdf = receipt?.media_type === "application/pdf";

  return (
    <div className="receipt-frame">
      {source ? (
        <>
          <img
            src={source}
            alt={receipt ? `Receipt, ${receipt.download_name}` : "Uploading"}
            loading="lazy"
            width={320}
            height={427}
            onClick={onEnlarge}
            className={onEnlarge ? "can-enlarge" : undefined}
          />
          {isPdf ? (
            <span className="tag pdf-badge">
              PDF{receipt?.page_count && receipt.page_count > 1 ? ` · ${receipt.page_count} pages` : ""}
            </span>
          ) : null}
          {preview ? <span className="frame-working">Encoding…</span> : null}
        </>
      ) : (
        <span className="muted small">No receipt yet</span>
      )}
    </div>
  );
}

/** The enlarged copy, in the panel rather than a new tab — so the `attachment`
 *  disposition on the bytes route never comes into play. */
export function Lightbox({ receipt, onClose }: { receipt: Receipt; onClose: () => void }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="lightbox" onClick={onClose} role="dialog" aria-label="Receipt">
      <img src={`/api/receipts/${receipt.id}/display`} alt={receipt.download_name} />
      <button className="lightbox-close" onClick={onClose} aria-label="Close">
        ×
      </button>
    </div>
  );
}

export type UploadTarget = {
  householdId: string;
  transactionId?: string | null;
  /** When set, the named receipt is detached to the inbox in the same batch. */
  replacesId?: string | null;
};

/** How many encodes the server is asked to do at once.
 *
 *  A month of receipts is thirty photographs selected in one go, and thirty in
 *  parallel would saturate the host and contend for SQLite's single writer.
 *  Three keeps the queue moving without either. */
const AT_ONCE = 3;

type Job = {
  id: number;
  file: File;
  preview: string;
  state: "waiting" | "sending" | "done" | "failed";
  why?: string;
};

/**
 * Drag-and-drop, click-to-choose, and paste -- **any number at a time**.
 *
 * Paste matters more than it sounds: Ctrl-V of a screenshot straight into an
 * open transaction is the fastest path that exists for a card statement, and
 * it costs one handler reading `event.clipboardData.files`.
 *
 * One file per request, on purpose, and the client issues three at a time.
 * A single request carrying thirty photographs would be one 100 MB body
 * against a cap meant to bound a single upload, one transcode queue with no
 * progress until all of it finished, and one failure that abandoned the other
 * twenty-nine. Per file, each one has its own progress, its own error and its
 * own retry.
 */
export function ReceiptDrop({
  target,
  label,
  icon,
  primary = false,
  onDone,
  listenForPaste = false,
}: {
  target: UploadTarget;
  label: string;
  /** Drawn before the label and hidden from screen readers -- the label is
   *  already the name of the control, and "plus add receipts" is not. */
  icon?: string;
  primary?: boolean;
  /** Called once per successful upload, so the list behind it stays current. */
  onDone: (result: ReceiptUpload) => void;
  listenForPaste?: boolean;
}) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const input = useRef<HTMLInputElement>(null);
  const next = useRef(0);
  const running = useRef(0);
  // Read through a ref: `send` runs from a pump that outlives the render it
  // started in, and a stale `target` would attach to the wrong transaction.
  const latest = useRef(target);
  latest.current = target;
  const report = useRef(onDone);
  report.current = onDone;
  // Every preview URL still alive. A ref, not the state: the unmount cleanup
  // below runs with the first render's closure, whose `jobs` is always empty,
  // so reading the state there revoked nothing and every dropped photo stayed
  // pinned in memory for the life of the tab (#195).
  const previews = useRef(new Set<string>());

  const update = useCallback((id: number, patch: Partial<Job>) => {
    setJobs((was) => was.map((one) => (one.id === id ? { ...one, ...patch } : one)));
  }, []);

  const send = useCallback(
    async (job: Job) => {
      update(job.id, { state: "sending" });
      try {
        const form = new FormData();
        form.append("file", job.file);
        if (latest.current.transactionId)
          form.append("transaction_id", latest.current.transactionId);
        // Only the first of a batch may replace: "replace" names one receipt,
        // and applying it to all of them would detach the same row repeatedly.
        if (latest.current.replacesId && job.id === 0)
          form.append("replaces_id", latest.current.replacesId);
        const result = await api.upload<ReceiptUpload>(
          `/households/${latest.current.householdId}/receipts`,
          form,
        );
        update(job.id, { state: "done" });
        // A finished job is no longer drawn, so its preview can go now.
        URL.revokeObjectURL(job.preview);
        previews.current.delete(job.preview);
        report.current(result);
      } catch (problem) {
        // The file is never dropped: the row stays with a Retry on it.
        update(job.id, {
          state: "failed",
          why: problem instanceof ApiError ? problem.message : String(problem),
        });
      } finally {
        running.current -= 1;
      }
    },
    [update],
  );

  // One pump for the whole queue. Each render that leaves capacity starts the
  // next waiting job, so a retry re-enters the same path as a fresh file.
  useEffect(() => {
    for (const job of jobs) {
      if (running.current >= AT_ONCE) break;
      if (job.state !== "waiting") continue;
      running.current += 1;
      void send(job);
    }
  }, [jobs, send]);

  const accept = useCallback((files: FileList | File[] | null | undefined) => {
    const chosen = [...(files ?? [])];
    if (chosen.length === 0) return;
    const made = chosen.map((file) => {
      // The encode is 0.3 to 5 seconds, so the local preview goes up first
      // and the server thumbnail replaces it when the 201 lands. `blob:` is
      // in img-src for exactly this.
      const preview = URL.createObjectURL(file);
      previews.current.add(preview);
      return { id: next.current++, file, preview, state: "waiting" as const };
    });
    setJobs((was) => [...was.filter((one) => one.state !== "done"), ...made]);
  }, []);

  useEffect(() => {
    if (!listenForPaste) return;
    const onPaste = (event: ClipboardEvent) => {
      const files = event.clipboardData?.files;
      if (files && files.length > 0) {
        event.preventDefault();
        accept(files);
      }
    };
    document.addEventListener("paste", onPaste);
    return () => document.removeEventListener("paste", onPaste);
  }, [listenForPaste, accept]);

  // Whatever is left when this unmounts -- a failed row keeps its preview on
  // screen until then, so it cannot be revoked any earlier.
  useEffect(() => {
    const alive = previews.current;
    return () => {
      for (const url of alive) URL.revokeObjectURL(url);
      alive.clear();
    };
  }, []);

  const busy = jobs.filter((one) => one.state === "waiting" || one.state === "sending");
  const failed = jobs.filter((one) => one.state === "failed");

  return (
    <div
      className="receipt-drop"
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault();
        accept(event.dataTransfer.files);
      }}
    >
      <button
        className={primary ? "primary" : undefined}
        disabled={busy.length > 0}
        onClick={() => input.current?.click()}
      >
        {busy.length > 0 ? (
          `Uploading ${busy.length}…`
        ) : (
          <>
            {icon ? (
              <span className="btn-icon" aria-hidden="true">
                {icon}
              </span>
            ) : null}
            {label}
          </>
        )}
      </button>
      <input
        ref={input}
        type="file"
        accept={ACCEPT}
        multiple
        hidden
        onChange={(event) => {
          accept(event.target.files);
          event.target.value = "";
        }}
      />

      {busy.length + failed.length > 0 ? (
        <ul className="upload-queue">
          {[...busy, ...failed].map((job) => (
            <li key={job.id} className={job.state === "failed" ? "failed" : undefined}>
              <img src={job.preview} alt="" />
              <span className="grow">
                <span className="name">{job.file.name}</span>
                <span className="small muted">
                  {job.state === "sending"
                    ? "Encoding and sending…"
                    : job.state === "failed"
                      ? job.why
                      : "Waiting"}
                </span>
              </span>
              {job.state === "failed" ? (
                <button onClick={() => update(job.id, { state: "waiting", why: undefined })}>
                  Retry
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/** The paperclip in the register's Src column. */
export function ReceiptMark({ count }: { count?: number }) {
  const word = count && count > 1 ? `has ${count} receipts` : "has a receipt";
  return (
    <span className="receipt-mark" title={word} aria-label={word}>
      📎
    </span>
  );
}

export function receiptsOf(householdId: string, transactionId: string) {
  return {
    queryKey: ["receipts", householdId, transactionId],
    queryFn: () => api.get<Receipt[]>(`/transactions/${transactionId}/receipts`),
  };
}
