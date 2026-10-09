/**
 * A filter you open, tick things in, and close.
 *
 * The one thing it does that a list of checkboxes does not: items arrive
 * **already gathered into groups**, and every group heading is itself a
 * tri-state checkbox that ticks or clears its whole group in one action. That
 * is the difference between choosing "the three Spanish accounts" in one click
 * and finding them among eleven.
 *
 * The grouping is the *caller's* idea, not this component's. Accounts group by
 * country and by type; categories group by the group they already belong to.
 * Passing the groups in rather than a `groupBy` key is what lets one component
 * serve both without knowing what either is.
 *
 * Nothing applies on a delay and there is no Apply button: every tick changes
 * the report behind it, so the filter and what you are looking at can never
 * disagree. Done just closes the popover.
 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { t } from "@lingui/core/macro";
import { useLingui } from "@lingui/react";
import { Trans } from "@lingui/react/macro";

export type PickerItem = { id: string; label: string; hint?: string };
export type PickerGroup = { key: string; label: string; items: PickerItem[] };

/**
 * `null` means "everything", and it is not the same value as "every id is
 * ticked" even though they show identically.
 *
 * A household that adds an account tomorrow is still looking at everything if
 * the filter says `null`, and is looking at everything-but-the-new-one if the
 * filter froze today's ids. The first is what a person means by leaving a
 * filter alone, so it is what the untouched state holds -- and it is also what
 * keeps the query string short in the common case.
 */
export type Selection = string[] | null;

