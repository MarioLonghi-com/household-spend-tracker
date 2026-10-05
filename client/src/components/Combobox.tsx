/**
 * Type-ahead over a list you may also type outside of.
 *
 * Replaces `<datalist>`, which browsers render however they like -- in several
 * of them nothing appears until you click the field or press the down arrow,
 * so the suggestions were effectively invisible while typing.
 *
 * **Nothing is highlighted until you ask for it.** That is deliberate, and it is
 * what keeps quick entry quick: the register's row submits on Enter, so if the
 * first match were pre-selected then "type Merc, press Enter" would quietly
 * change what you typed into Mercadona instead of adding the row. Suggestions
 * are offered; Enter only takes one once you have moved onto it with the arrows
 * or the mouse.
 */

import { useEffect, useId, useMemo, useRef, useState } from "react";

const MAX_SUGGESTIONS = 8;

/**
 * Prefix matches first, then anything containing it; case- and accent-insensitive.
 *
 * Returns positions in `options`, not the strings, so two options that read
 * alike are still two options and the one taken can be told apart (#19).
 */
function rank(options: string[], typed: string, limit: number = MAX_SUGGESTIONS): number[] {
  const every = options.map((_, at) => at);
  const needle = fold(typed);
  if (!needle) return every.slice(0, limit);

  const starts: number[] = [];
  const contains: number[] = [];
  for (const at of every) {
    const folded = fold(options[at]);
    if (folded === needle) continue; // already typed in full; nothing to offer
    if (folded.startsWith(needle)) starts.push(at);
    else if (folded.includes(needle)) contains.push(at);
  }
  return [...starts, ...contains].slice(0, limit);
}

function fold(text: string): string {
  return text
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .trim()
    .toLowerCase();
}

