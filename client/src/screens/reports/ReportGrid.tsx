/**
 * The frame the Income vs Expense table sits in (#401).
 *
 * **Why two tables.** A report wider than the screen has to scroll sideways
 * inside a box of its own, and a box with `overflow-x: auto` is a scroll
 * container in *both* directions. A `position: sticky; top: 0` heading inside
 * it sticks to that box, which never scrolls up or down -- so the month
 * headings went off the top with the page, however sticky they were declared.
 * The heading is therefore drawn as a table of its own, in a band that sticks
 * to the page, and kept in step with the body's sideways scroll. Both tables
 * share one `<colgroup>`, laid out `fixed`, so a column is the same width in
 * each whatever is in it.
 *
 * The drawn heading is `aria-hidden`: it is a picture of the real one. The
 * body table keeps a real `<thead>`, with its words for a screen reader and no
 * height on screen, so every figure is still announced under its month.
 *
 * **The month map** replaces the scrollbar that sat under the last row. It
 * travels with the heading, shows every month in the range with its net as a
 * small bar, and outlines the months in view. Drag the outline, click a month,
 * use the arrows, or the keys; a trackpad and Shift + wheel still scroll the
 * figures as before. It is shown only when the table is wider than the
 * screen -- when everything fits there is nowhere to go.
 */

import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent, PointerEvent, ReactNode } from "react";
import { t } from "@lingui/core/macro";
import { format } from "../../lib/money";
import { monthLabel, monthShort } from "../../lib/locale";

/** Column widths, in pixels, and whether Total and Average stay pinned. */
export type GridSizes = { label: number; month: number; summary: number; pinRight: boolean };

/**
 * The widths for a window this wide.
 *
 * Fixed rather than measured, because the heading and the body are two
 * tables and only fixed widths are guaranteed to agree. Total and Average are
 * pinned only where there is room for months beside them: on a phone, two
 * pinned summaries and the pinned label would leave no month in view.
 */
export function gridSizes(width: number): GridSizes {
  if (width < 640) return { label: 170, month: 104, summary: 112, pinRight: false };
  return { label: 220, month: 116, summary: 124, pinRight: width >= 760 };
}

/**
 * Which months are in view, from where the body is scrolled to.
 *
 * `first` is fractional -- a month half scrolled past is half in view -- and
 * `visible` is how many months' width the window has between the pinned
 * columns. Pure, so the test drives the same arithmetic the map does.
 */
export function monthsInView(
  scrollLeft: number,
  clientWidth: number,
  count: number,
  sizes: GridSizes,
): { first: number; visible: number } {
  const room = clientWidth - sizes.label - (sizes.pinRight ? 2 * sizes.summary : 0);
  const visible = Math.max(1, Math.min(count, room / sizes.month));
  const first = Math.max(0, Math.min(count - visible, scrollLeft / sizes.month));
  return { first, visible };
}

/** Where to scroll so that the last months fill the window. */
export function latestFirst(count: number, visible: number, sizes: GridSizes): number {
  return Math.max(0, (count - visible) * sizes.month);
}

/** Where to scroll so that month `index` sits in the middle of the window. */
export function centreOn(index: number, visible: number, sizes: GridSizes): number {
  return Math.max(0, (index + 0.5 - visible / 2) * sizes.month);
}

function useWindowWidth(): number {
  const [width, setWidth] = useState(() => (typeof window === "undefined" ? 1280 : window.innerWidth));
  useEffect(() => {
    const on = () => setWidth(window.innerWidth);
    window.addEventListener("resize", on);
    return () => window.removeEventListener("resize", on);
  }, []);
  return width;
}

