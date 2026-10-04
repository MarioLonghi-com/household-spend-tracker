import { describe, expect, it } from "vitest";
import { equalParts } from "./splitting";

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