export function GroupedPicker({
  label,
  groups,
  value,
  onChange,
  /**
   * A tick that is not an id, for a slice like "Uncategorised". `count` is
   * how many rows sit in it, shown beside it whether ticked or not (#188).
   */
  extra,
}: {
  label: string;
  groups: PickerGroup[];
  value: Selection;
  onChange: (next: Selection) => void;
  extra?: {
    label: string;
    hint?: string;
    count?: number;
    checked: boolean;
    onChange: (next: boolean) => void;
  };
}) {
  // Re-renders its words when the language changes.
  useLingui();
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const holder = useRef<HTMLDivElement>(null);
  const pop = useRef<HTMLDivElement>(null);
  //: Which edge of the button the popover hangs from. Left, as #160 settled,
  //: unless that runs it off the right of the screen -- a picker at the end of
  //: a filter line (#188) -- and hanging it from the right would not put it
  //: under the sidebar instead, which is the #160 bug over again.
  const [hang, setHang] = useState<"left" | "right">("left");

  useLayoutEffect(() => {
    if (!open) {
      setHang("left");
      return;
    }
    const button = holder.current?.getBoundingClientRect();
    const width = pop.current?.offsetWidth;
    if (!button || !width) return;
    const content = holder.current?.closest("main")?.getBoundingClientRect().left ?? 0;
    const overflowsRight = button.left + width > document.documentElement.clientWidth;
    const fitsLeftwards = button.right - width >= content;
    setHang(overflowsRight && fitsLeftwards ? "right" : "left");
  }, [open]);

  const everyId = useMemo(
    () => groups.flatMap((group) => group.items.map((item) => item.id)),
    [groups],
  );
  // `null` is "everything", so it renders as every box ticked.
  const chosen = useMemo(() => new Set(value ?? everyId), [value, everyId]);

  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => {
      if (!holder.current?.contains(event.target as Node)) setOpen(false);
    };
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

  const term = search.trim().toLowerCase();
  const shown = useMemo(() => {
    if (!term) return groups;
    return groups
      .map((group) => ({
        ...group,
        items: group.items.filter(
          (item) =>
            item.label.toLowerCase().includes(term) ||
            (item.hint ?? "").toLowerCase().includes(term) ||
            group.label.toLowerCase().includes(term),
        ),
      }))
      .filter((group) => group.items.length > 0);
  }, [groups, term]);

  /**
   * Back to `null` when everything ends up ticked, so that "I unticked one and
   * ticked it again" leaves the filter in the state it started in rather than
   * in a frozen list that happens to match today.
   */
  const settle = (next: Set<string>) => {
    if (next.size === everyId.length) return onChange(null);
    onChange([...next]);
  };

  const toggle = (id: string) => {
    const next = new Set(chosen);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    settle(next);
  };

  const toggleGroup = (group: PickerGroup) => {
    const ids = group.items.map((item) => item.id);
    const all = ids.every((id) => chosen.has(id));
    const next = new Set(chosen);
    for (const id of ids) {
      if (all) next.delete(id);
      else next.add(id);
    }
    settle(next);
  };

  /**
   * Untouched means every id is in *and* the extra slice, if there is one, is
   * on. Unticking "Uncategorised" narrows the report as surely as unticking an
   * account does, so the button has to stop claiming to show everything.
   */
  const untouched = value === null && (!extra || extra.checked);

  // A count rather than a list when it is narrowed: the list does not fit, and
  // "3 of 11" is the fact a person is checking for. The extra tick counts as
  // one of them, and when it is the only one it is named: "0 of 40" for "the
  // rows that need a category" would be the button saying nothing is shown.
  const extraOn = extra?.checked ? 1 : 0;
  const summary = untouched
    ? label
    : extra && extraOn && chosen.size === 0
      ? extra.label
      : `${chosen.size + extraOn} of ${everyId.length + (extra ? 1 : 0)}`;

  return (
    <div className="picker" ref={holder}>
      <button
        type="button"
        className={untouched ? "chip" : "chip active"}
        aria-expanded={open}
        aria-haspopup="true"
        onClick={() => setOpen((was) => !was)}
      >
        {summary} <span aria-hidden="true">▾</span>
      </button>

      {open && (
        <div
          ref={pop}
          className={hang === "right" ? "picker-pop hang-right" : "picker-pop"}
          role="group"
          aria-label={label}
        >
          <input
            className="picker-search"
            placeholder={t({ message: `Search ${label.toLowerCase()}`, comment: "Placeholder of a search box in a dropdown; the placeholder is what it lists, e.g. accounts" })}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            autoFocus
          />

          <div className="picker-list">
            {extra && (
              <label className="picker-item picker-extra">
                <input
                  type="checkbox"
                  checked={extra.checked}
                  onChange={(e) => extra.onChange(e.target.checked)}
                />
                <span>
                  {extra.label}
                  {extra.hint && <span className="muted small"> {extra.hint}</span>}
                </span>
                {extra.count !== undefined && <span className="nav-badge">{extra.count}</span>}
              </label>
            )}

            {shown.map((group) => {
              const ids = group.items.map((item) => item.id);
              const all = ids.every((id) => chosen.has(id));
              const some = !all && ids.some((id) => chosen.has(id));
              return (
                <div key={group.key} className="picker-group">
                  <label className="picker-item picker-head">
                    <input
                      type="checkbox"
                      checked={all}
                      ref={(box) => {
                        // The third state: some of this group, not all. Only
                        // reachable from JS, and without it a half-ticked
                        // group is indistinguishable from an empty one.
                        if (box) box.indeterminate = some;
                      }}
                      onChange={() => toggleGroup(group)}
                    />
                    <span>{group.label}</span>
                  </label>
                  {group.items.map((item) => (
                    <label key={item.id} className="picker-item">
                      <input
                        type="checkbox"
                        checked={chosen.has(item.id)}
                        onChange={() => toggle(item.id)}
                      />
                      <span>
                        {item.label}
                        {item.hint && <span className="muted small"> {item.hint}</span>}
                      </span>
                    </label>
                  ))}
                </div>
              );
            })}
            {shown.length === 0 && (
              <p className="muted small">
                <Trans comment="Shown in a dropdown when the search finds nothing">Nothing matches that.</Trans>
              </p>
            )}
          </div>

          <div className="picker-foot">
            <button type="button" className="link" onClick={() => onChange(null)}>
              Select all
            </button>
            <button type="button" className="link" onClick={() => onChange([])}>
              Select none
            </button>
            <button type="button" className="primary" onClick={() => setOpen(false)}>
              Done
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
