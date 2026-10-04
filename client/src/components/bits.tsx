/** Small shared pieces. Nothing here knows about the domain. */

import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ApiError } from "../lib/api";
import { fixed, moneyKey, sortRows } from "../lib/sorting";
import type { FixedGroup, SortDirection, SortKeyPart, SortValue } from "../lib/sorting";
import { format } from "../lib/money";

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
}) {
  if (!hint)
    return (
      <label className="field">
        <span>{label}</span>
        {children}
      </label>
    );

  // The hint is a button, and a button inside a <label> would also trip the
  // label's own click -- so the two sit side by side and the control keeps its
  // name from a wrapping label whose text is there for screen readers only.
  return (
    <div className="field">
      <div className="field-head">
        <span>{label}</span>
        {hint}
      </div>
      <label>
        <span className="sr-only">{label}</span>
        {children}
      </label>
    </div>
  );
}

export function Problem({ error }: { error: unknown }) {
  if (!error) return null;
  const message = error instanceof Error ? error.message : String(error);
  const wait = error instanceof ApiError ? error.retryAfter : undefined;
  return (
    <div className="banner" role="alert">
      {message}
      {wait ? ` Try again in ${wait} seconds.` : null}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

/**
 * A slide-in panel.
 *
 * `config` marks it as a place where things are set up rather than recorded --
 * a different border and an explicit badge, so it cannot be mistaken for the
 * ledger.
 */
export function Panel({
  title,
  onClose,
  config = false,
  wide = false,
  children,
}: {
  title: string;
  onClose: () => void;
  config?: boolean;
  /** For a panel that holds a table you have to read row by row. */
  wide?: boolean;
  children: ReactNode;
}) {
  const panel = useRef<HTMLElement>(null);

  // Every caller passes an inline arrow, so `onClose` is a new function each
  // render. Read it through a ref and run the effect once, or the panel
  // re-focuses itself mid-keystroke on every render.
  const close = useRef(onClose);
  close.current = onClose;

  // Escape on the document, not on the backdrop: a bubbling handler only fires
  // once focus is already inside, and three of these panels have no autofocused
  // field. Tab is cycled here too -- a dialog that says aria-modal and then lets
  // you tab into the table behind it is lying.
  useEffect(() => {
    const returnTo = document.activeElement as HTMLElement | null;
    panel.current?.focus();

    const key = (event: KeyboardEvent) => {
      // A confirmation open inside this panel (#199) owns the keyboard: its
      // Escape closes it and not the panel under it, and it cycles Tab itself.
      if (panel.current?.querySelector(".dialog")) return;
      if (event.key === "Escape") {
        close.current();
        return;
      }
      if (event.key !== "Tab" || !panel.current) return;
      const stops = panel.current.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      if (stops.length === 0) return;
      const first = stops[0];
      const last = stops[stops.length - 1];
      const here = document.activeElement;
      if (event.shiftKey && (here === first || here === panel.current)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && here === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("keydown", key);
      returnTo?.focus?.();
    };
  }, []);

  return (
    <div className="panel-backdrop" onClick={onClose} role="presentation">
      <aside
        ref={panel}
        tabIndex={-1}
        className={[ "panel", config && "config-panel", wide && "panel-wide" ]
          .filter(Boolean)
          .join(" ")}
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <header>
          <div>
            {config ? <div className="config-badge">Settings</div> : null}
            <h2>{title}</h2>
          </div>
          <button onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>
        {children}
      </aside>
    </div>
  );
}

export function Money({ minor, currency }: { minor: number; currency: string }) {
  return (
    <span className={minor < 0 ? "amount neg" : "amount pos"}>{format(minor, currency)}</span>
  );
}

/**
 * A bubble that explains the field next to it.
 *
 * Deliberately a button and not a hover tooltip: hover help is unreachable on a
 * phone and invisible to a keyboard. It closes on Escape and on a click
 * anywhere else, so it never sits on top of the form you are trying to fill in.
 */
export function Hint({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const holder = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => {
      if (!holder.current?.contains(event.target as Node)) setOpen(false);
    };
    // Capture phase, and stop it there: these bubbles open inside Panels, whose
    // own Escape listener would otherwise close the whole panel behind them.
    const key = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      setOpen(false);
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", key, true);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", key, true);
    };
  }, [open]);

  return (
    <span className="hint" ref={holder}>
      <button
        type="button"
        className="hint-open"
        aria-expanded={open}
        aria-label={open ? `Hide help: ${label}` : `What's this? ${label}`}
        onClick={() => setOpen((was) => !was)}
      >
        ?
      </button>
      {open && (
        <span className="bubble" role="note">
          {children}
        </span>
      )}
    </span>
  );
}

