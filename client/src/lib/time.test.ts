/**
 * `localToday` (#23): the day a person in Madrid is living in, not UTC's.
 *
 * Half past midnight on 31 March in Madrid (summer time, UTC+2) is still 30
 * March in UTC, which is what `toISOString().slice(0, 10)` used to answer.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { localToday } from "./time";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllEnvs();
});

describe("localToday", () => {
  it("is Madrid's date at half past midnight there, not UTC's", () => {
    vi.stubEnv("TZ", "Europe/Madrid");
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-03-30T22:30:00Z"));

    expect(localToday()).toBe("2026-03-31");
    expect(new Date().toISOString().slice(0, 10)).toBe("2026-03-30");
  });

  it("reads the instant it is given, in winter time too", () => {
    vi.stubEnv("TZ", "Europe/Madrid");
    // UTC+1 in January: 23:30Z on the 9th is 00:30 on the 10th.
    expect(localToday(new Date("2026-01-09T23:30:00Z"))).toBe("2026-01-10");
    expect(localToday(new Date("2026-01-09T22:30:00Z"))).toBe("2026-01-09");
  });
});
