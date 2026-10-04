// @vitest-environment jsdom

/**
 * Remembered settings: what is stored under which key, what a damaged or
 * foreign value reads as, and that a household switch reads the other
 * household's settings rather than carrying these over.
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { claimScreens, forgetScreens, readSticky, stickyKey, useSticky, writeSticky } from "./sticky";

afterEach(() => {
  window.localStorage.clear();
  vi.restoreAllMocks();
});

const isNumbers = (raw: unknown): raw is number[] =>
  Array.isArray(raw) && raw.every((one) => typeof one === "number");

describe("readSticky", () => {
  it("returns what was written", () => {
    writeSticky("k", ["EUR", "GBP"]);
    expect(readSticky("k", [] as string[])).toEqual(["EUR", "GBP"]);
  });

  it("refuses a value of another shape than the default", () => {
    window.localStorage.setItem("k", JSON.stringify("desc"));
    expect(readSticky("k", 12)).toBeUndefined();
    window.localStorage.setItem("k", JSON.stringify({ a: 1 }));
    expect(readSticky("k", [] as string[])).toBeUndefined();
  });

  it("refuses what the caller's check refuses", () => {
    window.localStorage.setItem("k", JSON.stringify([1, "two"]));
    expect(readSticky("k", [] as number[], isNumbers)).toBeUndefined();
    window.localStorage.setItem("k", JSON.stringify([1, 2]));
    expect(readSticky("k", [] as number[], isNumbers)).toEqual([1, 2]);
  });

  it("reads broken JSON and a throwing store as nothing", () => {
    window.localStorage.setItem("k", "{not json");
    expect(readSticky("k", "")).toBeUndefined();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("private window");
    });
    expect(readSticky("k", "")).toBeUndefined();
  });
});

describe("useSticky", () => {
  it("starts from the default, then remembers under screen and household", () => {
    const { result } = renderHook(() => useSticky("register", "h1", "search", ""));
    expect(result.current[0]).toBe("");
    act(() => result.current[1]("tesco"));
    expect(result.current[0]).toBe("tesco");
    expect(window.localStorage.getItem(stickyKey("register", "h1", "search"))).toBe('"tesco"');
  });

  it("comes back as it was left", () => {
    writeSticky(stickyKey("register", "h1", "size"), 3);
    const { result } = renderHook(() => useSticky("register", "h1", "size", 0));
    expect(result.current[0]).toBe(3);
  });

  it("takes an updater function", () => {
    const { result } = renderHook(() => useSticky("register", "h1", "size", 1));
    act(() => result.current[1]((n) => n + 1));
    act(() => result.current[1]((n) => n + 1));
    expect(result.current[0]).toBe(3);
    expect(readSticky(stickyKey("register", "h1", "size"), 0)).toBe(3);
  });

  it("reads the other household's value on a switch, and leaves this one's alone", () => {
    writeSticky(stickyKey("register", "h2", "search"), "rent");
    const { result, rerender } = renderHook(({ h }) => useSticky("register", h, "search", ""), {
      initialProps: { h: "h1" },
    });
    act(() => result.current[1]("tesco"));
    rerender({ h: "h2" });
    expect(result.current[0]).toBe("rent");
    act(() => result.current[1]("gas"));
    expect(readSticky(stickyKey("register", "h1", "search"), "")).toBe("tesco");
    expect(readSticky(stickyKey("register", "h2", "search"), "")).toBe("gas");
  });

  it("still holds a value for the page's life when the store throws", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("private window");
    });
    const { result } = renderHook(() => useSticky("register", "h1", "search", ""));
    act(() => result.current[1]("tesco"));
    expect(result.current[0]).toBe("tesco");
  });
});

describe("forgetting whose settings they were (#198)", () => {
  const personal = [
    stickyKey("register", "house-a", "search"),
    stickyKey("register", "house-b", "search"),
    stickyKey("reimbursements", "house-a", "range"),
  ];
  const device = ["spendtracker.register.columns", "spendtracker.appearance", "spendtracker.shell.navCollapsed"];

  function seed() {
    for (const key of [...personal, ...device]) writeSticky(key, "x");
  }

  it("forgets the register's and the reimbursements' settings, and nothing else", () => {
    seed();
    forgetScreens();
    for (const key of personal) expect(window.localStorage.getItem(key)).toBeNull();
    for (const key of device) expect(window.localStorage.getItem(key)).toBe(JSON.stringify("x"));
  });

  it("keeps them for the same person and forgets them for somebody else", () => {
    claimScreens("user-a");
    seed();
    claimScreens("user-a");
    for (const key of personal) expect(window.localStorage.getItem(key)).toBe(JSON.stringify("x"));

    claimScreens("user-b");
    for (const key of personal) expect(window.localStorage.getItem(key)).toBeNull();
    for (const key of device) expect(window.localStorage.getItem(key)).toBe(JSON.stringify("x"));
  });
});
