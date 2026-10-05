/**
 * The arithmetic behind dividing one transaction into several.
 *
 * Here rather than in the panel because it is pure: minor units in, minor units
 * out, no React and nothing to render. That is also what makes it testable
 * without a DOM, which matters for the one property that actually bites --
 * a split that does not add up is refused by the service, so the panel must
 * never produce one.
 */

/**
 * `total` divided `count` ways, adding up to exactly `total`, with no part
 * more than one minor unit from any other.
 *
 * Integer arithmetic throughout: computing in floats and rounding per part is
 * how you get three parts of 33.33 and a cent nobody can account for.
 *
 * The remainder is **spread** one unit at a time from the last part backwards,
 * not dumped whole on the last part. That distinction is invisible on ordinary
 * amounts -- €100 three ways is 33.33 / 33.33 / 33.34 either way -- and
 * obvious on small ones: 5 cents three ways is 1 / 2 / 2 when spread and
 * 1 / 1 / 3 when dumped, and the second is not a split anybody asked for. An
 * end-to-end run found it by splitting a part of an earlier split.
 *
 * `Math.trunc` rather than `Math.floor` so the sign does not change what
 * "round down" means: an outflow and a refund divide the same way.
 */
export function equalParts(total: number, count: number): number[] {
  if (count < 1) return [];
  const base = Math.trunc(total / count);
  const parts = Array.from({ length: count }, () => base);

  // Same sign as `total`, and always smaller than `count` -- so this loop runs
  // at most count - 1 times and touches each part at most once.
  const remainder = total - base * count;
  const step = remainder < 0 ? -1 : 1;
  for (let spread = 0; spread < Math.abs(remainder); spread++) {
    parts[count - 1 - spread] += step;
  }
  return parts;
}

/**
 * Where a dragged seam sticks. Most splits are one of these, and landing on
 * 2.21 of 4.42 by hand is the arithmetic the bar exists to save.
 */
export const SNAP_FRACTIONS: ReadonlyArray<readonly [number, number]> = [
  [1, 4],
  [1, 3],
  [1, 2],
  [2, 3],
  [3, 4],
];

/** How close a drag has to come to a fraction to stick to it: 1/40 of the whole. */
const SNAP_REACH = 40;

/**
 * Move the seam after part `seam` to `at` minor units from the start, taking
 * from or giving to the part on its other side and nobody else.
 *
 * `magnitudes` are the parts without their sign -- the bar draws sizes, and the
 * panel puts the original's direction back on when it saves. Every part keeps
 * at least one minor unit, because a zero part is refused by the service and a
 * negative one is not a part. With `snap`, a seam within reach of one of
 * `SNAP_FRACTIONS` lands on it exactly; the keyboard passes no snap, so a
 * single cent either side of a half is always reachable.
 */
export function moveSeam(
  magnitudes: number[],
  seam: number,
  at: number,
  snap = false,
): number[] {
  const total = magnitudes.reduce((sum, one) => sum + one, 0);
  const before = magnitudes.slice(0, seam).reduce((sum, one) => sum + one, 0);
  const pair = magnitudes[seam] + magnitudes[seam + 1];

  let target = Math.round(at);
  if (snap) {
    for (const [top, bottom] of SNAP_FRACTIONS) {
      const mark = Math.round((total * top) / bottom);
      if (Math.abs(mark - target) * SNAP_REACH <= total) {
        target = mark;
        break;
      }
    }
  }

  const left = Math.min(Math.max(target - before, 1), pair - 1);
  const moved = [...magnitudes];
  moved[seam] = left;
  moved[seam + 1] = pair - left;
  return moved;
}

/**
 * Part `index` was typed as `typed`; its neighbour becomes whatever makes the
 * parts come to `total`. The neighbour is the part after it, or for the last
 * part the one before. Worked out from the whole rather than from the
 * neighbour's old figure, so clearing a field and typing a new one still
 * lands -- a field emptied on the way is not a figure to adjust from.
 *
 * Null when that cannot be done: another part is blank or unreadable, or the
 * neighbour would fall below one minor unit. The caller keeps what was typed
 * and lets the remainder show, rather than rewriting a figure under
 * somebody's cursor.
 */
export function retype(
  magnitudes: (number | null)[],
  total: number,
  index: number,
  typed: number,
): number[] | null {
  if (typed < 1 || magnitudes.length < 2) return null;
  const other = index < magnitudes.length - 1 ? index + 1 : index - 1;
  let rest = total - typed;
  for (let at = 0; at < magnitudes.length; at++) {
    if (at === index || at === other) continue;
    const one = magnitudes[at];
    if (one === null || one < 1) return null;
    rest -= one;
  }
  if (rest < 1) return null;
  const moved = magnitudes.map((one) => one ?? 0);
  moved[index] = typed;
  moved[other] = rest;
  return moved;
}