function calm(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** `scrollTo` where the element has it (jsdom does not), `scrollLeft` where not. */
function scrollLeftTo(el: HTMLElement, left: number) {
  if (typeof el.scrollTo === "function") el.scrollTo({ left, behavior: calm() ? "auto" : "smooth" });
  else el.scrollLeft = left;
}

export function ReportGrid({
  months,
  net,
  currency,
  head,
  children,
}: {
  months: string[];
  /** Net per `YYYY-MM`, for the bars on the month map. */
  net: Record<string, number>;
  currency: string;
  /** The heading row's cells. `hidden` asks for them as screen-reader words only. */
  head: (hidden: boolean) => ReactNode;
  /** The body: the bands and the footer. */
  children: ReactNode;
}) {
  const windowWidth = useWindowWidth();
  const sizes = useMemo(() => gridSizes(windowWidth), [windowWidth]);
  const frame = useRef<HTMLDivElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const top = useRef<HTMLDivElement>(null);
  const track = useRef<HTMLDivElement>(null);
  const lens = useRef<HTMLSpanElement>(null);
  const drag = useRef<{ x: number; left: number; per: number } | null>(null);
  const [overflowing, setOverflowing] = useState(false);
  const bodyId = useId();
  const width = sizes.label + months.length * sizes.month + 2 * sizes.summary;

  const view = useCallback(() => {
    const el = body.current;
    return el
      ? monthsInView(el.scrollLeft, el.clientWidth, months.length, sizes)
      : { first: 0, visible: months.length };
  }, [months.length, sizes]);

  /** Everything that follows the body's sideways position. Direct DOM writes: it runs on every scroll frame. */
  const sync = useCallback(() => {
    const el = body.current;
    if (!el) return;
    if (top.current) top.current.scrollLeft = el.scrollLeft;
    const max = el.scrollWidth - el.clientWidth;
    frame.current?.classList.toggle("ie-left-hidden", el.scrollLeft > 1);
    frame.current?.classList.toggle("ie-right-hidden", el.scrollLeft < max - 1);
    const { first, visible } = view();
    if (lens.current && months.length > 0) {
      lens.current.style.left = `${(first / months.length) * 100}%`;
      lens.current.style.width = `${(visible / months.length) * 100}%`;
    }
    if (track.current && months.length > 0) {
      const from = Math.round(first);
      const to = Math.min(months.length - 1, Math.ceil(first + visible) - 1);
      track.current.querySelectorAll<HTMLElement>(".ie-map-month").forEach((chip, index) => {
        chip.classList.toggle("in-view", index >= from && index <= to);
      });
      track.current.setAttribute("aria-valuenow", String(max > 0 ? Math.round((el.scrollLeft / max) * 100) : 0));
      track.current.setAttribute(
        "aria-valuetext",
        t({
          message: `${monthLabel(months[from])} to ${monthLabel(months[to])}`,
          comment: "Screen-reader text on the Income vs Expense report's month map: the first and last month in view",
        }),
      );
    }
  }, [months, view]);

  // Wider than the screen or not, measured after layout and again whenever
  // the frame changes size. On first overflowing, open on the latest months:
  // they are the ones a person most often came to see. On the latest months,
  // not at the far end: where Total and Average are not pinned, the far end
  // is those two alone and no month at all.
  const monthsKey = months.join(",");
  const opened = useRef<string | null>(null);
  useLayoutEffect(() => {
    const el = body.current;
    if (!el) return;
    const measure = () => {
      const wide = el.scrollWidth > el.clientWidth + 1;
      setOverflowing(wide);
      if (wide && opened.current !== monthsKey) {
        opened.current = monthsKey;
        const { visible } = monthsInView(0, el.clientWidth, months.length, sizes);
        el.scrollLeft = latestFirst(months.length, visible, sizes);
      }
      sync();
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const watch = new ResizeObserver(measure);
    watch.observe(el);
    return () => watch.disconnect();
  }, [monthsKey, width, sync, months.length, sizes]);

  useLayoutEffect(sync, [overflowing, sync]);

  // A sideways wheel over the heading or the map moves the figures, the way
  // it would over the figures themselves. Up and down is left to the page.
  useEffect(() => {
    const targets = [top.current, track.current].filter(Boolean) as HTMLElement[];
    const wheel = (event: WheelEvent) => {
      const el = body.current;
      if (!el) return;
      if (Math.abs(event.deltaX) > Math.abs(event.deltaY) || event.shiftKey) {
        el.scrollLeft += event.deltaX || event.deltaY;
        event.preventDefault();
      }
    };
    targets.forEach((one) => one.addEventListener("wheel", wheel, { passive: false }));
    return () => targets.forEach((one) => one.removeEventListener("wheel", wheel));
  }, [overflowing]);

  const step = (count: number) => {
    const el = body.current;
    if (el) scrollLeftTo(el, el.scrollLeft + count * sizes.month);
  };

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    const el = body.current;
    const box = track.current?.getBoundingClientRect();
    if (!el || !box || box.width === 0) return;
    const per = box.width / months.length;
    if (event.target === lens.current) {
      drag.current = { x: event.clientX, left: el.scrollLeft, per };
      lens.current?.classList.add("dragging");
      track.current?.setPointerCapture?.(event.pointerId);
      event.preventDefault();
      return;
    }
    const index = Math.max(0, Math.min(months.length - 1, Math.floor((event.clientX - box.left) / per)));
    scrollLeftTo(el, centreOn(index, view().visible, sizes));
  };
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const el = body.current;
    if (!drag.current || !el) return;
    el.scrollLeft = drag.current.left + ((event.clientX - drag.current.x) / drag.current.per) * sizes.month;
  };
  const endDrag = () => {
    drag.current = null;
    lens.current?.classList.remove("dragging");
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const el = body.current;
    if (!el) return;
    const page = Math.max(1, Math.floor(view().visible));
    const moves: Record<string, number> = { ArrowLeft: -1, ArrowRight: 1, PageUp: -page, PageDown: page };
    if (event.key in moves) step(moves[event.key]);
    else if (event.key === "Home") scrollLeftTo(el, 0);
    else if (event.key === "End") scrollLeftTo(el, el.scrollWidth);
    else return;
    event.preventDefault();
  };

  const biggest = Math.max(1, ...months.map((period) => Math.abs(net[period] ?? 0)));
  const columns = (
    <colgroup>
      <col style={{ width: sizes.label }} />
      {months.map((period) => (
        <col key={period} style={{ width: sizes.month }} />
      ))}
      <col style={{ width: sizes.summary }} />
      <col style={{ width: sizes.summary }} />
    </colgroup>
  );
  const tableStyle: CSSProperties = { width, minWidth: "100%" };

  return (
    <div
      ref={frame}
      className={sizes.pinRight ? "ie-grid ie-pin-right" : "ie-grid"}
      style={{ "--ie-summary": `${sizes.summary}px` } as CSSProperties}
    >
      <div className="ie-grid-head">
        {overflowing && (
          <div className="ie-map">
            <button
              type="button"
              className="ie-map-step"
              aria-label={t({ message: "Earlier months", comment: "Screen-reader name of the left arrow on the Income vs Expense report's month map" })}
              onClick={() => step(-3)}
            >
              ‹
            </button>
            <div
              ref={track}
              className="ie-map-track"
              role="scrollbar"
              tabIndex={0}
              aria-controls={bodyId}
              aria-orientation="horizontal"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={0}
              aria-label={t({ message: "Months in view", comment: "Screen-reader name of the month map on the Income vs Expense report: drag or use the arrow keys to move" })}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={endDrag}
              onPointerCancel={endDrag}
              onKeyDown={onKeyDown}
            >
              {months.map((period, index) => {
                const value = net[period] ?? 0;
                const height = value === 0 ? 0 : Math.max(8, Math.round((Math.abs(value) / biggest) * 50));
                const labelled = months.length <= 24 || period.endsWith("-01") || index % 3 === 0;
                return (
                  <span
                    key={period}
                    className="ie-map-month"
                    title={`${monthLabel(period)}: ${format(value, currency)}`}
                    aria-hidden="true"
                  >
                    <span className="ie-map-year">
                      {index === 0 || period.endsWith("-01") ? `’${period.slice(2, 4)}` : ""}
                    </span>
                    <span className="ie-map-bars">
                      {height > 0 && <i className={value > 0 ? "up" : "down"} style={{ height: `${height}%` }} />}
                    </span>
                    <span className="ie-map-name">{labelled ? monthShort(period) : ""}</span>
                  </span>
                );
              })}
              <span ref={lens} className="ie-map-lens" aria-hidden="true" />
            </div>
            <button
              type="button"
              className="ie-map-step"
              aria-label={t({ message: "Later months", comment: "Screen-reader name of the right arrow on the Income vs Expense report's month map" })}
              onClick={() => step(3)}
            >
              ›
            </button>
          </div>
        )}
        <div ref={top} className="ie-grid-head-scroll" aria-hidden="true">
          <table className="report" style={tableStyle}>
            {columns}
            <thead>
              <tr>{head(false)}</tr>
            </thead>
          </table>
        </div>
      </div>

      <div ref={body} id={bodyId} className="matrix-scroll ie-grid-body" onScroll={sync}>
        <table className="report" style={tableStyle}>
          {columns}
          <thead className="report-sr-head">
            <tr>{head(true)}</tr>
          </thead>
          {children}
        </table>
      </div>
    </div>
  );
}