export type SortKey =
  | "date"
  | "account"
  | "payee"
  | "category"
  | "memo"
  | "amount"
  | "cleared"
  | "source";

export { fixed, moneyKey, sortRows };
export type { FixedGroup, SortDirection, SortKeyPart, SortValue };

/**
 * Sorting state for one table.
 *
 * Every list in this app sorts at its headers, so the three lines of state
 * that takes live here once rather than in each screen. `initial` may be a key
 * no heading uses -- that is how a list opens on an order the server chose,
 * with no arrow lit until a heading takes over.
 */
export function useSort<K extends string>(initial: K, initialDirection: SortDirection = "asc") {
  const [sort, setSort] = useState<K>(initial);
  const [direction, setDirection] = useState<SortDirection>(initialDirection);
  const onSort = useCallback((column: K, next: SortDirection) => {
    setSort(column);
    setDirection(next);
  }, []);
  return { sort, direction, onSort };
}

/**
 * A column heading you can sort by.
 *
 * The arrow is only drawn on the column actually in use -- an arrow on every
 * heading tells you nothing about which one is sorted. Clicking the column
 * already in use turns it around; clicking another starts it in the direction
 * that column is usually wanted in (newest and largest first, names A→Z).
 */
/**
 * Who did something, and what they did it with.
 *
 * An agent key borrows a person's authority. The whole design keeps
 * `actor_id` as the human deliberately, so the trail never says a robot did
 * it -- but that only works if the trail *also* says the human was not at the
 * keyboard. Without the second half, a row an agent created and a row the owner
 * typed are indistinguishable in the one place whose job is to distinguish.
 *
 * One component for all three readers -- the History list, its panel, and the
 * transaction panel's own log -- because three renderings of one fact is how
 * two of them end up disagreeing about a batch.
 *
 * Two elements rather than one composed string: the client styles it, and
 * nothing has to parse a sentence back apart to do so.
 */
export function Actor({ name, via }: { name: string | null; via?: string | null }) {
  if (!name && !via) return <>—</>;
  return (
    <>
      {name ?? "—"}
      {via ? (
        <span className="via" title={`Through the agent key ${via}`}>
          {" · via "}
          {via}
        </span>
      ) : null}
    </>
  );
}

