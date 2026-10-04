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