export function Combobox({
  value,
  onChange,
  options,
  placeholder,
  autoFocus,
  inputRef,
  onCommit,
  onCancel,
  browse = false,
  limit = MAX_SUGGESTIONS,
  "aria-label": ariaLabel,
}: {
  value: string;
  onChange: (next: string) => void;
  options: string[];
  placeholder?: string;
  autoFocus?: boolean;
  inputRef?: React.RefObject<HTMLInputElement>;
  /**
   * Enter with no suggestion highlighted, or focus leaving the field.
   *
   * Passed the value being committed rather than letting the caller read its
   * own state: taking a suggestion with Tab calls `onChange` and then blurs in
   * the same tick, so the caller's state has not re-rendered yet and reading it
   * would commit the half-typed text instead of the option just chosen.
   *
   * `taken` is the position in `options` of the suggestion the field holds,
   * when it holds one because it was picked from the list (by keyboard or
   * mouse) and nothing has been typed since; otherwise `undefined`. A caller
   * whose options can read alike -- a sentinel such as "Uncategorised" beside
   * a real category of that name -- uses it to commit the option chosen rather
   * than guess from the label (#19). Callers that only want the text ignore it.
   */
  onCommit?: (value: string, taken?: number) => void;
  /** Escape with the list already shut. */
  onCancel?: () => void;
  /**
   * Opening the field lists **every** option, and only typing narrows it
   * (#143).
   *
   * For a short list somebody browses as often as they type into -- the
   * categories. Without it the list was ranked against whatever the field
   * already held, which for a categorised row is that row's own category: a
   * list of the names containing "Groceries", minus Groceries itself, because
   * a name typed in full is not offered back. And an empty field stopped at
   * eight. Payees are the opposite case, hundreds of them nobody browses, and
   * leave this off.
   */
  browse?: boolean;
  /** How many suggestions at most. The categories pass `Infinity`. */
  limit?: number;
  "aria-label"?: string;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  //: Whether anything has been typed since the list last opened. Until it
  //: has, a browsing field shows the whole list rather than a ranking of text
  //: the person did not type this time.
  const [typedSinceOpen, setTypedSinceOpen] = useState(false);
  const holder = useRef<HTMLSpanElement>(null);
  const listId = useId();

  const browsing = browse && !typedSinceOpen;
  const matches = useMemo(
    () => (browsing ? options.map((_, at) => at).slice(0, limit) : rank(options, value, limit)),
    [browsing, options, value, limit],
  );
  const showing = open && matches.length > 0;

  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => {
      if (!holder.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  //: The value taken by keyboard this tick, for the blur that follows it.
  const justTaken = useRef<string | null>(null);
  //: Which option the field holds because it was picked, until typing
  //: replaces it. Handed to `onCommit` so the caller learns *which* option,
  //: not only its words.
  const picked = useRef<number | undefined>(undefined);

  const take = (at: number) => {
    const option = options[at];
    justTaken.current = option;
    picked.current = at;
    onChange(option);
    setOpen(false);
    setActive(-1);
  };

  function onKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (!showing) {
        setOpen(true);
        setActive(matches.length > 0 ? 0 : -1);
      } else {
        setActive((at) => (at + 1) % matches.length);
      }
      return;
    }
    if (event.key === "ArrowUp") {
      if (!showing) return;
      event.preventDefault();
      setActive((at) => (at <= 0 ? matches.length - 1 : at - 1));
      return;
    }
    if (event.key === "Escape") {
      if (!showing) {
        // Nothing of ours is open, so Escape means the thing we sit inside.
        if (onCancel) {
          event.preventDefault();
          event.stopPropagation();
          onCancel();
        }
        return;
      }
      // Stop here: the panels this sits inside close on Escape too, and one
      // keypress should shut one thing.
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
      setActive(-1);
      return;
    }
    if (event.key === "Enter") {
      if (showing && active >= 0) {
        // Taking a suggestion is the whole keypress. Without stopping it here
        // the register's row would also submit, so picking a payee would add
        // the transaction.
        event.preventDefault();
        event.stopPropagation();
        take(matches[active]);
        return;
      }
      // No suggestion under the cursor, so Enter means "this is my answer".
      // Inline in the register that saves the cell; in the quick-entry row
      // there is no onCommit and Enter belongs to the row, as it always did.
      if (onCommit) {
        event.preventDefault();
        event.stopPropagation();
        onCommit(value, picked.current);
      }
      return;
    }
    if (event.key === "Tab" && showing && active >= 0) {
      take(matches[active]);
    }
  }

  return (
    <span className="combo" ref={holder}>
      <input
        ref={inputRef}
        type="text"
        role="combobox"
        aria-expanded={showing}
        aria-controls={showing ? listId : undefined}
        aria-autocomplete="list"
        aria-activedescendant={showing && active >= 0 ? `${listId}-${active}` : undefined}
        aria-label={ariaLabel}
        autoComplete="off"
        spellCheck={false}
        placeholder={placeholder}
        autoFocus={autoFocus}
        value={value}
        onChange={(event) => {
          picked.current = undefined;
          onChange(event.target.value);
          setOpen(true);
          setActive(-1);
          setTypedSinceOpen(true);
        }}
        onFocus={() => {
          setOpen(true);
          setTypedSinceOpen(false);
        }}
        onBlur={(event) => {
          // Only when focus has actually left the whole control. Clicking a
          // suggestion moves focus inside it, and committing there would save
          // the half-typed text instead of the option just chosen.
          if (holder.current?.contains(event.relatedTarget as Node | null)) return;
          setOpen(false);
          const taken = justTaken.current;
          justTaken.current = null;
          onCommit?.(taken ?? value, picked.current);
        }}
        onKeyDown={onKeyDown}
      />
      {showing && (
        <ul className="combo-list" id={listId} role="listbox">
          {matches.map((at, index) => (
            <li
              key={at}
              id={`${listId}-${index}`}
              role="option"
              aria-selected={index === active}
              className={index === active ? "combo-option active" : "combo-option"}
              // mousedown, not click: click fires after blur, by which point the
              // list has closed and there is nothing left to click.
              onMouseDown={(event) => {
                event.preventDefault();
                take(at);
              }}
              onMouseEnter={() => setActive(index)}
            >
              {browsing ? options[at] : highlight(options[at], value)}
            </li>
          ))}
        </ul>
      )}
    </span>
  );
}

/** Show which part of the suggestion is the part you typed. */
function highlight(option: string, typed: string) {
  const at = fold(option).indexOf(fold(typed));
  if (!typed.trim() || at < 0) return option;
  const end = at + typed.trim().length;
  return (
    <>
      {option.slice(0, at)}
      <strong>{option.slice(at, end)}</strong>
      {option.slice(end)}
    </>
  );
}
