/**
 * The (i) beside a currency on the Exchange rates page (#411): which of the
 * household's accounts are in it, so it is clear why it is being fetched.
 *
 * The (?) `Hint`'s button and bubble, with one difference: the bubble is
 * `position: fixed` rather than absolute. It opens inside a table, and the
 * table's `overflow-x: auto` scroller would otherwise clip it, or grow a
 * scroll bar of its own to hold it. Sideways it is kept inside the window by
 * the same `bubbleNudge` (#412); up and down it opens below the button, or
 * above when that has more room, and a long list scrolls inside it.
 *
 * It stays next to the button in the document, so Tab goes from the button
 * straight into the list. Escape, a click anywhere else, or tabbing past the
 * list closes it.
 */

import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { t } from "@lingui/core/macro";
import { Trans } from "@lingui/react/macro";
import { useLingui } from "@lingui/react";
import { bubbleNudge } from "./bits";
import { compareNames, countryName } from "../lib/locale";
import type { FxAccountRef } from "../lib/fx";

interface Box {
  left: number;
  top: number;
  bottom: number;
}

export interface Placement {
  left: number;
  top: number;
  maxHeight: number;
  /** Opened above the button, so the arrow points down from its foot. */
  above: boolean;
  /** Where the arrow sits from the bubble's left edge, to point at the button. */
  arrow: number;
}

/**
 * Where a bubble of `size` goes for a button at `button`, all of it inside a
 * `viewport`, `margin` clear of every edge.
 *
 * Sideways: starts 8px left of the button, as the (?) bubble does, and is
 * moved by `bubbleNudge`. Up and down: below, unless it does not fit there
 * and there is more room above; whichever side it takes caps its height, and
 * the list scrolls inside.
 */
export function placeBubble(
  button: Box,
  size: { width: number; height: number },
  viewport: { width: number; height: number },
  margin = 16,
  gap = 8,
): Placement {
  const start = button.left - 8;
  const shift = bubbleNudge(start, start + size.width, viewport.width, margin);
  const arrow = Math.min(Math.max(6, 10 - shift), Math.max(6, size.width - 18));
  const below = viewport.height - margin - (button.bottom + gap);
  const above = button.top - gap - margin;
  if (size.height > below && above > below) {
    const height = Math.min(size.height, above);
    return { left: start + shift, top: button.top - gap - height, maxHeight: above, above: true, arrow };
  }
  return { left: start + shift, top: button.bottom + gap, maxHeight: Math.max(below, 0), above: false, arrow };
}

export function CurrencyAccounts({ currency, accounts }: { currency: string; accounts: FxAccountRef[] }) {
  // Re-renders its words when the language changes.
  useLingui();
  const [open, setOpen] = useState(false);
  const [place, setPlace] = useState<Placement | null>(null);
  const holder = useRef<HTMLSpanElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const bubble = useRef<HTMLDivElement>(null);
  const size = useRef<{ width: number; height: number } | null>(null);
  const listId = useId();
  const label = t({ message: `Accounts in ${currency}`, comment: "Button and list on the Exchange rates page: the household's accounts held in this currency code" });

  const sorted = [...accounts].sort((a, b) => compareNames(a.name, b.name));

  // Measured before paint, unplaced and hidden, then put where it fits -- so
  // it never shows half off the window and then jumps.
  useLayoutEffect(() => {
    if (!open) {
      setPlace(null);
      size.current = null;
      return;
    }
    if (place !== null || !bubble.current || !button.current) return;
    const box = bubble.current.getBoundingClientRect();
    size.current = { width: box.width, height: box.height };
    setPlace(placeBubble(button.current.getBoundingClientRect(), size.current, { width: window.innerWidth, height: window.innerHeight }));
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => {
      if (!holder.current?.contains(event.target as Node)) setOpen(false);
    };
    // Capture phase, and stop it there, as the (?) bubble does: a Panel's own
    // Escape listener would otherwise close the whole panel behind it.
    const key = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      setOpen(false);
      button.current?.focus();
    };
    // Fixed, so it follows its button when the page or the table scrolls; the
    // list's own scrolling leaves it where it is.
    const scrolled = (event: Event) => {
      if (event.target instanceof Node && bubble.current?.contains(event.target)) return;
      if (!size.current || !button.current) return;
      setPlace(placeBubble(button.current.getBoundingClientRect(), size.current, { width: window.innerWidth, height: window.innerHeight }));
    };
    // A new width can change how wide it is: measure it again.
    const resized = () => setPlace(null);
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", key, true);
    document.addEventListener("scroll", scrolled, true);
    window.addEventListener("resize", resized);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", key, true);
      document.removeEventListener("scroll", scrolled, true);
      window.removeEventListener("resize", resized);
    };
  }, [open]);

  const style: CSSProperties = place
    ? ({ left: place.left, top: place.top, maxHeight: place.maxHeight, "--bubble-arrow": `${place.arrow}px` } as CSSProperties)
    : { left: 0, top: 0, visibility: "hidden" };

  return (
    <span
      className="hint"
      ref={holder}
      onBlur={(event) => {
        // Tabbing past the list closes it, the way a click elsewhere does.
        if (event.relatedTarget && !holder.current?.contains(event.relatedTarget as Node)) setOpen(false);
      }}
    >
      <button
        ref={button}
        type="button"
        className="hint-open"
        aria-label={label}
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        onClick={() => setOpen((was) => !was)}
      >
        i
      </button>
      {open && (
        <div
          ref={bubble}
          id={listId}
          className={`bubble fixed-bubble${place?.above ? " above" : ""}`}
          role="region"
          aria-label={label}
          // Focusable so a keyboard can scroll a long list.
          tabIndex={0}
          style={style}
        >
          <ul className="currency-accounts">
            {sorted.map((account) => (
              <li key={account.id}>
                <span className="currency-account-name">{account.name}</span>
                {account.closed ? (
                  <span className="tag closed" title={t`this account is closed`}>
                    <Trans comment="Tag beside a name on the Accounts screen: adjective, an account no longer in use. See GLOSSARY.md">
                      Closed
                    </Trans>
                  </span>
                ) : null}
                <div className="small muted">
                  {account.institution ?? t({ message: "No bank set", comment: "In the list of accounts in a currency on the Exchange rates page: the account names no bank" })}
                  {" · "}
                  {account.country ? (
                    <>
                      <span aria-hidden="true">{account.flag}</span> {countryName(account.country, account.country_name ?? account.country)}
                    </>
                  ) : (
                    t`no country set`
                  )}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </span>
  );
}
