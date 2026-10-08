/**
 * Importing a statement.
 *
 * Upload or paste, look at what it would do, then commit. Nothing reaches the
 * register until the second step.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "../lib/api";
import { format } from "../lib/money";
import { STEP, useWindowed } from "../lib/useWindowed";
import { Dialog, Field, Problem, SortHeading, Toasts, useToasts } from "../components/bits";
import { Combobox } from "../components/Combobox";
import type { SortDirection } from "../components/bits";
import type {
  Account,
  CategoryGroup,
  Household,
  ImportLine,
  ImportPreview,
  IdentifierKind,
  OneTimePriorImport,
  Recognised,
} from "../lib/types";
import { IMPORT_OUTCOME_WORDS } from "../lib/labels";
import { formatCount, formatLocale } from "../lib/locale";
import { plural, t } from "@lingui/core/macro";
import { Plural, Trans } from "@lingui/react/macro";
import { detectedLabel } from "../lib/labels";

/**
 * One import staged and never committed, as the queue lists it.
 *
 * Declared here rather than in `lib/types.ts` because nothing outside this
 * screen has any use for it.
 */
export interface StagedImport {
  batch_id: string;
  filename: string | null;
  account_id: string;
  account_name: string | null;
  actor_name: string | null;
  staged_at: string;
  row_count: number;
  sha256: string;
}

type QueueSort = "filename" | "account" | "actor" | "staged_at" | "rows";

/** The bytes the browser still holds from the upload it just did. */
interface RawText {
  name: string;
  text: string;
  /** Before truncation, so the screen can say how much it is not showing. */
  total: number;
}

/**
 * How much of the file to render underneath the preview.
 *
 * A statement is tens of kilobytes, so this shows all of almost every real
 * one -- and the cap is what stops a mistaken 8MB upload putting eight million
 * characters into the DOM. When it bites, the screen says so rather than
 * quietly ending mid-line.
 */
export const RAW_TEXT_LIMIT = 20_000;

function isPdf(file: File): boolean {
  return file.type === "application/pdf" || /\.pdf$/i.test(file.name);
}

/**
 * Does this read as text at all?
 *
 * A bank's `.xls` export is not a PDF and is not text either; dumping its
 * bytes into the page is a screenful of replacement characters that looks like
 * a bug in this screen. Control characters in the first couple of kilobytes
 * are the cheap tell.
 */
export function looksBinary(text: string): boolean {
  const head = text.slice(0, 2000);
  if (!head) return false;
  let odd = 0;
  for (const ch of head) {
    const code = ch.codePointAt(0) ?? 0;
    if (ch === "\ufffd" || (code < 32 && ch !== "\n" && ch !== "\r" && ch !== "\t")) odd += 1;
  }
  return odd / head.length > 0.02;
}

/** One line of "The file itself", and the number the preview gives it. */
export interface RawLine {
  text: string;
  /** The preview's Line for this line, or null where the parser counts none. */
  no: number | null;
}

/**
 * Every line break Python's `str.splitlines()` knows. The parser splits on
 * all of these, so a form feed in a payee is a new line to it -- and has to be
 * one here too, or every number after it is one off.
 */
const LINE_BREAK = /\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]/g;

/**
 * Python's `str.isspace()`, which is what `line.strip()` strips. Not `\s`:
 * JavaScript counts a BOM as whitespace and Python does not, and Python counts
 * \x1c-\x1f and \x85 where JavaScript does not.
 */
const PY_SPACE = /^[\t\n\v\f\r\x1c-\x1f \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/;

/**
 * Is a quoted field still open at the end of this line?
 *
 * The parser numbers csv *records*, not lines: a quoted cell holding a line
 * break makes one record of two lines, and the second gets no number of its
 * own. This follows Python's `csv.reader` with its defaults -- a quote opens a
 * field only as the field's first character, `""` inside one is a literal
 * quote, and anything after the closing quote is plain text.
 */
function quoteOpenAfter(line: string, delimiter: string, open: boolean): boolean {
  let inQuotes = open;
  let fieldStart = !open;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (inQuotes) {
      if (ch === '"') {
        if (line[i + 1] === '"') i += 1;
        else inQuotes = false;
      }
    } else if (ch === delimiter) {
      fieldStart = true;
      continue;
    } else if (ch === '"' && fieldStart) {
      inQuotes = true;
    }
    fieldStart = false;
  }
  return inQuotes;
}

/**
 * The file as the parser counts it, cut off at `limit` characters.
 *
 * `statements/parsing.py` drops blank lines before anything else and numbers
 * the csv records that remain from 1, title block and header included. So a
 * blank line is shown but numbered null, and the count carries on after it;
 * numbering every physical line would drift from the preview's Line column on
 * the first blank one. Lines are read whole even where the view cuts one off,
 * so whether it is blank is decided by all of it, as the parser decides.
 */
export function numberRawLines(text: string, delimiter: string, limit = RAW_TEXT_LIMIT): RawLine[] {
  const lines: RawLine[] = [];
  let count = 0;
  let quoted = false;
  let start = 0;
  LINE_BREAK.lastIndex = 0;
  while (start < Math.min(text.length, limit)) {
    const found = LINE_BREAK.exec(text);
    const end = found ? found.index : text.length;
    const line = text.slice(start, end);
    let no: number | null = null;
    if (!PY_SPACE.test(line)) {
      if (!quoted) {
        count += 1;
        no = count;
      }
      quoted = quoteOpenAfter(line, delimiter, quoted);
    }
    lines.push({ text: line.slice(0, Math.max(0, limit - start)), no });
    if (!found) break;
    start = LINE_BREAK.lastIndex;
  }
  return lines;
}

/**
 * The text of what was just uploaded, from the file the browser still has.
 *
 * Deliberately not a round trip: the uploaded bytes are hashed, not retained,
 * and inventing retention for a read-only box underneath a preview would be a
 * storage decision made by a display feature. What this shows is the file in
 * memory from the upload that produced the preview on screen -- which is why
 * it is absent for an import opened from the queue, where there is no file.
 */
async function textOfUpload(file: File | null, pasted: string): Promise<RawText | null> {
  if (!file) {
    const typed = pasted ?? "";
    return typed.trim() ? { name: "pasted", text: typed, total: typed.length } : null;
  }
  if (isPdf(file)) return null;
  const text = await file.text();
  return { name: file.name, text, total: text.length };
}

/** The queue, sorted at its headings like every other list in the app. */
export function sortQueue(
  rows: StagedImport[],
  sort: QueueSort,
  direction: SortDirection,
): StagedImport[] {
  const value = (row: StagedImport): string | number => {
    switch (sort) {
      case "filename":
        return (row.filename ?? "").toLowerCase();
      case "account":
        return (row.account_name ?? "").toLowerCase();
      case "actor":
        return (row.actor_name ?? "").toLowerCase();
      case "rows":
        return row.row_count;
      default:
        return row.staged_at;
    }
  };
  const flip = direction === "desc" ? -1 : 1;
  return [...rows].sort((left, right) => {
    const a = value(left);
    const b = value(right);
    if (a === b) return left.batch_id < right.batch_id ? -1 : 1;
    return (a < b ? -1 : 1) * flip;
  });
}

