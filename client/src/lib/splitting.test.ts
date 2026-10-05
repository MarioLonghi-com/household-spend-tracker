import { describe, expect, it } from "vitest";
import { equalParts, moveSeam, retype } from "./splitting";

/**
 * One property matters above all the others: the parts add up to the whole.
 *
 * `services/transactions.split` refuses a split whose parts do not come to the
 * original -- correctly, because such a "split" moves the account's balance
 * while looking like a reclassification. So a panel that can produce one is a
 * panel that can only produce a 409.
 */
describe("equalParts", () => {
  it("divides evenly when it divides evenly", () => {
    expect(equalParts(-8000, 2)).toEqual([-4000, -4000]);
    expect(equalParts(9000, 3)).toEqual([3000, 3000, 3000]);
  });

  it("puts the odd cent on the last part rather than losing it", () => {
    // €100 three ways is the example everybody reaches for, and the one that
    // exposes a float implementation: 33.33 + 33.33 + 33.33 is 99.99.
    expect(equalParts(-10_000, 3)).toEqual([-3333, -3333, -3334]);
    expect(equalParts(-4815, 2)).toEqual([-2407, -2408]);
  });

  it("spreads the remainder rather than dumping it on the last part", () => {
    // Invisible on ordinary amounts and obvious on small ones. Five cents
    // three ways is 1/2/2, not 1/1/3 -- the end-to-end run found this by
    // splitting a part of an earlier split, where the amounts get tiny.
    expect(equalParts(5, 3)).toEqual([1, 2, 2]);
    expect(equalParts(-5, 3)).toEqual([-1, -2, -2]);
  });

  it("never puts two parts more than one minor unit apart", () => {
    // This is what "equal" has to mean when the total does not divide: the
    // parts add up, and no two of them differ by more than a cent.
    for (const total of [-10_000, -4815, -7978, 5, -5, 7, 623, -623, 1, 33]) {
      for (let count = 2; count <= 5; count++) {
        const parts = equalParts(total, count);
        expect(Math.max(...parts) - Math.min(...parts)).toBeLessThanOrEqual(1);
        expect(parts.reduce((sum, one) => sum + one, 0)).toBe(total);
      }
    }
  });

  it("always adds up, for every count this app allows and either direction", () => {
    // The service permits 2..5 parts. An outflow and a refund have to behave
    // the same way, which is why the helper truncates rather than floors.
    for (const total of [-10_000, -4815, -7978, -1, 1, 250_000, 33, -33]) {
      for (let count = 2; count <= 5; count++) {
        const parts = equalParts(total, count);
        expect(parts).toHaveLength(count);
        expect(parts.reduce((sum, one) => sum + one, 0)).toBe(total);
      }
    }
  });

  it("keeps every part on the same side of zero as the original", () => {
    // The panel asks for magnitudes and takes the direction from the row, so a
    // part with the opposite sign would be rendered as its absolute value and
    // silently change what the split means.
    for (let count = 2; count <= 5; count++) {
      expect(equalParts(-9999, count).every((one) => one < 0)).toBe(true);
      expect(equalParts(9999, count).every((one) => one > 0)).toBe(true);
    }
  });

  it("handles a total smaller than the number of parts without inventing money", () => {
    // Three cents four ways cannot give every part something. What it must not
    // do is round each to a cent and hand back more than there was.
    const parts = equalParts(3, 4);
    expect(parts.reduce((sum, one) => sum + one, 0)).toBe(3);
  });
});

/**
 * The bar's arithmetic (#28). Same property as above: whatever a seam or a
 * typed figure does, the parts still come to the whole and none is empty.
 */
describe("moveSeam", () => {
  const sum = (parts: number[]) => parts.reduce((a, b) => a + b, 0);

  it("moves money between the two parts either side of the seam and nobody else", () => {
    expect(moveSeam([150, 150, 142], 1, 200)).toEqual([150, 50, 242]);
    expect(moveSeam([221, 221], 0, 300)).toEqual([300, 142]);
  });

  it("sticks to a fraction within reach when snapping, and only then", () => {
    // Half of 442 is 221; 230 is within 1/40 of the whole.
    expect(moveSeam([100, 342], 0, 230, true)).toEqual([221, 221]);
    expect(moveSeam([100, 342], 0, 230)).toEqual([230, 212]);
    // A third of 9000 from the second seam of three.
    expect(moveSeam([2000, 2000, 5000], 1, 6100, true)).toEqual([2000, 4000, 3000]);
  });

  it("does not snap from out of reach", () => {
    expect(moveSeam([100, 342], 0, 260, true)).toEqual([260, 182]);
  });

  it("never leaves a part below one minor unit", () => {
    expect(moveSeam([221, 221], 0, -50)).toEqual([1, 441]);
    expect(moveSeam([221, 221], 0, 9999)).toEqual([441, 1]);
    expect(moveSeam([100, 100, 242], 0, 250)).toEqual([199, 1, 242]);
  });

  it("adds up whatever it is given", () => {
    for (const at of [-10, 0, 1, 73.6, 221, 300.4, 441, 500]) {
      for (const snap of [false, true]) {
        const moved = moveSeam([147, 147, 148], 0, at, snap);
        expect(sum(moved)).toBe(442);
        expect(Math.min(...moved)).toBeGreaterThanOrEqual(1);
        expect(moved.every(Number.isInteger)).toBe(true);
      }
    }
  });
});

describe("retype", () => {
  it("lets the next part give or take the difference", () => {
    expect(retype([221, 221], 442, 0, 300)).toEqual([300, 142]);
    expect(retype([147, 147, 148], 442, 1, 100)).toEqual([147, 100, 195]);
  });

  it("uses the part before when the last part is typed", () => {
    expect(retype([147, 147, 148], 442, 2, 200)).toEqual([147, 95, 200]);
  });

  it("works from the whole, so a field cleared on the way still lands", () => {
    expect(retype([221, null], 442, 1, 42)).toEqual([400, 42]);
  });

  it("refuses rather than empty the neighbour or guess at a blank", () => {
    expect(retype([221, 221], 442, 0, 442)).toBeNull();
    expect(retype([221, 221], 442, 0, 0)).toBeNull();
    expect(retype([null, 147, 148], 442, 1, 100)).toBeNull();
  });
});
