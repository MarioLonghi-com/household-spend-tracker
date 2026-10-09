import { useRef } from "react";
import { t } from "@lingui/core/macro";
import { useLingui } from "@lingui/react";
import { format } from "../lib/money";
import { moveSeam, SNAP_FRACTIONS } from "../lib/splitting";

/**
 * The transaction as one bar, cut into its parts, with seams you can move.
 *
 * Only ever two or three parts: past that the segments are too thin to grab
 * and the panel's typed amounts do the job (#28). Sizes in, sizes out -- the
 * parts' magnitudes, already adding up to the whole, so the bar never has to
 * draw a split that does not.
 *
 * A seam is a slider in its own right: Tab reaches it, the arrows move it a
 * minor unit, shift moves ten. Dragging sticks at the common fractions; the
 * keyboard does not, so the cent either side of a half is always reachable.
 */
export function SplitBar({
  magnitudes,
  currency,
  onChange,
}: {
  magnitudes: number[];
  currency: string;
  onChange: (magnitudes: number[]) => void;
}) {
  // Re-renders its words when the language changes.
  useLingui();
  const bar = useRef<HTMLDivElement>(null);
  const total = magnitudes.reduce((sum, one) => sum + one, 0);
  const starts = magnitudes.map((_, index) =>
    magnitudes.slice(0, index).reduce((sum, one) => sum + one, 0),
  );
  const seams = starts.slice(1);
  const share = (minor: number) => `${(minor / total) * 100}%`;

  // Where on the bar the pointer is, as a position in minor units.
  function at(clientX: number) {
    const box = bar.current?.getBoundingClientRect();
    if (!box || box.width === 0) return 0;
    return ((clientX - box.left) / box.width) * total;
  }

  return (
    <div className="split-bar-holder">
      <div className="split-bar" ref={bar}>
        {magnitudes.map((minor, index) => (
          <div
            key={index}
            className={`split-seg split-seg-${index + 1}`}
            style={{ left: share(starts[index]), width: share(minor) }}
            aria-hidden="true"
          >
            {minor * 8 >= total ? format(minor, currency) : null}
          </div>
        ))}
        {seams.map((position, seam) => (
          <div
            key={seam}
            className="split-seam"
            style={{ left: share(position) }}
            tabIndex={0}
            role="slider"
            aria-label={t({ message: `Between part ${seam + 1} and part ${seam + 2}`, comment: "Screen-reader name of the handle between two parts of a split transaction. See GLOSSARY.md" })}
            aria-valuemin={0}
            aria-valuemax={total}
            aria-valuenow={position}
            aria-valuetext={t({ message: `Part ${seam + 1} ${format(magnitudes[seam], currency)}, part ${seam + 2} ${format(magnitudes[seam + 1], currency)}`, comment: "Screen-reader value of a split handle: each part's number and amount. See GLOSSARY.md" })}
            onPointerDown={(event) => {
              event.preventDefault();
              event.currentTarget.focus();
              event.currentTarget.setPointerCapture?.(event.pointerId);
            }}
            onPointerMove={(event) => {
              if (!event.currentTarget.hasPointerCapture?.(event.pointerId)) return;
              onChange(moveSeam(magnitudes, seam, at(event.clientX), true));
            }}
            onKeyDown={(event) => {
              const step = event.shiftKey ? 10 : 1;
              const by =
                event.key === "ArrowRight" || event.key === "ArrowUp"
                  ? step
                  : event.key === "ArrowLeft" || event.key === "ArrowDown"
                    ? -step
                    : 0;
              if (by === 0) return;
              event.preventDefault();
              onChange(moveSeam(magnitudes, seam, position + by));
            }}
          >
            <span />
          </div>
        ))}
      </div>
      <div className="split-ticks" aria-hidden="true">
        {SNAP_FRACTIONS.map(([top, bottom]) => (
          <span key={`${top}/${bottom}`} style={{ left: `${(top / bottom) * 100}%` }}>
            {top}⁄{bottom}
          </span>
        ))}
      </div>
      <p className="small muted split-bar-help">
        Drag a seam, or Tab to it and use the arrow keys — shift moves ten times as far.
      </p>
    </div>
  );
}