/** A staged-at stamp, in the words the refusal to re-stage a file uses. */
function whenStaged(value: string): string {
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return value;
  return at.toLocaleString(formatLocale(), {
    day: "numeric",
    month: "long",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * The memo a line will actually carry, and whether somebody typed it.
 *
 * `memo` is what the bank sent and is never written over; `memo_chosen` is
 * the deliberate act laid beside it. The key being *present* is the decision,
 * so a typed-then-emptied memo is "no memo" rather than "use the bank's".
 */
export function memoOf(line: ImportLine): { text: string; chosen: boolean } {
  const parsed = (line.parsed ?? {}) as Record<string, unknown>;
  if ("memo_chosen" in parsed) {
    const chosen = parsed.memo_chosen;
    return { text: chosen == null ? "" : String(chosen), chosen: true };
  }
  const bank = parsed.memo;
  return { text: bank == null ? "" : String(bank), chosen: false };
}

const OUTCOME_WORDS = IMPORT_OUTCOME_WORDS;

type PreviewSort = "line" | "date" | "payee" | "memo" | "amount" | "category" | "outcome";

/**
 * What happens to each line, in the order the outcomes are worth reading.
 *
 * Ranked, not alphabetical. Sorting the rendered words would give
 * "created, duplicate_skipped, matched_existing, needs_review, rejected" --
 * an accident of the alphabet. Sorting by how much attention a line wants puts
 * the ones you have to decide about at one end, which is the reason to sort by
 * this column at all.
 */
const OUTCOME_RANK: Record<string, number> = {
  rejected: 0,
  needs_review: 1,
  matched_existing: 2,
  created: 3,
  duplicate_skipped: 4,
  skipped: 5,
};

/**
 * Sorting the preview.
 *
 * Done here rather than on the server, unlike the register: a preview is one
 * file's lines, they all arrived in the response, and re-reading a batch from
 * the database to put it in a different order would be a round trip to sort
 * something already in hand.
 */
function sortLines(lines: ImportLine[], sort: PreviewSort, direction: SortDirection): ImportLine[] {
  const field = (line: ImportLine, key: string) =>
    ((line.parsed ?? {}) as Record<string, unknown>)[key];

  const value = (line: ImportLine): string | number | null => {
    switch (sort) {
      case "line":
        return line.line_no;
      case "amount": {
        const amount = field(line, "amount");
        return typeof amount === "number" ? amount : null;
      }
      case "outcome":
        return OUTCOME_RANK[line.outcome] ?? 99;
      case "category":
        return line.category_name ?? null;
      case "memo": {
        // What the row shows, which is the typed memo where there is one.
        const shown = memoOf(line).text;
        return shown === "" ? null : shown;
      }
      default: {
        const raw = field(line, sort);
        return raw == null || raw === "" ? null : String(raw);
      }
    }
  };

  const flip = direction === "desc" ? -1 : 1;
  return [...lines].sort((left, right) => {
    const a = value(left);
    const b = value(right);
    // Nulls last whichever way round, as everywhere else: a line with no payee
    // is not "before A", and burying the blanks at one end either way is what
    // makes sorting by a sparse column useful.
    if (a === null && b === null) return left.line_no - right.line_no;
    if (a === null) return 1;
    if (b === null) return -1;
    if (a === b) return left.line_no - right.line_no;
    return (a < b ? -1 : 1) * flip;
  });
}

/**
 * The pointer to the One-time Import (#183), until one has been done here.
 *
 * Someone arriving from another budgeting app reaches for this screen first,
 * and statement by statement is the slow way to bring years across. So the
 * screen says where the bulk door is -- but only while no one-time import has
 * ever been done in the household. An undone one does not count: the history
 * is not in, so the pointer is still worth reading.
 */
export function OneTimeImportNote({
  household,
  onGo,
}: {
  household: Household;
  onGo?: (screen: string) => void;
}) {
  const done = useQuery({
    queryKey: ["one-time-import-history", household.id],
    queryFn: () =>
      api.get<{ imports: OneTimePriorImport[] }>(
        `/households/${household.id}/one-time-import/history`,
      ),
    retry: false,
  });
  // Nothing until the answer is in: a note that flashes up and vanishes again
  // is worse than one that arrives a moment late.
  const imports = done.data?.imports;
  if (!Array.isArray(imports)) return null;
  if (imports.some((one) => one.status === "applied")) return null;
  const link = <OneTimeImportLink onGo={onGo} />;
  return (
    <div className="card" role="note" aria-label={t`One-time Import`}>
      <p className="small" style={{ margin: 0 }}>
        <Trans>
          <strong>Coming from another budgeting app?</strong> Statements go in one at a time here.
          To bring your whole history across at once — YNAB so far — use the {link} on the{" "}
          {household.name} page. The household&rsquo;s owner runs it, once.
        </Trans>
      </p>
    </div>
  );
}

function OneTimeImportLink({ onGo }: { onGo?: (screen: string) => void }) {
  return (
    <>
        {onGo ? (
          <button
            type="button"
            className="link"
            onClick={() => {
              onGo("household");
              // The section is on the household page, below its settings.
              window.setTimeout(() => {
                document.getElementById("one-time-import-title")?.scrollIntoView({ block: "start" });
              }, 0);
            }}
          >
            {t`One-time Import`}
          </button>
        ) : (
          t`One-time Import`
        )}
    </>
  );
}

export function Import({
  household,
  onGo,
}: {
  household: Household;
  /** Switch the app to another screen: the One-time Import, and How import works. */
  onGo?: (screen: string) => void;
}) {
  const client = useQueryClient();
  const [accountId, setAccountId] = useState("");
  const [pasted, setPasted] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  // "line" is the default because it is the order the statement is in, and a
  // preview is something you check against the paper it came from.
  const categories = useQuery({
    queryKey: ["categories", household.id, false],
    queryFn: () => api.get<CategoryGroup[]>(`/households/${household.id}/categories`),
  });
  // Which lines have a category editor open or a save in flight. Committing
  // while one is open is a race: the blur fires a PATCH and the click fires the
  // commit, and whichever the server sees first decides whether the category
  // survives. Rather than order two requests, the button waits.
  const [busyLines, setBusyLines] = useState<Set<string>>(new Set());
  //: After a category is set by hand, the server says how many other lines in
  //: this file have the same payee and no choice of their own. That is the
  //: offer: you corrected one, there are eleven more.
  //: `uncategorised` when the decision being offered is "no category" (#9).
  //: It spreads within this file like a category does, but there is no rule
  //: to set from it: a payee told never to categorise still takes the bank's
  //: wording for interest and fees, so "every future statement follows" would
  //: be a promise the commit does not keep.
  const [offer, setOffer] = useState<
    { lineId: string; count: number; name: string; payee: string; uncategorised: boolean } | null
  >(null);
  const toasts = useToasts();
  const markBusy = useCallback((id: string, busy: boolean) => {
    setBusyLines((was) => {
      if (was.has(id) === busy) return was;
      const next = new Set(was);
      if (busy) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);
  const [sort, setSort] = useState<PreviewSort>("line");
  const [direction, setDirection] = useState<SortDirection>("asc");
  //: A multi-year statement is thousands of lines, and putting every one in the
  //: DOM froze the tab -- so the preview is windowed the way the register is.
  //: Sorting still covers the whole file: the window is over the sorted lines.
  const sortedLines = useMemo(
    () => (preview ? sortLines(preview.lines, sort, direction) : []),
    [preview, sort, direction],
  );
  const page = useWindowed(sortedLines, STEP, `${preview?.batch_id}|${sort}|${direction}`);
  const [skipped, setSkipped] = useState<Set<string>>(new Set());
  const [rejected, setRejected] = useState<Set<string>>(new Set());
  const [duplicateProblem, setDuplicateProblem] = useState<string | null>(null);
  //: The file the browser still holds from the upload that produced the
  //: preview on screen. Nothing is re-read from the server for it -- the bytes
  //: are hashed and thrown away, and a box underneath a preview is not a
  //: reason to start retaining statements.
  const [rawText, setRawText] = useState<RawText | null>(null);
  const [queueSort, setQueueSort] = useState<QueueSort>("staged_at");
  const [queueDirection, setQueueDirection] = useState<SortDirection>("desc");
  const [purging, setPurging] = useState<StagedImport | null>(null);

  //: Imports staged and never committed. The refusal to stage a file twice
  //: says one "is waiting to be reviewed"; this is how it gets reached.
  const queue = useQuery({
    queryKey: ["staged-imports", household.id],
    queryFn: () => api.get<StagedImport[]>(`/households/${household.id}/imports`),
  });
  const waiting = queue.data ?? [];

  const accounts = useQuery({
    queryKey: ["accounts", household.id],
    queryFn: () => api.get<Account[]>(`/households/${household.id}/accounts`),
  });
  const account = (accounts.data ?? []).find((a) => a.id === accountId);
  //: What the file said about which account it is for (issue #66), shown
  //: beside the picker so a pre-selection is never silent.
  const [recognised, setRecognised] = useState<Recognised | null>(null);
  //: When the file named no account: the tag in its name, offered for the
  //: account a person then picks, so the next statement picks it itself (#130).
  const [tagOffer, setTagOffer] = useState<{ kind: IdentifierKind; value: string } | null>(
    null,
  );

  async function chooseFile(chosen: File | null) {
    setFile(chosen);
    setRecognised(null);
    setTagOffer(null);
    if (!chosen) return;
    const form = new FormData();
    form.set("file", chosen);
    try {
      const found = await api.upload<Recognised>(
        `/households/${household.id}/imports/recognise`,
        form,
      );
      if (found.account_id) {
        setRecognised(found);
        setAccountId(found.account_id);
      } else if (found.tag && found.tag_kind) {
        setTagOffer({ kind: found.tag_kind, value: found.tag });
      }
    } catch {
      // Recognising is a convenience. A file it cannot read is refused
      // properly by "Read the file", with a reason.
    }
  }

  // Never added by itself: the person picked the account, and says yes here.
  const answerTag = useMutation({
    mutationFn: ({ add }: { add: boolean }) =>
      add
        ? api.post(`/households/${household.id}/identifiers/suggestions/add`, {
            kind: tagOffer!.kind,
            value: tagOffer!.value,
            account_id: accountId,
          })
        : api.post(`/households/${household.id}/identifiers/suggestions/ignore`, {
            kind: tagOffer!.kind,
            value: tagOffer!.value,
          }),
    onSuccess: (_answer, { add }) => {
      if (add && tagOffer) {
        toasts.say(
          account
            ? t`${tagOffer.value} now tells a file it is for ${account.name}`
            : t`${tagOffer.value} now tells a file it is for that account`,
        );
      }
      setTagOffer(null);
      client.invalidateQueries({ queryKey: ["identifiers", household.id] });
    },
    onError: (error) => toasts.say((error as Error).message),
  });

  const send = useMutation({
    mutationFn: async (force: boolean) => {
      const form = new FormData();
      form.set("account_id", accountId);
      form.set("force", String(force));
      if (file) form.set("file", file);
      else form.set("pasted", pasted);
      const staged = await api.upload<ImportPreview>(`/households/${household.id}/imports`, form);
      // Read from the file in memory, not from the server: this is the same
      // bytes that were just posted, and nothing keeps them afterwards.
      return { staged, text: await textOfUpload(file, pasted) };
    },
    onSuccess: ({ staged, text }) => {
      setPreview(staged);
      setRawText(text);
      setDuplicateProblem(null);
      setSkipped(new Set());
      setRejected(new Set());
      client.invalidateQueries({ queryKey: ["staged-imports", household.id] });
    },
    onError: (problem) => {
      // Clear first: a duplicate warning left over from a previous file
      // suppresses the banner for whatever went wrong this time, and leaves an
      // "Import it anyway" button pointing at a request that cannot succeed.
      setDuplicateProblem(null);
      if (problem instanceof ApiError && problem.status === 409) {
        setDuplicateProblem(problem.message);
        // The import it is talking about may have been staged in another
        // session since this screen was opened, and the banner's way into it
        // is found in the queue.
        client.invalidateQueries({ queryKey: ["staged-imports", household.id] });
      }
    },
  });

  // Open one off the queue. `GET /imports/{id}` already returned a whole
  // preview; all this screen was missing was a way to name the id.
  const open = useMutation({
    mutationFn: (entry: StagedImport) =>
      api.get<ImportPreview>(`/households/${household.id}/imports/${entry.batch_id}`),
    onSuccess: (staged, entry) => {
      setPreview(staged);
      // The account the file was staged against, so the preview's amounts are
      // formatted in its currency rather than the household's.
      setAccountId(entry.account_id);
      // There is no file in memory for an import staged in another session, so
      // the text box is honestly absent rather than showing the last upload's.
      setRawText(null);
      setDuplicateProblem(null);
      setSkipped(new Set());
      setRejected(new Set());
    },
    onError: (error) => toasts.say((error as Error).message),
  });

  // And throw one away. Without this a staged import nobody wants is a
  // permanent refusal to import that file, because the duplicate check counts
  // a staged import as already-seen.
  const purge = useMutation({
    mutationFn: (entry: StagedImport) =>
      api.del<null>(`/households/${household.id}/imports/${entry.batch_id}`),
    onSuccess: (_answer, entry) => {
      setPurging(null);
      if (preview?.batch_id === entry.batch_id) {
        setPreview(null);
        setRawText(null);
      }
      setDuplicateProblem(null);
      toasts.say(
        entry.filename
          ? t`${entry.filename} was discarded — nothing was written`
          : t`that import was discarded — nothing was written`,
      );
      client.invalidateQueries({ queryKey: ["staged-imports", household.id] });
    },
    onError: (error) => {
      setPurging(null);
      toasts.say((error as Error).message);
    },
  });

  // Which staged import the "already staged" refusal is talking about. The
  // refusal is a sentence, so it is matched back to the queue by the two
  // things the request knows: the account it was for and the file's name.
  const alreadyStaged = useMemo(() => {
    if (!duplicateProblem) return null;
    const name = file ? file.name : "pasted";
    const mine = waiting.filter((one) => one.account_id === accountId);
    return mine.find((one) => one.filename === name) ?? (mine.length === 1 ? mine[0] : null);
  }, [duplicateProblem, waiting, accountId, file]);

  // Scoped to this file. Teaching the payee -- so every future statement
  // follows -- is a larger decision with its own screen, and doing it silently
  // from here would be changing something the person did not ask about.
  const spread = useMutation({
    mutationFn: (lineId: string) =>
      api.post<ImportPreview>(
        `/households/${household.id}/imports/${preview!.batch_id}/lines/${lineId}/apply-to-payee`,
        {},
      ),
    // The dialog is closed by whoever pressed the button, not here: the
    // combined answer runs this and then the rule, and closing in between
    // would take the second half's confirmation with it.
    onSuccess: (updated) => setPreview(updated),
    onError: (error) => toasts.say((error as Error).message),
  });

  // The larger decision: teach the payee, so every future statement follows.
  // It creates the payee when the statement has brought a new one, which is a
  // row in the ledger that was not there before -- so the confirmation says so.
  const rule = useMutation({
    mutationFn: (lineId: string) =>
      api.post<{ payee_name: string; category_name: string; payee_created: boolean }>(
        `/households/${household.id}/imports/${preview!.batch_id}/lines/${lineId}/payee-rule`,
        {},
      ),
    onSuccess: (done) => {
      toasts.say(
        done.payee_created
          ? t`${done.payee_name} will always be ${done.category_name} — and the payee was added`
          : t`${done.payee_name} will always be ${done.category_name}`,
      );
    },
    onError: (error) => toasts.say((error as Error).message),
  });

  // Both halves of "stop asking me", in the order that leaves the file right
  // even if the second call fails: spread first, then teach the payee. The
  // reverse order would set a rule for a statement that never got categorised.
  const busy = spread.isPending || rule.isPending;

  async function onlyThisFile(lineId: string) {
    await spread.mutateAsync(lineId);
    setOffer(null);
  }

  async function alsoTheRule(lineId: string) {
    // Spread first: if teaching the payee fails, the file is still right, and
    // the toast says what went wrong. The other order would leave a rule set
    // for a statement nobody categorised.
    await spread.mutateAsync(lineId);
    await rule.mutateAsync(lineId).catch(() => undefined);
    setOffer(null);
  }

  const commit = useMutation({
    mutationFn: () =>
      api.post<{ created: number; absorbed: number; skipped: number }>(
        `/households/${household.id}/imports/${preview!.batch_id}/commit`,
        { skip_line_ids: [...skipped], reject_match_line_ids: [...rejected] },
      ),
    onSuccess: () => {
      setPreview(null);
      setFile(null);
      setPasted("");
      setRawText(null);
      client.invalidateQueries({ queryKey: ["register", household.id] });
      client.invalidateQueries({ queryKey: ["accounts", household.id] });
      client.invalidateQueries({ queryKey: ["batches", household.id] });
      client.invalidateQueries({ queryKey: ["staged-imports", household.id] });
    },
  });

  function toggle(set: Set<string>, id: string, apply: (next: Set<string>) => void) {
    const next = new Set(set);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    apply(next);
  }

  return (
    <>
      <h1>
        <Trans>Import a statement</Trans>
      </h1>
      {onGo ? (
        <p className="small muted" style={{ marginTop: 0 }}>
          <Trans>
            Every path a statement can take, and every way a line can go, is explained in{" "}
            <button type="button" className="link" onClick={() => onGo("import-guide")}>
              How import works
            </button>
            .
          </Trans>
        </p>
      ) : null}
      {!preview ? <OneTimeImportNote household={household} onGo={onGo} /> : null}

      {!preview && waiting.length > 0 && (
        <div className="card import-queue">
          <h2>
            <Trans>Waiting to be reviewed</Trans>
          </h2>
          <p className="muted small">
            {plural(waiting.length, {
              one: "An import was staged and never finished. Nothing in it has reached the register — open one to carry on where it was left, or discard it.",
              other: `${waiting.length} imports were staged and never finished. Nothing in them has reached the register — open one to carry on where it was left, or discard it.`,
            })}
          </p>

          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  {(
                    [
                      { label: t`File`, column: "filename" },
                      { label: t`Into`, column: "account" },
                      { label: t`Staged by`, column: "actor" },
                      { label: t`Staged`, column: "staged_at" },
                    ] as { label: string; column: QueueSort }[]
                  ).map((one) => (
                    <SortHeading
                      key={one.column}
                      label={one.label}
                      column={one.column}
                      sort={queueSort}
                      direction={queueDirection}
                      onSort={(column, next) => {
                        setQueueSort(column);
                        setQueueDirection(next);
                      }}
                    />
                  ))}
                  <SortHeading
                    label={t`Rows`}
                    column="rows"
                    sort={queueSort}
                    direction={queueDirection}
                    onSort={(column, next) => {
                      setQueueSort(column);
                      setQueueDirection(next);
                    }}
                    align="right"
                  />
                  {/* Buttons, not a fact about the row: nothing to sort on. */}
                  <th />
                </tr>
              </thead>
              <tbody>
                {sortQueue(waiting, queueSort, queueDirection).map((one) => (
                  <tr key={one.batch_id}>
                    <td className="small mono">{one.filename ?? "—"}</td>
                    <td className="small">{one.account_name ?? "—"}</td>
                    <td className="small">{one.actor_name ?? "—"}</td>
                    <td className="small muted">{whenStaged(one.staged_at)}</td>
                    <td className="amount small">{one.row_count}</td>
                    <td className="queue-actions">
                      <button
                        className="primary"
                        disabled={open.isPending || purge.isPending}
                        onClick={() => open.mutate(one)}
                      >
                        {open.isPending && open.variables?.batch_id === one.batch_id
                          ? t`Opening…`
                          : t`Open`}
                      </button>
                      <button
                        className="link"
                        disabled={open.isPending || purge.isPending}
                        onClick={() => setPurging(one)}
                      >
                        <Trans>Discard</Trans>
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {purging && (
        <Dialog title={t`Discard this staged import?`} onClose={() => setPurging(null)}>
          <p style={{ marginTop: 0 }}>
            <Plural
              value={purging.row_count}
              one={
                <>
                  <strong>{purging.filename ?? t`This import`}</strong> and its {purging.row_count} line
                  will be deleted. Nothing in it has reached the register, so there is nothing to undo
                  afterwards — and the file can be imported again from scratch.
                </>
              }
              other={
                <>
                  <strong>{purging.filename ?? t`This import`}</strong> and its {purging.row_count} lines
                  will be deleted. Nothing in it has reached the register, so there is nothing to undo
                  afterwards — and the file can be imported again from scratch.
                </>
              }
            />
          </p>
          <div className="dialog-choices">
            <button
              className="primary"
              disabled={purge.isPending}
              onClick={() => purge.mutate(purging)}
            >
              {purge.isPending ? t`Discarding…` : t`Yes, discard it`}
            </button>
            <button disabled={purge.isPending} onClick={() => setPurging(null)}>
              <Trans>Keep it</Trans>
            </button>
          </div>
        </Dialog>
      )}

      {!preview && (
        <div className="card">
          <p className="muted small">
            <Trans>
              A CSV, an OFX/QFX download, a bank's .xls export, a statement PDF, or a paste from
              your bank's website. CSV and spreadsheets are worked out from the file itself — the
              delimiter, the date format, the decimal separator, which column is which, and which
              row the table actually starts on. OFX needs none of that: it names its own fields,
              and its transaction ids are used so re-importing an overlapping statement cannot
              double anything up. A PDF is read by where the words sit on the page, so a
              running-balance column is not mistaken for the amount — and a designed bill, which
              is summary boxes rather than a table, is refused rather than half-read. You will see
              all of it before anything is written.
            </Trans>
          </p>

          <Problem error={send.error && !duplicateProblem ? send.error : null} />

          {duplicateProblem && (
            <div className="banner warn">
              {duplicateProblem}
              <div className="banner-choices">
                {/* The refusal says "open that import rather than starting a
                    second one", so here is the way to do exactly that. */}
                {alreadyStaged && (
                  <button
                    className="primary"
                    disabled={open.isPending}
                    onClick={() => open.mutate(alreadyStaged)}
                  >
                    {open.isPending ? t`Opening…` : t`Open that import`}
                  </button>
                )}
                <button onClick={() => send.mutate(true)}>
                  <Trans>Import it anyway</Trans>
                </button>
                {alreadyStaged && (
                  <button className="link" onClick={() => setPurging(alreadyStaged)}>
                    <Trans>Discard the staged one</Trans>
                  </button>
                )}
              </div>
            </div>
          )}

          <div className="row">
            <Field label={t`Into which account`}>
              <select value={accountId} onChange={(e) => setAccountId(e.target.value)}>
                <option value="">{t`Choose…`}</option>
                {(accounts.data ?? []).map((one) => (
                  <option key={one.id} value={one.id}>
                    {one.name} ({one.currency})
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t`File`}>
              <input
                type="file"
                accept=".csv,.txt,.ofx,.qfx,.xls,.pdf,text/csv,application/x-ofx,application/pdf"
                onChange={(e) => void chooseFile(e.target.files?.[0] ?? null)}
              />
            </Field>
          </div>
          {recognised && (
            <p className="small muted" style={{ margin: "6px 0 0" }}>
              {recognised.account_id === accountId ? (
                <Trans>
                  Chose <strong>{recognised.account_name}</strong> for you: {recognised.how}.
                </Trans>
              ) : (
                <span style={{ color: "var(--warn)" }}>
                  <Trans>
                    This file looks like it is for <strong>{recognised.account_name}</strong> (
                    {recognised.how}), not the account chosen above.
                  </Trans>
                </span>
              )}
            </p>
          )}

          {tagOffer && account && (
            <p className="small" style={{ margin: "6px 0 0" }} data-testid="tag-offer">
              {tagOffer.kind === "iban" ? (
                <Trans>
                  This file's name carries <span className="mono">{tagOffer.value}</span>. Keep it as{" "}
                  <strong>{account.name}</strong>'s IBAN, so its next statement picks the account
                  itself?
                </Trans>
              ) : (
                <Trans>
                  This file's name carries <span className="mono">{tagOffer.value}</span>. Keep it as{" "}
                  <strong>{account.name}</strong>'s file tag, so its next statement picks the account
                  itself?
                </Trans>
              )}{" "}
              <button
                className="link"
                disabled={answerTag.isPending}
                onClick={() => answerTag.mutate({ add: true })}
              >
                <Trans>Add</Trans>
              </button>{" "}
              <button
                className="link"
                disabled={answerTag.isPending}
                onClick={() => answerTag.mutate({ add: false })}
              >
                <Trans>Ignore</Trans>
              </button>
            </p>
          )}

          <p className="small muted" style={{ margin: "14px 0 6px" }}>
            <Trans>Or paste the rows, if your bank only lets you copy them from a page:</Trans>
          </p>
          <textarea
            rows={5}
            value={pasted}
            onChange={(e) => setPasted(e.target.value)}
            // An example of what a bank's page copies, which a translation may
            // swap for a bank its readers know.
            placeholder={t`Fecha;Concepto;Importe\n05/01/2026;MERCADONA;-45,20`}
          />
          <p />
          <button
            className="primary"
            disabled={!accountId || (!file && !pasted.trim()) || send.isPending}
            onClick={() => send.mutate(false)}
          >
            {send.isPending ? t`Reading…` : t`Read the file`}
          </button>
        </div>
      )}

      <Toasts items={toasts.items} onDone={toasts.dismiss} />

      {offer && (
        <Dialog title={t`The rest of this payee?`} onClose={() => setOffer(null)}>
          <p style={{ marginTop: 0 }}>
            <Plural
              value={offer.count}
              one={
                <>
                  <strong>{offer.count} other line has the same payee</strong> and no category of
                  their own.
                </>
              }
              other={
                <>
                  <strong>{offer.count} other lines have the same payee</strong> and no category of
                  their own.
                </>
              }
            />{" "}
            {offer.uncategorised ? (
              <Trans>
                Leave them <strong>uncategorised</strong> too?
              </Trans>
            ) : (
              <Trans>
                Put them in <strong>{offer.name}</strong> too?
              </Trans>
            )}
          </p>
          {/* Three answers, and the middle one is the one people actually want
              the second time they see this dialog. It was a link inside the
              explanatory sentence underneath, which read as a footnote rather
              than as a choice -- so it is a button beside the others. */}
          <div className="dialog-choices">
            <button
              className="primary"
              disabled={busy}
              onClick={() => void onlyThisFile(offer.lineId)}
            >
              {spread.isPending && !rule.isPending
                ? t`Applying…`
                : t`Yes, all ${offer.count + 1} of them`}
            </button>
            {offer.uncategorised ? null : (
              <button
                className="primary"
                disabled={busy}
                onClick={() => void alsoTheRule(offer.lineId)}
              >
                {rule.isPending ? t`Setting the rule…` : t`Yes, all of them and set the rule`}
              </button>
            )}
            <button disabled={busy} onClick={() => setOffer(null)}>
              <Trans>No, just this one</Trans>
            </button>
          </div>

          {offer.uncategorised ? (
            <p className="small muted" style={{ margin: "12px 0 0" }}>
              <Trans>This file only. The payee&rsquo;s rule is left as it is for future statements.</Trans>
            </p>
          ) : (
            <p className="small muted" style={{ margin: "12px 0 0" }}>
              <Trans>
                The first is this file only. The second also makes <strong>{offer.name}</strong>{" "}
                the rule for <strong>{offer.payee}</strong>, so every future statement follows
                without asking.
              </Trans>
            </p>
          )}
        </Dialog>
      )}

      {preview && (
        <>
          <div className="card">
            <h2>
              <Trans>What this would do</Trans>
            </h2>
            <p className="muted small">
              {preview.filename} · {plural(preview.lines.length, { other: `${preview.lines.length} lines` })} · {t`into ${account?.name ?? ""}`}
            </p>

            {preview.warnings.map((warning) => (
              <div className="banner warn" key={warning}>
                {warning}
              </div>
            ))}

            <dl className="small muted" style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
              {Object.entries(preview.detected).map(([key, value]) => (
                <div key={key}>
                  <dt style={{ fontWeight: 600 }}>{detectedLabel(key)}</dt>
                  <dd style={{ margin: 0 }} className="mono">
                    {value === null ? "—" : String(value)}
                  </dd>
                </div>
              ))}
            </dl>

            <div className="row" style={{ marginTop: 12 }}>
              {Object.entries(preview.counts)
                .filter(([, count]) => count > 0)
                .map(([outcome, count]) => (
                  <span key={outcome} className={`pill ${outcome}`}>
                    {count} {OUTCOME_WORDS[outcome] ?? outcome}
                  </span>
                ))}
            </div>
          </div>

          <div className="card">
            <Problem error={commit.error ?? spread.error} />

            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th style={{ width: 28 }}>
                      <Trans>Add</Trans>
                    </th>
                    {(
                      [
                        { label: t`Line`, column: "line" },
                        { label: t`Date`, column: "date" },
                        { label: t`Payee`, column: "payee" },
                        { label: t`Memo`, column: "memo" },
                      ] as { label: string; column: PreviewSort }[]
                    ).map((one) => (
                      <SortHeading
                        key={one.column}
                        label={one.label}
                        column={one.column}
                        sort={sort}
                        direction={direction}
                        onSort={(column, next) => {
                          setSort(column);
                          setDirection(next);
                        }}
                      />
                    ))}
                    <SortHeading
                      label={t`Amount`}
                      column="amount"
                      sort={sort}
                      direction={direction}
                      onSort={(column, next) => {
                        setSort(column);
                        setDirection(next);
                      }}
                      align="right"
                    />
                    <SortHeading
                      label={t`Category`}
                      column="category"
                      sort={sort}
                      direction={direction}
                      onSort={(column, next) => {
                        setSort(column);
                        setDirection(next);
                      }}
                    />
                    <SortHeading
                      label={t`What happens`}
                      column="outcome"
                      sort={sort}
                      direction={direction}
                      onSort={(column, next) => {
                        setSort(column);
                        setDirection(next);
                      }}
                    />
                  </tr>
                </thead>
                <tbody>
                  {page.visible.map((line) => (
                    <PreviewRow
                      key={line.id}
                      line={line}
                      household={household}
                      batchId={preview.batch_id}
                      groups={categories.data ?? []}
                      onBusy={markBusy}
                      onUpdated={(updated) =>
                        setPreview({
                          ...preview,
                          lines: preview.lines.map((one) =>
                            one.id === updated.id ? updated : one,
                          ),
                        })
                      }
                      onCategorised={(updated) => {
                        setPreview({
                          ...preview,
                          lines: preview.lines.map((one) =>
                            one.id === updated.id ? updated : one,
                          ),
                        });
                        setOffer(
                          updated.similar_lines > 0 &&
                            (updated.category_name || updated.category_uncategorised)
                            ? {
                                lineId: updated.id,
                                count: updated.similar_lines,
                                name: updated.category_name ?? t`uncategorised`,
                                payee:
                                  ((updated.parsed ?? {}) as Record<string, string>).payee ??
                                  t`this payee`,
                                uncategorised: updated.category_uncategorised,
                              }
                            : null,
                        );
                      }}
                      currency={account?.currency ?? household.base_currency}
                      skipped={skipped.has(line.id)}
                      rejectedMatch={rejected.has(line.id)}
                      onToggleSkip={() => toggle(skipped, line.id, setSkipped)}
                      onToggleMatch={() => toggle(rejected, line.id, setRejected)}
                    />
                  ))}
                </tbody>
              </table>
              {/* No request behind it: every line is already here. */}
              {!page.allShown && (
                <button
                  type="button"
                  className="more-rows"
                  ref={page.sentinelRef}
                  onClick={page.extend}
                >
                  {t`Showing ${formatCount(page.shown)} of ${formatCount(page.total)} — show more`}
                </button>
              )}
            </div>

            <div className="row" style={{ marginTop: 16 }}>
              <button
                className="primary"
                disabled={commit.isPending || busyLines.size > 0}
                title={busyLines.size > 0 ? t`Finish the category you are editing first` : ""}
                onClick={() => commit.mutate()}
              >
                {commit.isPending ? t`Importing…` : t`Import these`}
              </button>
              <button onClick={() => setPreview(null)}>
                <Trans>Cancel</Trans>
              </button>
            </div>
            <p className="small muted" style={{ marginTop: 10 }}>
              <Trans>This lands as one entry in History, so the whole import can be undone in one go.</Trans>
            </p>
          </div>

          {rawText && (
            <RawFile
              raw={rawText}
              kind={String(preview.detected.kind ?? "")}
              delimiter={String(preview.detected.delimiter ?? ",")}
            />
          )}
        </>
      )}
    </>
  );
}

/**
 * The uploaded file, as text, underneath what was made of it.
 *
 * For checking a row against the file it came from without leaving the screen:
 * the table says what the app thinks each line means, and this says what the
 * line actually was.
 *
 * It is the file the browser still holds from the upload it just did. Nothing
 * is fetched, because nothing is kept -- the bytes are hashed and discarded,
 * and `import_lines.raw` is what makes an import explainable a year later. A
 * PDF is not shown at all: its text is in a compressed stream and rendering
 * the bytes would be noise.
 *
 * The gutter carries the preview's Line numbers (see `numberRawLines`). They
 * are drawn by CSS from `data-line`, not written into the text, so copying
 * lines out of the view copies the file and nothing else. OFX is left
 * unnumbered: its Line is the transaction's place in the statement, not a line
 * of the file, and a gutter counting file lines beside it would invite exactly
 * the mismatch the numbers are there to rule out.
 */
export function RawFile({
  raw,
  kind,
  delimiter,
}: {
  raw: RawText;
  kind: string;
  delimiter: string;
}) {
  const binary = looksBinary(raw.text);
  const truncated = raw.total > RAW_TEXT_LIMIT;
  const numbered = kind !== "ofx";
  const lines = useMemo(
    () => (binary ? [] : numberRawLines(raw.text, delimiter || ",")),
    [binary, raw.text, delimiter],
  );
  const widest = lines.reduce((most, line) => Math.max(most, line.no ?? 0), 0);

  return (
    <div className="card">
      <h2>
        <Trans>The file itself</Trans>
      </h2>
      <p className="muted small">
        {raw.name} · {t`${formatCount(raw.total)} characters`}
        {truncated ? ` · ${t`showing the first ${formatCount(RAW_TEXT_LIMIT)}`}` : ""}
        {` · ${t`from the copy in this browser, not from the server — the file itself is not kept`}`}
      </p>
      {binary ? (
        <p className="small muted">
          <Trans>
            This file is not text — a spreadsheet export keeps its rows in a binary format. What it
            was read as is in the table above.
          </Trans>
        </p>
      ) : (
        <>
          {numbered ? (
            <pre
              className="raw-file numbered mono small"
              style={{ ["--gutter" as string]: `${String(widest).length}ch` }}
            >
              {lines.map((line, index) => (
                <span key={index} className="raw-line" data-line={line.no ?? undefined}>
                  {line.text}
                  {index < lines.length - 1 ? "\n" : ""}
                </span>
              ))}
            </pre>
          ) : (
            <>
              <pre className="raw-file mono small">{raw.text.slice(0, RAW_TEXT_LIMIT)}</pre>
              <p className="small muted">
                <Trans>
                  Not numbered: in an OFX file the Line column above counts transactions, not lines
                  of the file.
                </Trans>
              </p>
            </>
          )}
          {truncated && (
            <p className="small muted">
              {t`Cut off at ${formatCount(RAW_TEXT_LIMIT)} characters. Every line of it was still read — the table above is the whole file.`}
            </p>
          )}
        </>
      )}
    </div>
  );
}

function PreviewRow({
  line,
  household,
  batchId,
  groups,
  onCategorised,
  onUpdated,
  onBusy,
  currency,
  skipped,
  rejectedMatch,
  onToggleSkip,
  onToggleMatch,
}: {
  line: ImportLine;
  household: Household;
  batchId: string;
  groups: CategoryGroup[];
  onCategorised: (line: ImportLine) => void;
  /** A change with nothing to offer about the other rows, such as a memo. */
  onUpdated: (line: ImportLine) => void;
  onBusy: (lineId: string, busy: boolean) => void;
  currency: string;
  skipped: boolean;
  rejectedMatch: boolean;
  onToggleSkip: () => void;
  onToggleMatch: () => void;
}) {
  const parsed = (line.parsed ?? {}) as Record<string, string | number | null>;
  const willWrite = line.outcome === "created" || line.outcome === "matched_existing";
  const amount = typeof parsed.amount === "number" ? parsed.amount : null;

  return (
    <tr style={skipped ? { opacity: 0.45 } : undefined}>
      <td>
        <input
          type="checkbox"
          aria-label={t`Include line ${line.line_no}`}
          checked={willWrite && !skipped}
          disabled={!willWrite}
          onChange={onToggleSkip}
          style={{ width: "auto" }}
        />
      </td>
      <td className="small muted mono">{line.line_no}</td>
      <td className="small">{parsed.date ?? "—"}</td>
      <td className="small">
        {parsed.payee_resolved ? (
          <>
            {parsed.payee_resolved}
            <div className="muted mono" style={{ fontSize: 11 }}>
              {parsed.payee}
            </div>
          </>
        ) : (
          (parsed.payee ?? <span className="muted">—</span>)
        )}
      </td>
      {/* What the bank wrote in its own words, or what somebody typed instead.
          Worth seeing before committing: it is often the only thing that
          distinguishes two identical-looking rows on the same day. */}
      <MemoCell
        line={line}
        household={household}
        batchId={batchId}
        onSaved={onUpdated}
        onBusy={onBusy}
        editable={willWrite}
      />
      <td className="amount">{amount === null ? "" : format(amount, currency)}</td>
      <CategoryCell
        line={line}
        household={household}
        batchId={batchId}
        groups={groups}
        onSaved={onCategorised}
        onBusy={onBusy}
        editable={willWrite}
      />
      <td className="small">
        <span className={`pill ${line.outcome}`}>{OUTCOME_WORDS[line.outcome]}</span>
        {line.reason ? <div className="muted">{line.reason}</div> : null}
        {line.outcome === "matched_existing" ? (
          <button className="link" onClick={onToggleMatch}>
            {rejectedMatch ? t`Undo — add as new instead` : t`Not the same thing — add it separately`}
          </button>
        ) : null}
      </td>
    </tr>
  );
}


/**
 * The memo, typed before the row exists.
 *
 * The same shape as the category cell beside it, and stored the same way: one
 * small PATCH against the staged line rather than held in the browser and sent
 * at commit, so it survives a reload and is still there when the import is
 * reopened from the queue tomorrow.
 *
 * What the bank sent is never written over. A typed memo is shown in place of
 * it with the original underneath, the way a resolved payee shows the raw
 * descriptor underneath — and "hand it back" is a button rather than a trick
 * with an empty box, because emptying the box means something else: this row
 * gets no memo at all.
 */
function MemoCell({
  line,
  household,
  batchId,
  onSaved,
  onBusy,
  editable,
}: {
  line: ImportLine;
  household: Household;
  batchId: string;
  onSaved: (line: ImportLine) => void;
  onBusy: (lineId: string, busy: boolean) => void;
  editable: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [typed, setTyped] = useState("");
  const shown = memoOf(line);
  const bank = ((line.parsed ?? {}) as Record<string, unknown>).memo;

  const save = useMutation({
    mutationFn: (body: { memo?: string | null; clear_memo?: boolean }) =>
      api.patch<ImportLine>(
        `/households/${household.id}/imports/${batchId}/lines/${line.id}/memo`,
        body,
      ),
    onSuccess: (updated) => {
      setEditing(false);
      onSaved(updated);
    },
    onSettled: () => onBusy(line.id, false),
  });

  // Held open from the moment the editor appears until the save comes back, so
  // the import button knows there is an unfinished decision on this line.
  useEffect(() => {
    onBusy(line.id, editing);
    return () => onBusy(line.id, false);
  }, [editing, line.id, onBusy]);

  function commit() {
    if (typed === shown.text && shown.chosen) {
      setEditing(false);
      return;
    }
    save.mutate({ memo: typed });
  }

  if (!editing) {
    return (
      <td className="small editable">
        {editable ? (
          <button
            type="button"
            className="cell-edit"
            title={t`The memo this row will carry`}
            onClick={() => {
              setTyped(shown.text);
              setEditing(true);
            }}
          >
            {shown.text ? (
              <span className={shown.chosen ? undefined : "muted"}>{shown.text}</span>
            ) : (
              <span className="muted">{shown.chosen ? t`no memo` : "—"}</span>
            )}
            {shown.chosen && bank ? (
              <span className="memo-was mono">{String(bank)}</span>
            ) : null}
          </button>
        ) : (
          /* A rejected or already-imported line writes nothing, so there is
             no memo to set. */
          <span className="cell-static muted">{shown.text || "—"}</span>
        )}
      </td>
    );
  }

  return (
    <td className="editing">
      <input
        type="text"
        value={typed}
        autoFocus
        maxLength={500}
        aria-label={t`Memo`}
        placeholder={bank ? String(bank) : t`a note for this row`}
        onChange={(e) => setTyped(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
          if (e.key === "Escape") setEditing(false);
        }}
        onBlur={commit}
      />
      {shown.chosen && bank ? (
        <button
          type="button"
          className="link"
          // Before the blur, or the blur saves what is in the box first.
          onMouseDown={(e) => {
            e.preventDefault();
            save.mutate({ clear_memo: true });
          }}
        >
          {t`use the bank's: ${String(bank)}`}
        </button>
      ) : null}
      {save.error ? <div className="small neg">{(save.error as Error).message}</div> : null}
    </td>
  );
}

/**
 * Where a line will land, and changing it before it does.
 *
 * Shown as a guess until somebody says otherwise: the payee's rule picks it,
 * and a statement of three hundred rows is three hundred categories nobody had
 * to type. The guess is marked as one, because "likely" and "decided" are
 * different promises and only the first is worth checking.
 *
 * Typed rather than picked, like the register's: thirty categories under five
 * headings means scrolling to find one you already know the name of, and
 * "mortgage" finds "Bills: Rent / Mortgage" from any part of either half.
 *
 * Three answers, the way the memo has them: a category, an empty box for "back
 * to the suggestion", and *Uncategorised* for "no category, whatever the rule
 * says" (#9). Before the third existed, emptying the box on a line the payee's
 * rule or the bank's wording had categorised simply brought the guess back.
 *
 * The choice is saved to the staged line straight away rather than held here
 * and sent at commit. It costs one small request and it means the decision
 * survives a reload, which the skip and absorb ticks -- held in the browser --
 * do not.
 */
function CategoryCell({
  line,
  household,
  batchId,
  groups,
  onSaved,
  onBusy,
  editable,
}: {
  line: ImportLine;
  household: Household;
  batchId: string;
  groups: CategoryGroup[];
  onSaved: (line: ImportLine) => void;
  onBusy: (lineId: string, busy: boolean) => void;
  editable: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [typed, setTyped] = useState("");
  //: What the box held when it opened. Committing that, untouched, is no
  //: decision at all and sends nothing -- the line keeps whatever it had,
  //: whichever category its words also happen to spell (#19).
  const [opened, setOpened] = useState("");
  const [unmatched, setUnmatched] = useState(false);

  const all = useMemo(() => groups.flatMap((group) => group.categories), [groups]);
  // "Uncategorised" first, as an option like any other, so it is reached the
  // same way a category is -- by typing or with the arrows -- rather than by a
  // link the keyboard cannot get to before the field's blur has saved.
  const labels = useMemo(() => [uncategorisedWord(), ...all.map((one) => one.full_name)], [all]);
  const shown = categoryOf(line);

  const save = useMutation({
    mutationFn: (choice: CategoryChoice) =>
      api.patch<ImportLine>(
        `/households/${household.id}/imports/${batchId}/lines/${line.id}`,
        choice === "uncategorised"
          ? { uncategorised: true }
          : { category_id: choice, clear_category: choice === null },
      ),
    onSuccess: (updated) => {
      setEditing(false);
      setUnmatched(false);
      onSaved(updated);
    },
    onSettled: () => onBusy(line.id, false),
  });

  // Held open from the moment the editor appears until the save comes back, so
  // the import button knows there is an unfinished decision on this line.
  useEffect(() => {
    onBusy(line.id, editing);
    return () => onBusy(line.id, false);
  }, [editing, line.id, onBusy]);

  /** The answer an option picked from the list stands for: its position in `labels`. */
  function picked(at: number): CategoryChoice | undefined {
    if (at === 0) return "uncategorised";
    return all[at - 1]?.id;
  }

  function resolve(text: string): { choice: CategoryChoice } | "ambiguous" {
    const needle = fold(text);
    // An empty box hands the line back to the suggestion, as it always has.
    if (!needle) return { choice: null };
    // Typed text that is exactly the sentinel's label means the sentinel, and
    // is checked before the categories (#19). The other order let a household
    // category called "Other: Uncategorised" (short name "Uncategorised")
    // swallow it, so the option could not be chosen at all. That category is
    // still reached by its full name, or by picking it from the list, which
    // commits the option itself rather than its words.
    const none = fold(uncategorisedWord());
    if (needle === none) return { choice: "uncategorised" };
    for (const one of all) {
      if (fold(one.full_name) === needle || fold(one.name) === needle) return { choice: one.id };
    }
    const hits: CategoryChoice[] = all
      .filter((one) => fold(one.full_name).includes(needle) || fold(one.name).includes(needle))
      .map((one) => one.id);
    if (none.includes(needle)) hits.push("uncategorised");
    return hits.length === 1 ? { choice: hits[0] } : "ambiguous";
  }

  function commit(next: string = typed, taken?: number) {
    const fromList = taken === undefined ? undefined : picked(taken);
    // Nothing picked and nothing changed: no decision, so nothing to send.
    // Without this, a line marked uncategorised reopened in a household with a
    // category whose name is "Uncategorised" resolved to that category on blur.
    if (fromList === undefined && fold(next) === fold(opened)) {
      setEditing(false);
      setUnmatched(false);
      return;
    }
    const answer = fromList === undefined ? resolve(next) : { choice: fromList };
    if (answer === "ambiguous") {
      setUnmatched(true);
      return;
    }
    const unchanged =
      answer.choice === "uncategorised"
        ? line.category_uncategorised
        : !line.category_uncategorised &&
          answer.choice === (line.category_id ?? null) &&
          !line.category_chosen;
    if (unchanged) {
      setEditing(false);
      return;
    }
    save.mutate(answer.choice);
  }

  if (!editing) {
    return (
      <td className="small editable">
        {editable ? (
          <button
            type="button"
            className="cell-edit"
            title={t`Where this line will land`}
            onClick={() => {
              const start = line.category_uncategorised
                ? uncategorisedWord()
                : (line.category_name ?? "");
              setTyped(start);
              setOpened(start);
              setUnmatched(false);
              setEditing(true);
            }}
          >
            <span className={shown.muted ? "muted" : undefined}>{shown.text}</span>
          </button>
        ) : (
          /* A rejected or already-imported line writes nothing, so there is
             nothing to categorise. */
          <span className="cell-static muted">—</span>
        )}
      </td>
    );
  }

  return (
    <td className="editing">
      <Combobox
        value={typed}
        onChange={(next) => {
          setTyped(next);
          setUnmatched(false);
        }}
        options={labels}
        browse
        limit={Infinity}
        placeholder={t`type any part`}
        aria-label={t`Category`}
        autoFocus
        onCommit={commit}
        onCancel={() => {
          setEditing(false);
          setUnmatched(false);
        }}
      />
      {unmatched ? (
        <div className="small neg">
          <Trans>No single category matches that.</Trans>
        </div>
      ) : line.category_chosen ? (
        // Said once somebody has chosen, because only then is there a
        // suggestion to go back to -- and it is not "Uncategorised", which is
        // the other thing an empty-looking cell can mean.
        <div className="small muted">
          <Trans>Empty it to go back to the suggestion.</Trans>
        </div>
      ) : null}
      {save.error ? <div className="small neg">{(save.error as Error).message}</div> : null}
    </td>
  );
}

/**
 * The option that means "no category, and do not ask the rule" (#9).
 *
 * Emptying the box cannot mean that: it already means "back to the
 * suggestion", which for a payee with a usual category or a line the bank
 * labelled as interest or a fee is a category again.
 */
/** The choice that means "no category", as the category box lists and accepts it. */
export function uncategorisedWord(): string {
  return t`Uncategorised`;
}

/** What the category cell sends: a category id, null for "back to the rule", or none at all. */
type CategoryChoice = string | null | "uncategorised";

/**
 * The category cell's four states, in words.
 *
 * A category somebody chose, the suggestion (muted, because it is a guess),
 * uncategorised because somebody said so, and uncategorised because nothing
 * suggested anything. The last two look alike in the register afterwards, and
 * are different promises here: one is a decision, the other a gap.
 */
export function categoryOf(line: ImportLine): { text: string; muted: boolean } {
  if (line.category_uncategorised) return { text: t`uncategorised (chosen)`, muted: false };
  if (line.category_name) return { text: line.category_name, muted: !line.category_chosen };
  return { text: t`uncategorised`, muted: true };
}

/** Same folding the Combobox ranks with, so what matches is what was offered. */
function fold(text: string): string {
  return text
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .trim()
    .toLowerCase();
}
