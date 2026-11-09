import { describe, expect, it } from "vitest";
import { registerHref, registerPresetFrom } from "./registerLink";

/** The address `registerHref` writes, read back the way the new tab reads it. */
const roundTrip = (link: Parameters<typeof registerHref>[0]) =>
  registerPresetFrom(registerHref(link).slice(1));

describe("a link into Transactions (#401)", () => {
  it("carries one category, a month, the currency, the accounts and the row", () => {
    expect(
      roundTrip({
        household: "hh-1",
        open: "txn-1",
        since: "2026-08-01",
        until: "2026-08-31",
        categories: ["cat-rent"],
        backlog: false,
        currency: "EUR",
        accounts: ["acc-a", "acc-b"],
      }),
    ).toEqual({
      open: "txn-1",
      range: { since: "2026-08-01", until: "2026-08-31" },
      categories: ["cat-rent"],
      backlog: false,
      currencies: ["EUR"],
      accounts: ["acc-a", "acc-b"],
    });
  });

  it("says Needs a category alone as no ids with the backlog ticked", () => {
    const preset = roundTrip({ household: "hh-1", open: "txn-1", categories: [], backlog: true });
    expect(preset?.categories).toEqual([]);
    expect(preset?.backlog).toBe(true);
  });

  it("leaves a facet it was not given unfiltered", () => {
    expect(roundTrip({ household: "hh-1", open: "txn-1" })).toEqual({
      open: "txn-1",
      range: { since: null, until: null },
      categories: null,
      backlog: true,
      currencies: null,
      accounts: undefined,
    });
  });

  it("keeps every category but drops the backlog when only that was asked", () => {
    const preset = roundTrip({ household: "hh-1", open: "txn-1", categories: null, backlog: false });
    expect(preset?.categories).toBeNull();
    expect(preset?.backlog).toBe(false);
  });

  it("names the household in the address, for the shell to check", () => {
    const query = new URLSearchParams(registerHref({ household: "hh-9", open: "txn-1" }).slice(2));
    expect(query.get("open")).toBe("register");
    expect(query.get("household")).toBe("hh-9");
  });

  it("drops anything malformed rather than trust it, and still lands on the row", () => {
    const preset = registerPresetFrom(
      "?open=register&txn=txn-1&since=yesterday&until=2026-13-99x&category=../me&currency=eur&account=a%20b",
    );
    expect(preset).toEqual({
      open: "txn-1",
      range: { since: null, until: null },
      categories: null,
      backlog: true,
      currencies: null,
      accounts: undefined,
    });
  });

  it("is no link at all without a well-formed row, or for another screen", () => {
    expect(registerPresetFrom("?open=register&txn=../../x")).toBeNull();
    expect(registerPresetFrom("?open=register")).toBeNull();
    expect(registerPresetFrom("?open=history&txn=txn-1")).toBeNull();
  });
});