export function SortHeading<K extends string>({
  label,
  short,
  column,
  sort,
  direction,
  onSort,
  align,
  className,
  children,
}: {
  label: string;
  /**
   * What to draw when the heading is what sets the column's width.
   *
   * A column holding one letter should not be six characters wide because its
   * heading is a word -- but the accessible name stays the full `label`, so a
   * sort control is never announced as "S". The glyph is hidden from the
   * accessibility tree and the word is carried beside it, which is the same
   * bargain the Cleared and Source *values* already make one row down.
   */
  short?: string;
  column: K;
  sort: K;
  direction: SortDirection;
  onSort: (column: K, direction: SortDirection) => void;
  align?: "right";
  /** For a column with a width of its own, such as the accounts flag. */
  className?: string;
  /**
   * Beside the sort button rather than in it -- the register's resize handle,
   * which must not sort the column it is being dragged across.
   */
  children?: ReactNode;
}) {
  const active = sort === column;
  // Generic over the key so the register and the import preview share one
  // heading: the register's sort is a server round trip, the preview's is a
  // sort of rows already in hand, and neither needs its own arrow.
  const natural: SortDirection = column === "date" || column === "amount" ? "desc" : "asc";
  const next: SortDirection = active ? (direction === "asc" ? "desc" : "asc") : natural;

  return (
    <th
      className={[align === "right" ? "amount" : "", "sortable", className ?? ""]
        .filter(Boolean)
        .join(" ")}
      aria-sort={active ? (direction === "asc" ? "ascending" : "descending") : "none"}
    >
      <button type="button" onClick={() => onSort(column, next)} title={`Sort by ${label}`}>
        {short ? (
          <>
            <span aria-hidden="true">{short}</span>
            <span className="sr-only">{label}</span>
          </>
        ) : (
          label
        )}
        <span className={active ? "arrow on" : "arrow"} aria-hidden="true">
          {active ? (direction === "asc" ? "▲" : "▼") : "▾"}
        </span>
      </button>
      {children}
    </th>
  );
}


/**
 * A dialog in the middle of the screen, for a question that interrupts.
 *
 * Distinct from `Panel`, which slides in beside what you were doing and lets
 * you keep reading it. This one is for a question that has to be answered
 * before the thing underneath makes sense -- an offer about the rows you are
 * looking at, where carrying on down the table without answering would mean
 * answering it by accident.
 *
 * Same keyboard contract as `Panel`: Escape closes it, Tab cycles inside it. A
 * dialog that says `aria-modal` and then lets you tab into the page behind it
 * is lying.
 */
export function Dialog({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const box = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;

  useEffect(() => {
    const returnTo = document.activeElement as HTMLElement | null;
    box.current?.focus();

    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        close.current();
        return;
      }
      if (event.key !== "Tab" || !box.current) return;
      const stops = box.current.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      if (stops.length === 0) return;
      const first = stops[0];
      const last = stops[stops.length - 1];
      const here = document.activeElement;
      if (event.shiftKey && (here === first || here === box.current)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && here === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("keydown", key);
      returnTo?.focus?.();
    };
  }, []);

  return (
    <div className="dialog-backdrop" onClick={onClose} role="presentation">
      <div
        ref={box}
        tabIndex={-1}
        className="dialog"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <header>
          <h2>{title}</h2>
          <button onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>
        {children}
      </div>
    </div>
  );
}

/**
 * A message that appears, says one thing, and goes.
 *
 * For confirming something that already happened, where a banner would sit
 * there afterwards needing to be dismissed. It is `role="status"`, not
 * `alert`: it is not interrupting, and a screen reader should hear it when it
 * finishes the sentence it is on.
 *
 * It does not carry anything you would need to act on. Anything that does
 * belongs where the action is, because a message that vanishes is a message
 * somebody can miss.
 */
export function Toasts({
  items,
  onDone,
}: {
  items: { id: number; text: string }[];
  onDone: (id: number) => void;
}) {
  useEffect(() => {
    if (items.length === 0) return;
    const timers = items.map((one) => setTimeout(() => onDone(one.id), 4500));
    return () => timers.forEach(clearTimeout);
  }, [items, onDone]);

  if (items.length === 0) return null;
  return (
    <div className="toasts" role="status" aria-live="polite">
      {items.map((one) => (
        <div className="toast" key={one.id}>
          {one.text}
        </div>
      ))}
    </div>
  );
}

/** One place to raise them from, so a screen does not grow its own. */
export function useToasts() {
  const [items, setItems] = useState<{ id: number; text: string }[]>([]);
  const say = useCallback((text: string) => {
    setItems((was) => [...was, { id: Date.now() + Math.random(), text }]);
  }, []);
  const dismiss = useCallback((id: number) => {
    setItems((was) => was.filter((one) => one.id !== id));
  }, []);
  return { items, say, dismiss };
}
